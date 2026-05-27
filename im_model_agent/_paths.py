from __future__ import annotations

import sys
from pathlib import Path


def ensure_project_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    path = str(repo_root)
    if path not in sys.path:
        sys.path.insert(0, path)
