"""CSV -> DuckDB. Repeatable, atomic: the dataset file only appears once fully loaded."""

import os
from pathlib import Path

import duckdb

from app.errors import JobError
from app.profiler import profile_table

TABLE = "data"

# Tried in order. The first that loads wins; later ones are progressively more forgiving.
_ATTEMPTS = [
    {"label": "auto"},
    {"label": "latin-1", "encoding": "latin-1"},
    {"label": "all-varchar", "all_varchar": True},
    {"label": "all-varchar-latin-1", "all_varchar": True, "encoding": "latin-1"},
    {"label": "all-varchar-lenient", "all_varchar": True, "encoding": "latin-1", "ignore_errors": True},
]


def _load(conn: duckdb.DuckDBPyConnection, csv_path: Path, opts: dict) -> None:
    args = ["?", "header=true", "normalize_names=true", "sample_size=100000"]
    for key in ("encoding", "all_varchar", "ignore_errors"):
        if key in opts:
            value = opts[key]
            args.append(f"{key}={'true' if value is True else repr(value)}")
    conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    conn.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM read_csv({', '.join(args)})", [str(csv_path)])


def ingest_csv(csv_path: Path, dest_dir: Path) -> dict:
    """Load `csv_path` into `dest_dir/data.duckdb` and return its schema profile."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / "data.duckdb.tmp"
    final = dest_dir / "data.duckdb"
    for stale in (tmp, Path(str(tmp) + ".wal")):
        stale.unlink(missing_ok=True)

    conn = duckdb.connect(str(tmp))
    try:
        last_error: Exception | None = None
        loaded = None
        for opts in _ATTEMPTS:
            try:
                _load(conn, csv_path, opts)
                loaded = opts["label"]
                break
            except duckdb.Error as exc:
                last_error = exc
        if loaded is None:
            raise JobError("csv_unreadable", f"Could not parse the file as CSV: {_first_line(last_error)}")

        rows = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        if rows == 0:
            raise JobError("empty_csv", "The CSV has a header but no data rows.")
        profile = profile_table(conn, TABLE)
        profile["load_strategy"] = loaded
        conn.execute("CHECKPOINT")
    finally:
        conn.close()

    os.replace(tmp, final)  # atomic swap; readers of an older file keep working
    Path(str(tmp) + ".wal").unlink(missing_ok=True)
    return profile


def _first_line(exc: Exception | None) -> str:
    return str(exc).splitlines()[0] if exc else "unknown error"
