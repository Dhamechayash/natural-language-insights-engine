"""HTTP API. Thin by design: validate, enqueue, report. Work happens in the worker pool."""

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.cache import cache_key, savings
from app.charts import recommend_chart
from app.config import Settings, get_settings
from app.errors import AppError
from app.jobs import Store, WorkerPool
from app.tasks import make_handler, mark_dataset_failed, thread_history

log = logging.getLogger("api")
STATIC = Path(__file__).parent / "static"
CHUNK = 1024 * 1024


class QuestionIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    follow_up_of: str | None = Field(None, max_length=64, description="Job id of the previous question in this thread.")
    no_cache: bool = Field(False, description="Skip the cache and generate a fresh answer (which then replaces the cached one).")


def _error_response(request: Request, status: int, code: str, message: str, details=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status)


def _public_job(job: dict) -> dict:
    return {
        "id": job["id"],
        "kind": job["kind"],
        "dataset_id": job["dataset_id"],
        "question": job["payload"].get("question"),
        "follow_up_of": job["payload"].get("follow_up_of"),
        "status": job["status"],
        "attempts": job["attempts"],
        "result": job["result"],
        "error": job["error"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


def _public_dataset(d: dict, with_profile: bool = False) -> dict:
    out = {"id": d["id"], "name": d["name"], "status": d["status"], "error": d["error"], "created_at": d["created_at"]}
    if d["profile"]:
        out["rows"] = d["profile"]["row_count"]
        out["columns"] = [{"name": c["name"], "type": c["type"]} for c in d["profile"]["columns"]]
        if with_profile:
            out["profile"] = d["profile"]
    return out


def create_app(settings: Settings | None = None, llm_factory=None) -> FastAPI:
    settings = settings or get_settings()
    store = Store(settings.db_path)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    handler_kwargs = {"llm_factory": llm_factory} if llm_factory else {}
    pool = WorkerPool(
        store,
        make_handler(store, settings, **handler_kwargs),
        workers=settings.workers,
        max_attempts=settings.max_attempts,
        lease_s=settings.lease_s,
        on_job_failed=mark_dataset_failed(store),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.start_workers:
            pool.start()
        yield
        pool.stop()

    app = FastAPI(title="Natural Language Insights Engine", lifespan=lifespan)
    app.state.store, app.state.pool, app.state.settings = store, pool, settings

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(AppError)
    async def _app_error(request, exc: AppError):
        return _error_response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error_response(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return _error_response(request, 422, "invalid_request", "The request is invalid.", details)

    @app.exception_handler(Exception)
    async def _unhandled(request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return _error_response(request, 500, "internal_error", "Something went wrong.")

    def _backpressure():
        if store.queue_depth() >= settings.max_queue:
            raise AppError("queue_full", "The server is busy; retry shortly.", 429)

    @app.get("/health")
    def health():
        return {"status": "ok", "queue_depth": store.queue_depth(), "llm_configured": bool(settings.anthropic_api_key)}

    @app.post("/datasets", status_code=202)
    async def upload_dataset(file: UploadFile = File(...), name: str | None = Form(None)):
        _backpressure()
        filename = file.filename or "upload.csv"
        if not filename.lower().endswith((".csv", ".tsv", ".txt")):
            raise AppError("unsupported_file_type", "Upload a .csv file.", 415)
        limit = settings.max_upload_mb * CHUNK
        tmp = settings.uploads_dir / f"{uuid.uuid4().hex}.part"
        size = 0
        try:
            with open(tmp, "wb") as out:
                while chunk := await file.read(CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise AppError("file_too_large", f"File exceeds {settings.max_upload_mb} MB.", 413)
                    out.write(chunk)
            if size == 0:
                raise AppError("empty_file", "The uploaded file is empty.", 400)
            dataset_id = store.create_dataset((name or filename)[:200], "")
            final = settings.uploads_dir / f"{dataset_id}.csv"
            tmp.replace(final)
        finally:
            tmp.unlink(missing_ok=True)
        with store._conn() as c:
            c.execute("UPDATE datasets SET source_path=? WHERE id=?", (str(final), dataset_id))
        job, _ = store.enqueue("ingest", dataset_id, {})
        return {"dataset_id": dataset_id, "job_id": job["id"], "status_url": f"/jobs/{job['id']}"}

    @app.get("/datasets")
    def list_datasets():
        return {"datasets": [_public_dataset(d) for d in store.list_datasets()]}

    @app.get("/datasets/{dataset_id}")
    def get_dataset(dataset_id: str):
        d = store.get_dataset(dataset_id)
        if d is None:
            raise AppError("dataset_not_found", "No such dataset.", 404)
        return _public_dataset(d, with_profile=True)

    @app.post("/datasets/{dataset_id}/questions", status_code=202)
    def ask_question(dataset_id: str, body: QuestionIn, idempotency_key: str | None = Header(None, max_length=128)):
        d = store.get_dataset(dataset_id)
        if d is None:
            raise AppError("dataset_not_found", "No such dataset.", 404)
        if d["status"] != "ready":
            raise AppError("dataset_not_ready", f"Dataset is {d['status']}; questions need a ready dataset.", 409)
        payload = {"question": body.question.strip()}
        if body.follow_up_of:
            parent = store.get_job(body.follow_up_of)
            if parent is None or parent["kind"] != "question" or parent["dataset_id"] != dataset_id:
                raise AppError("follow_up_not_found", "No such earlier question on this dataset.", 404)
            if parent["status"] not in ("succeeded", "refused"):
                raise AppError("follow_up_not_ready", "The earlier question has not finished yet.", 409)
            payload["follow_up_of"] = parent["id"]
        key = f"{dataset_id}:{idempotency_key}" if idempotency_key else None
        if key and (existing := store.get_job_by_idempotency_key(key)):  # a retried request gets its original job
            return JSONResponse({"job_id": existing["id"], "status_url": f"/jobs/{existing['id']}"}, status_code=200)
        history = thread_history(store, payload.get("follow_up_of"), dataset_id)
        payload["cache_key"] = cache_key(dataset_id, payload["question"], history, settings.anthropic_model)
        if not body.no_cache and (source := store.cache_lookup(payload["cache_key"])):
            result = dict(source["result"])
            if not result.get("chart") and result.get("rows"):  # answered before charts existed or before the picker improved
                result["chart"] = recommend_chart(result["columns"], result["rows"], result.get("truncated", False))
            result["cache"] = {
                "source_job_id": source["id"],
                "answered_at": source["updated_at"],
                "saved": savings(result.get("usage") or {}, settings.anthropic_model),
            }
            result["usage"] = {"input_tokens": 0, "output_tokens": 0}
            job = store.create_finished_job("question", dataset_id, payload, source["status"], result)
            store.cache_record_hit(payload["cache_key"])
            return JSONResponse({"job_id": job["id"], "status_url": f"/jobs/{job['id']}", "cached": True}, status_code=200)
        _backpressure()
        job, created = store.enqueue("question", dataset_id, payload, key)
        return JSONResponse({"job_id": job["id"], "status_url": f"/jobs/{job['id']}"}, status_code=202 if created else 200)

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str):
        job = store.get_job(job_id)
        if job is None:
            raise AppError("job_not_found", "No such job.", 404)
        return _public_job(job)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def default_app() -> FastAPI:
    return create_app()
