from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_offline_migration_accepts_percent_encoded_database_url() -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = "postgresql://user:p%40ss@localhost/db"

    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head", "--sql"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
