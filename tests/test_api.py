import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.fakes import FakeLLM, answerable, refusal

CSV = "id,qty,price\n1,2,3.5\n2,1,10\n"


@pytest.fixture
def make_client(settings):
    from dataclasses import replace

    clients = []

    def build(script=None, **overrides):
        s = replace(settings, start_workers=True, **overrides)
        llm = FakeLLM(script or [answerable("SELECT sum(qty * price) AS revenue FROM data")])
        client = TestClient(create_app(s, llm_factory=lambda _s: llm))
        client.__enter__()
        client.llm = llm
        clients.append(client)
        return client

    yield build
    for c in clients:
        c.__exit__(None, None, None)


def wait_job(client, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "refused"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def upload(client, content=CSV, name="t.csv"):
    r = client.post("/datasets", files={"file": (name, content, "text/csv")})
    assert r.status_code == 202, r.text
    body = r.json()
    assert wait_job(client, body["job_id"])["status"] == "succeeded"
    return body["dataset_id"]


def test_full_flow_upload_then_question(make_client):
    client = make_client()
    ds = upload(client)
    assert client.get(f"/datasets/{ds}").json()["rows"] == 2
    r = client.post(f"/datasets/{ds}/questions", json={"question": "total revenue?"})
    assert r.status_code == 202
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "succeeded" and job["result"]["rows"] == [[17.0]]
    assert "sql" in job["result"]


def test_refusal_is_a_distinct_terminal_status(make_client):
    client = make_client([refusal("no cost column")])
    ds = upload(client)
    job = wait_job(client, client.post(f"/datasets/{ds}/questions", json={"question": "profit margin?"}).json()["job_id"])
    assert job["status"] == "refused" and "cost" in job["result"]["reason"]


def test_bad_csv_marks_dataset_failed(make_client):
    client = make_client()
    r = client.post("/datasets", files={"file": ("x.csv", "a,b\n", "text/csv")})
    assert wait_job(client, r.json()["job_id"])["status"] == "failed"
    d = client.get(f"/datasets/{r.json()['dataset_id']}").json()
    assert d["status"] == "failed" and d["error"]["code"] == "empty_csv"


@pytest.mark.parametrize(
    "call,status,code",
    [
        (lambda c: c.get("/jobs/nope"), 404, "job_not_found"),
        (lambda c: c.get("/datasets/nope"), 404, "dataset_not_found"),
        (lambda c: c.post("/datasets/nope/questions", json={"question": "hello there"}), 404, "dataset_not_found"),
        (lambda c: c.post("/datasets", files={"file": ("x.exe", "abc", "text/plain")}), 415, "unsupported_file_type"),
        (lambda c: c.post("/datasets", files={"file": ("x.csv", "", "text/csv")}), 400, "empty_file"),
        (lambda c: c.post("/datasets", files={"file": ("x.csv", "a" * (2 * 1024 * 1024), "text/csv")}), 413, "file_too_large"),
        (lambda c: c.post("/datasets"), 422, "invalid_request"),
        (lambda c: c.get("/nope"), 404, "not_found"),
    ],
)
def test_error_shapes(make_client, call, status, code):
    resp = call(make_client())
    assert resp.status_code == status
    err = resp.json()["error"]
    assert err["code"] == code and err["request_id"] and "Traceback" not in resp.text


def test_question_validation(make_client):
    client = make_client()
    ds = upload(client)
    for bad in ({}, {"question": ""}, {"question": "x" * 501}):
        assert client.post(f"/datasets/{ds}/questions", json=bad).status_code == 422


def test_question_on_unready_dataset_is_409(make_client, settings):
    client = make_client()
    ds = client.app.state.store.create_dataset("d", "x")
    r = client.post(f"/datasets/{ds}/questions", json={"question": "hello there"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "dataset_not_ready"


def test_idempotency_key(make_client):
    client = make_client()
    ds = upload(client)
    h = {"Idempotency-Key": "abc"}
    a = client.post(f"/datasets/{ds}/questions", json={"question": "total revenue"}, headers=h)
    b = client.post(f"/datasets/{ds}/questions", json={"question": "total revenue"}, headers=h)
    assert a.json()["job_id"] == b.json()["job_id"] and (a.status_code, b.status_code) == (202, 200)


def test_idempotency_key_wins_over_cache_after_completion(make_client):
    client = make_client()
    ds = upload(client)
    h = {"Idempotency-Key": "k1"}
    a = client.post(f"/datasets/{ds}/questions", json={"question": "total revenue"}, headers=h)
    wait_job(client, a.json()["job_id"])
    b = client.post(f"/datasets/{ds}/questions", json={"question": "total revenue"}, headers=h)
    assert b.json()["job_id"] == a.json()["job_id"] and b.status_code == 200


def test_backpressure_returns_429(make_client):
    client = make_client(max_queue=2)
    ds = upload(client)
    client.app.state.pool.stop()  # nothing drains the queue now
    store = client.app.state.store
    for _ in range(2):
        store.enqueue("question", ds, {"question": "x"})
    r = client.post(f"/datasets/{ds}/questions", json={"question": "one more please"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "queue_full"


def test_missing_api_key_fails_job_cleanly(settings):
    from dataclasses import replace

    s = replace(settings, start_workers=True, anthropic_api_key="")
    with TestClient(create_app(s)) as client:  # real llm factory, no key
        ds = upload(client)
        job = wait_job(client, client.post(f"/datasets/{ds}/questions", json={"question": "anything"}).json()["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "llm_not_configured"


def ask_q(client, ds, question, follow_up_of=None):
    r = client.post(f"/datasets/{ds}/questions", json={"question": question, "follow_up_of": follow_up_of})
    assert r.status_code in (200, 202), r.text  # 200 = answered from cache
    return wait_job(client, r.json()["job_id"])


def test_follow_up_sends_earlier_turns_oldest_first(make_client):
    client = make_client()
    ds = upload(client)
    first = ask_q(client, ds, "total revenue?")
    assert client.llm.history == []
    second = ask_q(client, ds, "and per item?", follow_up_of=first["id"])
    ask_q(client, ds, "now sort it", follow_up_of=second["id"])
    history = client.llm.history
    assert [t["question"] for t in history] == ["total revenue?", "and per item?"]
    assert history[0]["sql"] and history[0]["rows"] == [[17.0]]
    assert second["question"] == "and per item?" and second["follow_up_of"] == first["id"]


def test_follow_up_of_refused_turn_carries_the_reason(make_client):
    client = make_client([refusal("no cost column")])
    ds = upload(client)
    first = ask_q(client, ds, "profit margin?")
    ask_q(client, ds, "what about revenue then?", follow_up_of=first["id"])
    assert client.llm.history == [{"question": "profit margin?", "refused": "no cost column"}]


def test_follow_up_history_is_capped(make_client):
    from app.tasks import MAX_HISTORY_TURNS

    client = make_client()
    ds = upload(client)
    parent_id = None
    for i in range(MAX_HISTORY_TURNS + 2):
        parent_id = ask_q(client, ds, f"question {i}", follow_up_of=parent_id)["id"]
    ask_q(client, ds, "last one", follow_up_of=parent_id)
    assert len(client.llm.history) == MAX_HISTORY_TURNS
    assert client.llm.history[-1]["question"] == f"question {MAX_HISTORY_TURNS + 1}"


def test_follow_up_must_reference_a_question_on_the_same_dataset(make_client):
    client = make_client()
    ds, other = upload(client), upload(client)
    first = ask_q(client, ds, "total revenue?")
    ingest_job = client.post("/datasets", files={"file": ("t.csv", CSV, "text/csv")}).json()["job_id"]
    for parent in ("nope", first["id"], ingest_job):
        target = other if parent == first["id"] else ds
        r = client.post(f"/datasets/{target}/questions", json={"question": "and then?", "follow_up_of": parent})
        assert r.status_code == 404 and r.json()["error"]["code"] == "follow_up_not_found"


def test_follow_up_of_unfinished_question_is_409(make_client):
    client = make_client()
    ds = upload(client)
    client.app.state.pool.stop()
    pending, _ = client.app.state.store.enqueue("question", ds, {"question": "still running"})
    r = client.post(f"/datasets/{ds}/questions", json={"question": "and then?", "follow_up_of": pending["id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "follow_up_not_ready"


def post_q(client, ds, question, **extra):
    r = client.post(f"/datasets/{ds}/questions", json={"question": question, **extra})
    assert r.status_code in (200, 202), r.text
    return r, wait_job(client, r.json()["job_id"])


def test_repeated_question_is_answered_from_cache(make_client):
    client = make_client(anthropic_model="claude-sonnet-5-5")
    ds = upload(client)
    _, first = post_q(client, ds, "Total revenue?")
    r, second = post_q(client, ds, "  total   REVENUE ")  # case, spacing and trailing ? are ignored
    assert client.llm.sessions == 1
    assert r.status_code == 200 and r.json()["cached"] is True
    assert second["status"] == "succeeded" and second["result"]["rows"] == first["result"]["rows"]
    assert second["result"]["usage"] == {"input_tokens": 0, "output_tokens": 0}
    saved = second["result"]["cache"]["saved"]
    assert saved["input_tokens"] == first["result"]["usage"]["input_tokens"] and saved["total_tokens"] > 0
    assert saved["cost_usd"] == round((saved["input_tokens"] * 2 + saved["output_tokens"] * 10) / 1e6, 6)
    assert second["result"]["cache"]["source_job_id"] == first["id"] and "cache" not in first["result"]


def test_no_cache_regenerates_and_replaces_the_entry(make_client):
    client = make_client()
    ds = upload(client)
    post_q(client, ds, "total revenue")
    _, fresh = post_q(client, ds, "total revenue", no_cache=True)
    assert client.llm.sessions == 2 and "cache" not in fresh["result"]
    _, hit = post_q(client, ds, "total revenue")
    assert client.llm.sessions == 2 and hit["result"]["cache"]["source_job_id"] == fresh["id"]


def test_refusals_are_cached(make_client):
    client = make_client([refusal("no cost column")])
    ds = upload(client)
    post_q(client, ds, "profit margin?")
    _, hit = post_q(client, ds, "profit margin?")
    assert client.llm.sessions == 1 and hit["status"] == "refused" and hit["result"]["cache"]


def test_follow_up_in_a_different_thread_is_not_a_cache_hit(make_client):
    client = make_client()
    ds = upload(client)
    a = ask_q(client, ds, "total revenue?")
    b = ask_q(client, ds, "revenue per item?")
    ask_q(client, ds, "and sorted?", follow_up_of=a["id"])
    other_thread = ask_q(client, ds, "and sorted?", follow_up_of=b["id"])
    assert client.llm.sessions == 4 and "cache" not in other_thread["result"]
    same_thread = ask_q(client, ds, "and sorted?", follow_up_of=a["id"])
    assert client.llm.sessions == 4 and same_thread["result"]["cache"]


def test_cache_is_per_dataset_and_model(make_client, settings):
    from app.cache import cache_key

    assert cache_key("d1", "q", [], "m1") != cache_key("d2", "q", [], "m1")
    assert cache_key("d1", "q", [], "m1") != cache_key("d1", "q", [], "m2")
    assert cache_key("d1", "Q?", [], "m1") == cache_key("d1", "q", [], "m1")


def test_unknown_model_reports_tokens_but_no_cost():
    from app.cache import savings

    assert savings({"input_tokens": 10, "output_tokens": 5}, "test") == {
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cost_usd": None, "model": "test",
    }


def test_health(make_client):
    assert make_client().get("/health").json()["status"] == "ok"
