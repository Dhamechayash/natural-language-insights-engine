"""The path of a question: profile -> LLM SQL -> validate -> run read-only -> grounded narration."""

import re
import threading
from pathlib import Path

import duckdb
from langsmith import traceable

from app.charts import recommend_chart
from app.config import Settings
from app.errors import JobError
from app.llm import LLM
from app.profiler import jsonable
from app.validator import SqlRejected, validate_sql

MAX_SQL_ATTEMPTS = 3
NARRATE_ROWS = 20


def profile_to_text(profile: dict) -> str:
    """Render the profile (plus any annotations) as compact prompt context."""
    lines = [f"Table `{profile['table']}`: {profile['row_count']} rows.", "Columns:"]
    for c in profile["columns"]:
        parts = [f"- {c['name']} ({c['type']}), nulls {c['null_pct']}%, distinct {c['distinct']}"]
        if "min" in c:
            parts.append(f"range {c['min']} .. {c['max']}")
        if "mean" in c and c["mean"] is not None:
            parts.append(f"mean {c['mean']}")
        if c.get("negative_pct"):
            parts.append(f"negative in {c['negative_pct']}% of rows")
        if c.get("zero_pct"):
            parts.append(f"zero in {c['zero_pct']}% of rows")
        if "values" in c:
            parts.append("values: " + ", ".join(f"{v['value']!r}({v['count']})" for v in c["values"]))
        elif "top_values" in c:
            parts.append("examples: " + ", ".join(repr(v["value"]) for v in c["top_values"]))
        if c.get("likely_identifier_or_free_text"):
            parts.append("high cardinality (identifier or free text)")
        if "parse_hint" in c:
            parts.append(c["parse_hint"])
        lines.append("; ".join(parts))
    lines.append(f"Sample rows: {profile['sample_rows']}")
    ann = profile.get("annotations")
    if ann:
        lines.append("Suggested interpretation (unverified hints; check against the data):")
        for col, role in ann.get("roles", {}).items():
            lines.append(f"- {col}: {role}")
        for m in ann.get("metrics", []):
            lines.append(f"- metric {m['name']} = {m['sql_expression']} [{m['confidence']}] {m['description']}")
        for note in ann.get("notes", []):
            lines.append(f"- note: {note}")
    return "\n".join(lines)


def run_readonly(db_file: Path, sql: str, settings: Settings) -> tuple[list[str], list[list], bool]:
    """Execute on a read-only connection with external file access disabled, a timeout and a row cap."""
    conn = duckdb.connect(str(db_file), read_only=True, config={"enable_external_access": False})
    timer = threading.Timer(settings.query_timeout_s, conn.interrupt)
    timer.start()
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description]
        fetched = cur.fetchmany(settings.max_rows + 1)
        truncated = len(fetched) > settings.max_rows
        rows = [[jsonable(v) for v in row] for row in fetched[: settings.max_rows]]
        return columns, rows, truncated
    finally:
        timer.cancel()
        conn.close()


def _numbers_in(text: str) -> list[float]:
    out = []
    for token in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            out.append(float(token.replace(",", "").rstrip(".")))
        except ValueError:
            pass
    return out


def numbers_grounded(answer: str, rows: list[list]) -> bool:
    """Every number in the narration must trace to a result value: rounded, as a %, in K/M/B, or a rank/row count."""
    values: list[float] = []
    for row in rows:
        for v in row:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values.append(float(v))
            elif isinstance(v, str):
                values.extend(_numbers_in(v))  # e.g. "2011-03" or a name containing digits
    for n in _numbers_in(answer):
        if n == int(n) and n <= len(rows):  # ranks and counts like "top 3" / "5 rows"
            continue
        if not any(_matches(n, v) for v in values):
            return False
    return True


def _matches(n: float, v: float) -> bool:
    for scale, tol in ((1, 0.005), (100, 0.005), (1e3, 0.05), (1e6, 0.05), (1e9, 0.05)):
        if abs(n - v / scale) <= max(0.01, abs(v / scale) * tol):
            return True
    return abs(n - v * 100) <= max(0.01, abs(v * 100) * 0.005)


def fallback_answer(columns: list[str], rows: list[list], truncated: bool) -> str:
    if not rows:
        return "No matching data was found."
    if len(rows) == 1:
        return "; ".join(f"{c}: {v}" for c, v in zip(columns, rows[0]))
    return f"Returned {len(rows)}{'+' if truncated else ''} rows; see the table."


@traceable(name="ask", run_type="chain", process_inputs=lambda i: {"question": i.get("question"), "history": i.get("history")})
def ask(question: str, db_file: Path, profile: dict, llm: LLM, settings: Settings, history: list[dict] | None = None) -> dict:
    """Returns {status: 'succeeded'|'refused', ...}. Raises JobError for infrastructure failures.

    `history` holds earlier turns of a follow-up thread (oldest first) so the question can refer back to them."""
    session = llm.new_sql_session(question, profile_to_text(profile), history or [])
    in_tok = out_tok = 0
    last_error = ""
    for attempt in range(1, MAX_SQL_ATTEMPTS + 1):
        proposal = session.propose()
        in_tok += proposal.input_tokens
        out_tok += proposal.output_tokens

        if not proposal.answerable:
            return {
                "status": "refused",
                "reason": proposal.reason or "The data cannot answer this question.",
                "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
            }
        try:
            sql = validate_sql(proposal.sql, profile["table"])
            columns, rows, truncated = run_readonly(db_file, sql, settings)
        except SqlRejected as exc:
            last_error = exc.message
        except duckdb.InterruptException as exc:
            raise JobError("query_timeout", f"The query exceeded {settings.query_timeout_s:g}s.") from exc
        except duckdb.Error as exc:
            last_error = str(exc).splitlines()[0]
        else:
            return _finish(proposal.resolved_question or question, llm, proposal, sql, columns, rows, truncated, in_tok, out_tok)
        if attempt < MAX_SQL_ATTEMPTS:
            session.report_error(last_error)

    return {
        "status": "refused",
        "reason": "Could not produce a valid query for this question from the available columns.",
        "detail": last_error,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


def _finish(question, llm, proposal, sql, columns, rows, truncated, in_tok, out_tok) -> dict:
    verified = False
    answer = fallback_answer(columns, rows, truncated)
    if rows:
        text, i, o = llm.narrate(question, columns, rows[:NARRATE_ROWS])
        in_tok, out_tok = in_tok + i, out_tok + o
        if text and numbers_grounded(text, rows[:NARRATE_ROWS]):
            answer, verified = text, True
    return {
        "status": "succeeded",
        "answer": answer,
        "answer_verified": verified,
        "resolved_question": question,
        "sql": sql,
        "assumptions": proposal.assumptions,
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
        "chart": recommend_chart(columns, rows, truncated),
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }
