import duckdb
import pytest

from app.errors import JobError
from app.ingest import ingest_csv


def test_profile_of_retail_fixture(retail_db):
    _, profile = retail_db
    cols = {c["name"]: c for c in profile["columns"]}
    assert profile["row_count"] > 1000
    assert cols["quantity"]["negative_pct"] > 0  # returns/cancellations surfaced as a data-quality signal
    assert "TRY_STRPTIME" in cols["invoicedate"]["parse_hint"]
    assert cols["country"]["values"]  # low-cardinality column lists all values


def test_unseen_csv_different_delimiter_and_headers(tmp_path, fixture_path):
    profile = ingest_csv(fixture_path("orders_second.csv"), tmp_path / "ds")
    names = [c["name"] for c in profile["columns"]]
    assert "price_eur" in names and "order_id" in names  # headers normalised to SQL-safe names
    assert {c["name"]: c["type"] for c in profile["columns"]}["ordered_at"] == "DATE"


def test_latin1_and_bom_and_duplicate_headers(tmp_path):
    csv = tmp_path / "odd.csv"
    csv.write_bytes("﻿name,name,café\nJosé,1,2\n".encode("latin-1", errors="ignore").replace(b"\xef\xbb\xbf", b""))
    profile = ingest_csv(csv, tmp_path / "ds")
    assert len(profile["columns"]) == 3
    assert len({c["name"] for c in profile["columns"]}) == 3


def test_mixed_types_fall_back_to_text(tmp_path):
    csv = tmp_path / "mixed.csv"
    csv.write_text("id,val\n" + "\n".join(f"{i},{i}" for i in range(200)) + "\n201,not a number\n")
    profile = ingest_csv(csv, tmp_path / "ds")
    assert profile["row_count"] == 201


def test_all_null_column(tmp_path):
    csv = tmp_path / "nulls.csv"
    csv.write_text("a,b\n1,\n2,\n")
    profile = ingest_csv(csv, tmp_path / "ds")
    assert {c["name"]: c["null_pct"] for c in profile["columns"]}["b"] == 100.0


def test_header_only_is_rejected(tmp_path):
    csv = tmp_path / "empty.csv"
    csv.write_text("a,b,c\n")
    with pytest.raises(JobError) as exc:
        ingest_csv(csv, tmp_path / "ds")
    assert exc.value.code == "empty_csv"


def test_failed_ingest_leaves_no_queryable_file(tmp_path):
    csv = tmp_path / "empty.csv"
    csv.write_text("a,b,c\n")
    with pytest.raises(JobError):
        ingest_csv(csv, tmp_path / "ds")
    assert not (tmp_path / "ds" / "data.duckdb").exists()


def test_reingest_swaps_atomically(tmp_path, fixture_path):
    ingest_csv(fixture_path("orders_second.csv"), tmp_path / "ds")
    ingest_csv(fixture_path("retail_small.csv"), tmp_path / "ds")
    conn = duckdb.connect(str(tmp_path / "ds" / "data.duckdb"), read_only=True)
    assert "invoiceno" in [r[0] for r in conn.execute("DESCRIBE data").fetchall()]
