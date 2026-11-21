from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "demo.sqlite"
MANAGER_DATABASE = PROJECT_ROOT / "data" / "manager.sqlite"


def database_path() -> Path:
    configured = os.environ.get("IIKO_INTELLIGENCE_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    return MANAGER_DATABASE if MANAGER_DATABASE.is_file() else DEFAULT_DATABASE
