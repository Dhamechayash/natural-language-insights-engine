"""Runtime settings, read from environment variables (and .env if present)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    anthropic_api_key: str
    anthropic_model: str
    workers: int
    max_upload_mb: int
    query_timeout_s: float
    max_rows: int
    max_queue: int
    max_attempts: int
    lease_s: int
    start_workers: bool
    anthropic_workspace_id: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def dataset_dir(self, dataset_id: str) -> Path:
        return self.data_dir / "datasets" / dataset_id


def get_settings() -> Settings:
    return Settings(
        data_dir=Path(os.getenv("DATA_DIR", "./data")).resolve(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_workspace_id=os.getenv("ANTHROPIC_WORKSPACE_ID", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        workers=int(os.getenv("WORKERS", "2")),
        max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "200")),
        query_timeout_s=float(os.getenv("QUERY_TIMEOUT_S", "30")),
        max_rows=int(os.getenv("MAX_ROWS", "1000")),
        max_queue=int(os.getenv("MAX_QUEUE", "100")),
        max_attempts=int(os.getenv("MAX_ATTEMPTS", "3")),
        lease_s=int(os.getenv("LEASE_S", "300")),
        start_workers=os.getenv("START_WORKERS", "1") == "1",
    )
