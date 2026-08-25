from __future__ import annotations

import sys
from pathlib import Path


def pytest_configure() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    src_path = repo_root / "agentcicd" / "src"
    compat_src_path = repo_root / "agentcicd_fixtures" / "src"

    for path in (src_path, compat_src_path):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)

    try:
        import agentcicd.fixtures.aisystem as package
    except Exception:
        return
    sys.modules.setdefault("agentcicd_fixtures_aisystem", package)

    try:
        import agentcicd.fixtures.core as core_package
    except Exception:
        return
    sys.modules.setdefault("agentcicd_fixtures_core", core_package)
