"""LLM access behind a small interface so the engine can be tested with a scripted fake."""

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

from app.config import Settings
from app.errors import JobError

log = logging.getLogger("llm")

SQL_SYSTEM = """You turn business questions into ONE read-only DuckDB SQL query over a single table named `data`.

Rules:
- Use only columns listed in the schema profile. Never invent columns, tables or values.
- If the question cannot be answered from these columns (missing concept, e.g. no cost column for "profit", no \
forecasting, no external data), set answerable=false and say exactly what is missing. Refusing is correct and preferred over guessing.
- Pay attention to data-quality notes (negative values, nulls, cancellations/returns). State how you treated them in `assumptions`.
- Revenue-like measures usually need a derivation such as quantity * unit price; state the formula you chose in `assumptions`.
- Text columns with a parse hint must be cast (TRY_CAST) before use.
- Output a single SELECT (CTEs allowed). No DDL/DML, no file access, no semicolon-separated statements.
- Prefer clear column aliases. Add ORDER BY when the question implies ranking. Do not add LIMIT unless the question asks for top-N.
- The question may follow up on earlier turns ("those products", "and in France?", "break that down by month").
Resolve such references from the earlier turns, then write one standalone query; earlier SQL is a starting point, not a table you can select from.
- Always respond by calling the `submit_query` tool exactly once, including when the question is unanswerable."""

NARRATE_SYSTEM = """You write a 1-3 sentence answer to a business question from the query result rows provided.
Use only numbers that appear in the rows (you may round and format). Do not add outside facts, causes or recommendations.
If the rows are empty, say no matching data was found."""

ANNOTATE_SYSTEM = """You inspect the schema profile of an unfamiliar transactional CSV and describe how to interpret it.
Be conservative: only claim a role when the column names and values clearly support it. Assign confidence honestly.
Always respond by calling the `annotate_schema` tool exactly once."""

SUBMIT_QUERY_TOOL = {
    "name": "submit_query",
    "description": "Submit the SQL for the question, or declare it unanswerable.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "resolved_question": {
                "type": "string",
                "description": "The question rewritten to stand alone, with references to earlier turns spelled out. "
                "Repeat it unchanged if it already stands alone.",
            },
            "answerable": {"type": "boolean"},
            "reason": {"type": "string", "description": "If not answerable: what is missing. Otherwise a one-line approach."},
            "sql": {"type": "string", "description": "Single DuckDB SELECT. Empty if not answerable."},
            "assumptions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["resolved_question", "answerable", "reason", "sql", "assumptions"],
        "additionalProperties": False,
    },
}

ANNOTATE_TOOL = {
    "name": "annotate_schema",
    "description": "Describe column roles and candidate metric definitions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "roles": {
                "type": "object",
                "description": "column name -> one of: timestamp, quantity, unit_price, amount, customer_id, order_id, product_id, "
                "product_name, category, country_or_region, other",
                "additionalProperties": {"type": "string"},
            },
            "metrics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "sql_expression": {"type": "string"},
                        "description": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["name", "sql_expression", "description", "confidence"],
                },
            },
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["roles", "metrics", "notes"],
    },
}


def history_to_text(history: list[dict]) -> str:
    """Render earlier turns of a follow-up thread as prompt context."""
    lines = ["Earlier turns in this conversation (oldest first):"]
    for i, turn in enumerate(history, 1):
        lines.append(f"{i}. Question: {turn['question']}")
        if "refused" in turn:
            lines.append(f"   Not answerable: {turn['refused']}")
        elif turn.get("sql"):
            lines.append(f"   SQL: {turn['sql']}")
            lines.append(f"   Answer: {turn.get('answer', '')}")
            if turn.get("rows"):
                lines.append(f"   First rows {turn['columns']}: {json.dumps(turn['rows'], default=str)}")
    return "\n".join(lines)


@dataclass
class Proposal:
    answerable: bool
    reason: str
    sql: str
    assumptions: list[str] = field(default_factory=list)
    resolved_question: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class SqlSession(Protocol):
    def propose(self) -> Proposal: ...
    def report_error(self, message: str) -> None: ...


class LLM(Protocol):
    def new_sql_session(self, question: str, profile_text: str, history: list[dict]) -> SqlSession: ...
    def narrate(self, question: str, columns: list[str], rows: list[list]) -> tuple[str, int, int]: ...
    def annotate(self, profile_text: str) -> dict: ...


class AnthropicLLM:
    def __init__(self, settings: Settings):
        if not settings.anthropic_api_key:
            raise JobError("llm_not_configured", "ANTHROPIC_API_KEY is not set on the server.")
        import anthropic

        self._anthropic = anthropic
        # Keys not scoped to a workspace must name one on every request.
        headers = {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key, default_headers=headers, timeout=60.0, max_retries=2
        )
        # Records every call (prompts, tokens, latency) in LangSmith; a no-op unless LANGSMITH_TRACING=true.
        from langsmith.wrappers import wrap_anthropic

        self._client = wrap_anthropic(self._client)
        self.model = settings.anthropic_model

    def _call(self, **kwargs):
        try:
            return self._client.messages.create(model=self.model, **kwargs)
        except (self._anthropic.RateLimitError, self._anthropic.APIConnectionError, self._anthropic.InternalServerError) as exc:
            log.warning("llm unavailable: %s: %s", type(exc).__name__, exc)
            raise JobError("llm_unavailable", "The language model is temporarily unavailable.", retryable=True) from exc
        except self._anthropic.AuthenticationError as exc:
            log.warning("llm auth failed: %s: %s", type(exc).__name__, exc)
            raise JobError("llm_auth_failed", "The server's LLM credentials were rejected.") from exc
        except self._anthropic.APIError as exc:
            log.warning("llm request failed (model=%s): %s: %s", self.model, type(exc).__name__, exc)
            raise JobError("llm_error", "The language model request failed.") from exc

    def new_sql_session(self, question: str, profile_text: str, history: list[dict]) -> SqlSession:
        return _AnthropicSqlSession(self, question, profile_text, history)

    def narrate(self, question, columns, rows):
        payload = json.dumps({"question": question, "columns": columns, "rows": rows}, default=str)
        resp = self._call(max_tokens=400, system=NARRATE_SYSTEM, messages=[{"role": "user", "content": payload}])
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text, resp.usage.input_tokens, resp.usage.output_tokens

    def annotate(self, profile_text: str) -> dict:
        resp = self._call(
            max_tokens=2000,
            system=ANNOTATE_SYSTEM,
            messages=[{"role": "user", "content": profile_text}],
            tools=[ANNOTATE_TOOL],
            # Forced tool_choice is rejected by current models; the system prompt asks for the tool instead.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        )
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        return {}


class _AnthropicSqlSession:
    def __init__(self, llm: AnthropicLLM, question: str, profile_text: str, history: list[dict]):
        self._llm = llm
        earlier = f"{history_to_text(history)}\n\n" if history else ""
        self._messages: list[dict] = [
            {"role": "user", "content": f"Schema profile:\n{profile_text}\n\n{earlier}Question: {question}"}
        ]
        self._last_tool_id: str | None = None

    def propose(self) -> Proposal:
        resp = self._llm._call(
            max_tokens=1500,
            system=SQL_SYSTEM,
            messages=self._messages,
            tools=[SUBMIT_QUERY_TOOL],
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        )
        block = next((b for b in resp.content if b.type == "tool_use"), None)
        if block is None:
            raise JobError("llm_bad_output", "The language model returned no structured answer.", retryable=True)
        self._last_tool_id = block.id
        self._messages.append(
            {"role": "assistant", "content": [{"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}]}
        )
        data = block.input
        return Proposal(
            answerable=bool(data.get("answerable")),
            reason=str(data.get("reason", "")),
            sql=str(data.get("sql", "")),
            assumptions=[str(a) for a in data.get("assumptions", [])],
            resolved_question=str(data.get("resolved_question", "")),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

    def report_error(self, message: str) -> None:
        self._messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": self._last_tool_id,
                        "is_error": True,
                        "content": f"That query failed: {message}\nFix it, or set answerable=false if the data cannot support the question.",
                    }
                ],
            }
        )


def get_llm(settings: Settings) -> LLM:
    return AnthropicLLM(settings)
