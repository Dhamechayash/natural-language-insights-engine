import duckdb
import pytest

from app.engine import ask, numbers_grounded, run_readonly
from app.errors import JobError
from tests.fakes import FakeLLM, answerable, refusal

REVENUE = "SELECT sum(quantity * unitprice) AS revenue FROM data"


def test_answers_with_sql_and_assumptions(retail_db, settings):
    db, profile = retail_db
    llm = FakeLLM([answerable(REVENUE, ["revenue = quantity * unitprice"])])
    result = ask("total revenue", db, profile, llm, settings)
    assert result["status"] == "succeeded"
    assert result["sql"] == REVENUE
    assert result["assumptions"] == ["revenue = quantity * unitprice"]
    assert result["usage"]["input_tokens"] > 0
    assert result["answer_verified"] is True


def test_refuses_when_llm_says_unanswerable(retail_db, settings):
    db, profile = retail_db
    result = ask("what is our profit margin?", db, profile, FakeLLM([refusal("no cost column")]), settings)
    assert result["status"] == "refused"
    assert "cost" in result["reason"]


def test_invented_column_is_fed_back_then_repaired(retail_db, settings):
    db, profile = retail_db
    llm = FakeLLM([answerable("SELECT sum(profit) FROM data"), answerable("SELECT count(*) AS n FROM data")])
    result = ask("how many rows", db, profile, llm, settings)
    assert result["status"] == "succeeded"
    assert len(llm.session.errors) == 1 and "profit" in llm.session.errors[0]


def test_gives_up_with_refusal_after_repeated_bad_sql(retail_db, settings):
    db, profile = retail_db
    llm = FakeLLM([answerable("SELECT nope FROM data")])
    result = ask("anything", db, profile, llm, settings)
    assert result["status"] == "refused"
    assert llm.session.calls == 3


def test_disallowed_sql_never_runs(retail_db, settings):
    db, profile = retail_db
    llm = FakeLLM([answerable("DROP TABLE data"), refusal("cannot")])
    result = ask("drop it", db, profile, llm, settings)
    assert result["status"] == "refused"
    assert duckdb.connect(str(db), read_only=True).execute("SELECT count(*) FROM data").fetchone()[0] > 0


def test_ungrounded_narration_falls_back(retail_db, settings):
    db, profile = retail_db
    llm = FakeLLM([answerable("SELECT count(*) AS n FROM data")], narration="There are 987654321 rows.")
    result = ask("count", db, profile, llm, settings)
    assert result["answer_verified"] is False
    assert "987654321" not in result["answer"]


def test_empty_result_says_no_data(retail_db, settings):
    db, profile = retail_db
    llm = FakeLLM([answerable("SELECT * FROM data WHERE quantity > 1000000")])
    result = ask("huge orders", db, profile, llm, settings)
    assert result["rows"] == [] and "No matching data" in result["answer"]


def test_row_cap_truncates(retail_db, settings):
    db, _ = retail_db
    columns, rows, truncated = run_readonly(db, "SELECT * FROM data", settings)
    assert len(rows) == settings.max_rows and truncated


def test_file_access_blocked_at_connection_level(retail_db, settings):
    db, _ = retail_db  # bypasses the validator on purpose: second layer must hold alone
    with pytest.raises(duckdb.Error):
        run_readonly(db, "SELECT * FROM read_csv('/etc/hosts')", settings)


def test_writes_blocked_at_connection_level(retail_db, settings):
    db, _ = retail_db
    with pytest.raises(duckdb.Error):
        run_readonly(db, "DELETE FROM data", settings)


def test_timeout_surfaces_as_job_error(retail_db, settings):
    from dataclasses import replace

    db, profile = retail_db
    slow = "SELECT count(*) FROM data a, data b, data c, data d WHERE a.quantity + b.quantity + c.quantity + d.quantity = 99999"
    llm = FakeLLM([answerable(slow)])
    with pytest.raises(JobError) as exc:
        ask("slow", db, profile, llm, replace(settings, query_timeout_s=0.3))
    assert exc.value.code == "query_timeout"


@pytest.mark.parametrize(
    "text,rows,ok",
    [
        ("Revenue was $1,234.57.", [[1234.5678]], True),
        ("Revenue was about $1.2 million.", [[1234567.0]], True),
        ("About 34% of revenue.", [[0.34]], True),
        ("The top 3 are listed.", [["a", 1.0], ["b", 2.0], ["c", 3.0]], True),
        ("Revenue was $999.", [[1234.5]], False),
    ],
)
def test_numbers_grounded(text, rows, ok):
    assert numbers_grounded(text, rows) is ok
