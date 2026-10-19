import shutil
from pathlib import Path

import pytest

from app.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path, anthropic_api_key="test", anthropic_model="test", workers=2, max_upload_mb=1,
        query_timeout_s=5, max_rows=100, max_queue=5, max_attempts=3, lease_s=60, start_workers=False,
    )  # fmt: skip


@pytest.fixture
def retail_db(tmp_path):
    """(db_file, profile) for the small retail fixture."""
    from app.ingest import ingest_csv

    profile = ingest_csv(FIXTURES / "retail_small.csv", tmp_path / "retail")
    return tmp_path / "retail" / "data.duckdb", profile


@pytest.fixture
def fixture_path():
    return lambda name: FIXTURES / name


__all__ = ["shutil"]
