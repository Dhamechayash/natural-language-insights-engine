"""Durable job store (SQLite) and worker pool.

Claiming is one atomic transaction, so concurrent workers never run the same job.
A job holds a lease while running; if a worker dies the lease expires and the job is
requeued (up to max_attempts) by the reaper.
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from app.errors import JobError

log = logging.getLogger("jobs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
    source_path TEXT, profile TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, dataset_id TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL, result TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    lease_until REAL, idempotency_key TEXT UNIQUE, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS question_cache (
    key TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, source_job_id TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, last_hit_at REAL
);
"""

TERMINAL = {"succeeded", "failed", "refused"}


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ---- datasets
    def create_dataset(self, name: str, source_path: str) -> str:
        dataset_id, now = uuid.uuid4().hex[:12], time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO datasets(id,name,status,source_path,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (dataset_id, name, "pending", source_path, now, now),
            )
        return dataset_id

    def update_dataset(self, dataset_id: str, status: str, profile: dict | None = None, error: dict | None = None):
        with self._conn() as c:
            c.execute(
                "UPDATE datasets SET status=?, profile=?, error=?, updated_at=? WHERE id=?",
                (status, json.dumps(profile) if profile else None, json.dumps(error) if error else None, time.time(), dataset_id),
            )

    def get_dataset(self, dataset_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        return _decode(row, ("profile", "error"))

    def list_datasets(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()
        return [_decode(r, ("profile", "error")) for r in rows]

    # ---- jobs
    def queue_depth(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]

    def enqueue(self, kind: str, dataset_id: str, payload: dict, idempotency_key: str | None = None) -> tuple[dict, bool]:
        """Returns (job, created). A repeated idempotency key returns the original job."""
        now = time.time()
        job_id = uuid.uuid4().hex
        with self._conn() as c:
            try:
                c.execute(
                    "INSERT INTO jobs(id,kind,dataset_id,payload,status,idempotency_key,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (job_id, kind, dataset_id, json.dumps(payload), "queued", idempotency_key, now, now),
                )
                created = True
            except sqlite3.IntegrityError:
                row = c.execute("SELECT id FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                job_id, created = row["id"], False
        return self.get_job(job_id), created

    def get_job_by_idempotency_key(self, key: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
        return _decode(row, ("payload", "result", "error"))

    def create_finished_job(self, kind: str, dataset_id: str, payload: dict, status: str, result: dict) -> dict:
        """Record a job that needed no worker, e.g. a question answered from the cache."""
        now, job_id = time.time(), uuid.uuid4().hex
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs(id,kind,dataset_id,payload,status,result,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, kind, dataset_id, json.dumps(payload), status, json.dumps(result), now, now),
            )
        return self.get_job(job_id)

    # ---- question cache: entries point at the job that produced the answer rather than copying it
    def cache_lookup(self, key: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT j.* FROM question_cache q JOIN jobs j ON j.id = q.source_job_id "
                "WHERE q.key=? AND j.status IN ('succeeded','refused')",
                (key,),
            ).fetchone()
        return _decode(row, ("payload", "result", "error"))

    def cache_put(self, key: str, dataset_id: str, source_job_id: str):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO question_cache(key,dataset_id,source_job_id,created_at) VALUES (?,?,?,?)",
                (key, dataset_id, source_job_id, time.time()),
            )

    def cache_record_hit(self, key: str):
        with self._conn() as c:
            c.execute("UPDATE question_cache SET hits=hits+1, last_hit_at=? WHERE key=?", (time.time(), key))

    def get_job(self, job_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _decode(row, ("payload", "result", "error"))

    def claim_next(self, lease_s: float) -> dict | None:
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if row is None:
                    c.execute("COMMIT")
                    return None
                c.execute(
                    "UPDATE jobs SET status='running', attempts=attempts+1, lease_until=?, updated_at=? WHERE id=?",
                    (now + lease_s, now, row["id"]),
                )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return self.get_job(row["id"])

    def finish(self, job_id: str, status: str, result: dict | None = None, error: dict | None = None):
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET status=?, result=?, error=?, lease_until=NULL, updated_at=? WHERE id=?",
                (status, json.dumps(result) if result is not None else None, json.dumps(error) if error else None, time.time(), job_id),
            )

    def requeue(self, job_id: str):
        with self._conn() as c:
            c.execute("UPDATE jobs SET status='queued', lease_until=NULL, updated_at=? WHERE id=?", (time.time(), job_id))

    def reap_expired(self, max_attempts: int) -> list[str]:
        """Requeue jobs whose worker vanished; fail those out of attempts. Returns ids of jobs that were failed."""
        now = time.time()
        failed = []
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT id, attempts FROM jobs WHERE status='running' AND lease_until < ?", (now,)).fetchall()
            for r in rows:
                if r["attempts"] >= max_attempts:
                    err = json.dumps({"code": "worker_lost", "message": "The job did not complete after several attempts."})
                    c.execute("UPDATE jobs SET status='failed', error=?, lease_until=NULL, updated_at=? WHERE id=?", (err, now, r["id"]))
                    failed.append(r["id"])
                else:
                    c.execute("UPDATE jobs SET status='queued', lease_until=NULL, updated_at=? WHERE id=?", (now, r["id"]))
            c.execute("COMMIT")
        return failed


def _decode(row, json_fields) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for f in json_fields:
        out[f] = json.loads(out[f]) if out.get(f) else None
    return out


Handler = Callable[[dict], tuple[str, dict]]


class WorkerPool:
    """N polling threads. The handler returns (status, result) or raises JobError."""

    def __init__(self, store: Store, handler: Handler, workers: int, max_attempts: int, lease_s: float, poll_s: float = 0.2,
                 on_job_failed: Callable[[dict, dict], None] | None = None):
        self.store, self.handler = store, handler
        self.n, self.max_attempts, self.lease_s, self.poll_s = workers, max_attempts, lease_s, poll_s
        self.on_job_failed = on_job_failed
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self):
        for i in range(self.n):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            try:
                for job_id in self.store.reap_expired(self.max_attempts):
                    lost = self.store.get_job(job_id)
                    if self.on_job_failed and lost:
                        self.on_job_failed(lost, lost["error"])
                job = self.store.claim_next(self.lease_s)
            except Exception:
                log.exception("job store error")
                self._stop.wait(1)
                continue
            if job is None:
                self._stop.wait(self.poll_s)
                continue
            self.run_one(job)

    def run_one(self, job: dict):
        try:
            status, result = self.handler(job)
            self.store.finish(job["id"], status, result=result)
        except JobError as exc:
            if exc.retryable and job["attempts"] < self.max_attempts:
                log.warning("job %s retryable error %s (attempt %s)", job["id"], exc.code, job["attempts"])
                self.store.requeue(job["id"])
                self._stop.wait(min(2 ** job["attempts"], 10))
            else:
                self._fail(job, {"code": exc.code, "message": exc.message})
        except Exception:
            log.exception("job %s crashed", job["id"])
            self._fail(job, {"code": "internal_error", "message": "Something went wrong processing this job."})

    def _fail(self, job: dict, error: dict):
        self.store.finish(job["id"], "failed", error=error)
        if self.on_job_failed:
            self.on_job_failed(job, error)
