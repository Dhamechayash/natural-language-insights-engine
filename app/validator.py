"""Static checks on LLM-written SQL. This is the first of two layers; the second is a
read-only DuckDB connection with external access disabled (see engine.py)."""

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

DENIED_FUNCTION_PREFIXES = ("read_", "duckdb_", "pragma_", "sniff_")
DENIED_FUNCTION_SUFFIXES = ("_scan",)
DENIED_FUNCTIONS = {"glob", "query", "query_table", "getenv", "current_setting", "load_extension", "install"}


class SqlRejected(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def validate_sql(sql: str, allowed_table: str = "data") -> str:
    """Return the cleaned SQL if it is a single read-only SELECT over `allowed_table`."""
    cleaned = (sql or "").strip().rstrip(";").strip()
    if not cleaned:
        raise SqlRejected("Empty SQL.")
    try:
        statements = [s for s in sqlglot.parse(cleaned, dialect="duckdb") if s is not None]
    except SqlglotError as exc:
        raise SqlRejected(f"SQL does not parse: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise SqlRejected("Exactly one statement is allowed.")
    root = statements[0]
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise SqlRejected("Only SELECT queries are allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlRejected("Table functions are not allowed.")
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlRejected("Qualified table names are not allowed; use the table name only.")
        name = table.name.lower()
        if name != allowed_table.lower() and name not in cte_names:
            raise SqlRejected(f"Unknown table '{table.name}'. The only table is '{allowed_table}'.")

    for func in root.find_all(exp.Func):
        name = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if name in DENIED_FUNCTIONS or name.startswith(DENIED_FUNCTION_PREFIXES) or name.endswith(DENIED_FUNCTION_SUFFIXES):
            raise SqlRejected(f"Function '{name}' is not allowed.")
    return cleaned
