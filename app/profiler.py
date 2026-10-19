"""Builds the query context (schema profile) from the loaded table itself.

Nothing here knows what the data is about: it only measures columns.
"""

import datetime as dt
import decimal
from typing import Any

import duckdb

CATEGORICAL_MAX_DISTINCT = 30
TOP_K = 5
DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y", "%d %b %Y", "%b %d, %Y",
]  # fmt: skip
SAMPLE_ROWS = 3


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if value != value else round(value, 6)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    return str(value)


def _is_numeric(type_name: str) -> bool:
    t = type_name.upper()
    return t.startswith(("DECIMAL", "NUMERIC")) or t in {
        "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
        "UINTEGER", "UBIGINT", "FLOAT", "DOUBLE", "REAL",
    }  # fmt: skip


def _is_temporal(type_name: str) -> bool:
    return type_name.upper().startswith(("DATE", "TIMESTAMP"))


def profile_table(conn: duckdb.DuckDBPyConnection, table: str) -> dict:
    row_count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    described = conn.execute(f"DESCRIBE {table}").fetchall()
    columns = [_profile_column(conn, table, name, ctype, row_count) for name, ctype, *_ in described]
    sample_cur = conn.execute(f"SELECT * FROM {table} LIMIT {SAMPLE_ROWS}")
    names = [d[0] for d in sample_cur.description]
    sample = [{n: jsonable(v) for n, v in zip(names, row)} for row in sample_cur.fetchall()]
    return {"table": table, "row_count": row_count, "columns": columns, "sample_rows": sample}


def _profile_column(conn, table: str, name: str, ctype: str, row_count: int) -> dict:
    col = _q(name)
    nulls, distinct = conn.execute(f"SELECT count(*) - count({col}), count(DISTINCT {col}) FROM {table}").fetchone()
    non_null = row_count - nulls
    info: dict[str, Any] = {
        "name": name,
        "type": ctype,
        "null_pct": round(100 * nulls / row_count, 2) if row_count else 0.0,
        "distinct": distinct,
    }

    if _is_numeric(ctype):
        mn, mx, avg, neg, zero = conn.execute(
            f"SELECT min({col}), max({col}), avg({col}), "
            f"count(*) FILTER (WHERE {col} < 0), count(*) FILTER (WHERE {col} = 0) FROM {table}"
        ).fetchone()
        info.update(min=jsonable(mn), max=jsonable(mx), mean=jsonable(avg))
        if non_null:
            info["negative_pct"] = round(100 * neg / non_null, 2)
            info["zero_pct"] = round(100 * zero / non_null, 2)
    elif _is_temporal(ctype):
        mn, mx = conn.execute(f"SELECT min({col}), max({col}) FROM {table}").fetchone()
        info.update(min=jsonable(mn), max=jsonable(mx))
    else:
        mn, mx = conn.execute(f"SELECT min(length({col})), max(length({col})) FROM {table}").fetchone()
        info.update(min_length=mn, max_length=mx)
        # A text column that is really numbers/dates (e.g. when typing fell back to all-varchar).
        if non_null:
            hint = _parse_hint(conn, table, col, non_null)
            if hint:
                info["parse_hint"] = hint
    if non_null:
        if distinct / non_null > 0.9 and distinct > 50:
            info["likely_identifier_or_free_text"] = True
        limit = CATEGORICAL_MAX_DISTINCT if distinct <= CATEGORICAL_MAX_DISTINCT else TOP_K
        top = conn.execute(
            f"SELECT {col}, count(*) c FROM {table} WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY c DESC, 1 LIMIT {limit}"
        ).fetchall()
        key = "values" if distinct <= CATEGORICAL_MAX_DISTINCT else "top_values"
        info[key] = [{"value": jsonable(v), "count": c} for v, c in top]
    return info


def _parse_hint(conn, table: str, col: str, non_null: int) -> str | None:
    """For text columns that are really numbers or dates, say how to cast them."""
    (num_ok,) = conn.execute(f"SELECT count(try_cast(replace({col}, ',', '') AS DOUBLE)) FROM {table}").fetchone()
    if num_ok / non_null > 0.95:
        bad = conn.execute(
            f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL AND try_cast(replace({col}, ',', '') AS DOUBLE) IS NULL LIMIT 3"
        ).fetchall()
        hint = "text that mostly looks numeric (TRY_CAST(REPLACE(col, ',', '') AS DOUBLE) works for measures)"
        if bad:
            hint += f"; but some values are not numeric, e.g. {[b[0] for b in bad]}; keep as text if it is an identifier or code"
        return hint
    (ts_ok,) = conn.execute(f"SELECT count(try_cast({col} AS TIMESTAMP)) FROM {table}").fetchone()
    if ts_ok / non_null > 0.95:
        return "text that looks like timestamps; use TRY_CAST(col AS TIMESTAMP)"
    selects = ", ".join(f"count(try_strptime({col}, '{fmt}'))" for fmt in DATE_FORMATS)
    counts = conn.execute(f"SELECT {selects} FROM {table}").fetchone()
    good = [fmt for fmt, n in zip(DATE_FORMATS, counts) if n / non_null > 0.95]
    if not good:
        return None
    if len(good) == 1:
        return f"text dates in format '{good[0]}'; use TRY_STRPTIME(col, '{good[0]}')"
    return f"text dates, ambiguous between formats {good} (day/month order unclear); pick one and state it in assumptions"
