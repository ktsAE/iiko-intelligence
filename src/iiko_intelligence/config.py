from __future__ import annotations

import os
import re
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[2]
# Installed releases all use the original installation's DB, DPAPI profile, reports and Codex settings.
PROJECT_ROOT = (CODE_ROOT.parent.parent if CODE_ROOT.parent.name == ".iiko-updates"
                and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", CODE_ROOT.name) else CODE_ROOT)
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "demo.sqlite"
MANAGER_DATABASE = PROJECT_ROOT / "data" / "manager.sqlite"


def database_path() -> Path:
    configured = os.environ.get("IIKO_INTELLIGENCE_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    return MANAGER_DATABASE if MANAGER_DATABASE.is_file() else DEFAULT_DATABASE
