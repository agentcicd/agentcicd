from __future__ import annotations

import sys
from pathlib import Path


def pytest_configure() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    extra_paths = [
        repo_root,
        repo_root / "agentcicd" / "src",
        repo_root / "agentcicd_fixtures" / "src",
    ]
    for path in extra_paths:
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
