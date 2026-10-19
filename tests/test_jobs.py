import threading
import time

from app.errors import JobError
from app.jobs import Store, WorkerPool


def make_store(tmp_path):
    return Store(tmp_path / "jobs.sqlite")


def test_concurrent_claims_never_double_run(tmp_path):
    store = make_store(tmp_path)
    ds = store.create_dataset("d", "x")
    for _ in range(40):
        store.enqueue("question", ds, {})
    claimed, lock = [], threading.Lock()

    def worker():
        while (job := store.claim_next(60)) is not None:
            with lock:
                claimed.append(job["id"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(claimed) == 40 and len(set(claimed)) == 40


def test_expired_lease_is_requeued_then_failed(tmp_path):
    store = make_store(tmp_path)
    ds = store.create_dataset("d", "x")
    job, _ = store.enqueue("question", ds, {})
    store.claim_next(lease_s=-1)  # worker "dies" immediately
    assert store.reap_expired(max_attempts=2) == []
    assert store.get_job(job["id"])["status"] == "queued"
    store.claim_next(lease_s=-1)
    assert store.reap_expired(max_attempts=2) == [job["id"]]
    failed = store.get_job(job["id"])
    assert failed["status"] == "failed" and failed["error"]["code"] == "worker_lost"


def test_idempotency_key_returns_same_job(tmp_path):
    store = make_store(tmp_path)
    ds = store.create_dataset("d", "x")
    a, created_a = store.enqueue("question", ds, {}, "k1")
    b, created_b = store.enqueue("question", ds, {}, "k1")
    assert a["id"] == b["id"] and created_a and not created_b


def run_pool(store, handler, attempts=3):
    pool = WorkerPool(store, handler, workers=1, max_attempts=attempts, lease_s=60)
    pool._stop.wait = lambda *_: False  # no real backoff sleeps in tests
    while (job := store.claim_next(60)) is not None:
        pool.run_one(job)


def test_retryable_error_retries_then_succeeds(tmp_path):
    store = make_store(tmp_path)
    job, _ = store.enqueue("question", store.create_dataset("d", "x"), {})
    calls = []

    def handler(j):
        calls.append(1)
        if len(calls) < 3:
            raise JobError("llm_unavailable", "later", retryable=True)
        return "succeeded", {"ok": True}

    run_pool(store, handler)
    assert store.get_job(job["id"])["status"] == "succeeded" and len(calls) == 3


def test_retries_exhausted_marks_failed(tmp_path):
    store = make_store(tmp_path)
    job, _ = store.enqueue("question", store.create_dataset("d", "x"), {})

    def handler(j):
        raise JobError("llm_unavailable", "later", retryable=True)

    run_pool(store, handler, attempts=2)
    done = store.get_job(job["id"])
    assert done["status"] == "failed" and done["attempts"] == 2


def test_unexpected_exception_hides_internals(tmp_path):
    store = make_store(tmp_path)
    job, _ = store.enqueue("question", store.create_dataset("d", "x"), {})

    def handler(j):
        raise RuntimeError("secret path /etc/foo")

    run_pool(store, handler)
    err = store.get_job(job["id"])["error"]
    assert err["code"] == "internal_error" and "secret" not in err["message"]


def test_worker_pool_end_to_end_processes_jobs(tmp_path):
    store = make_store(tmp_path)
    ds = store.create_dataset("d", "x")
    ids = [store.enqueue("question", ds, {})[0]["id"] for _ in range(10)]
    pool = WorkerPool(store, lambda j: ("succeeded", {"n": 1}), workers=3, max_attempts=3, lease_s=60, poll_s=0.01)
    pool.start()
    deadline = time.time() + 5
    while time.time() < deadline and any(store.get_job(i)["status"] != "succeeded" for i in ids):
        time.sleep(0.05)
    pool.stop()
    assert all(store.get_job(i)["status"] == "succeeded" for i in ids)
