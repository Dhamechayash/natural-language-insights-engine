"""Job handlers: what workers actually do."""

import logging
from pathlib import Path
from typing import Callable

from app.config import Settings
from app.engine import ask, profile_to_text
from app.errors import JobError
from app.ingest import ingest_csv
from app.jobs import Store
from app.llm import LLM, get_llm

log = logging.getLogger("tasks")
MAX_HISTORY_TURNS = 5
HISTORY_ROWS = 5


def make_handler(store: Store, settings: Settings, llm_factory: Callable[[Settings], LLM] = get_llm):
    def handle(job: dict) -> tuple[str, dict]:
        if job["kind"] == "ingest":
            return _ingest(job, store, settings, llm_factory)
        if job["kind"] == "question":
            return _question(job, store, settings, llm_factory)
        raise JobError("unknown_job_kind", f"Unknown job kind {job['kind']}")

    return handle


def mark_dataset_failed(store: Store):
    """Hook for WorkerPool: a failed ingest job marks its dataset failed."""

    def hook(job: dict, error: dict):
        if job["kind"] == "ingest":
            store.update_dataset(job["dataset_id"], "failed", error=error)

    return hook


def _ingest(job, store, settings, llm_factory):
    dataset = store.get_dataset(job["dataset_id"])
    if dataset is None:
        raise JobError("dataset_not_found", "Dataset no longer exists.")
    profile = ingest_csv(Path(dataset["source_path"]), settings.dataset_dir(dataset["id"]))
    # Optional enrichment: the data is already queryable without it, so failure here is not fatal.
    if settings.anthropic_api_key:
        try:
            profile["annotations"] = llm_factory(settings).annotate(profile_to_text(profile))
        except Exception:
            log.warning("schema annotation skipped for %s", dataset["id"], exc_info=True)
    store.update_dataset(dataset["id"], "ready", profile=profile)
    return "succeeded", {"dataset_id": dataset["id"], "rows": profile["row_count"], "columns": len(profile["columns"])}


def _question(job, store, settings, llm_factory):
    dataset = store.get_dataset(job["dataset_id"])
    if dataset is None or dataset["status"] != "ready":
        raise JobError("dataset_not_ready", "Dataset is not available for questions.")
    llm = llm_factory(settings)
    db_file = settings.dataset_dir(dataset["id"]) / "data.duckdb"
    history = thread_history(store, job["payload"].get("follow_up_of"), dataset["id"])
    result = ask(job["payload"]["question"], db_file, dataset["profile"], llm, settings, history)
    status = result.pop("status")
    # The pool marks the job finished right after this returns; lookups only match finished jobs.
    if job["payload"].get("cache_key"):
        store.cache_put(job["payload"]["cache_key"], dataset["id"], job["id"])
    return status, result


def thread_history(store: Store, parent_id: str | None, dataset_id: str) -> list[dict]:
    """Earlier turns of a follow-up thread, oldest first, capped to the most recent MAX_HISTORY_TURNS."""
    turns = []
    while parent_id and len(turns) < MAX_HISTORY_TURNS:
        parent = store.get_job(parent_id)
        if parent is None or parent["kind"] != "question" or parent["dataset_id"] != dataset_id:
            break
        r = parent["result"] or {}
        turn = {"question": parent["payload"]["question"]}
        if parent["status"] == "succeeded":
            turn.update(sql=r.get("sql"), answer=r.get("answer"), columns=r.get("columns"), rows=(r.get("rows") or [])[:HISTORY_ROWS])
        elif parent["status"] == "refused":
            turn["refused"] = r.get("reason")
        turns.append(turn)
        parent_id = parent["payload"].get("follow_up_of")
    return turns[::-1]
