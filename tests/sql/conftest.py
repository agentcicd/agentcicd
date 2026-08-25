from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


def _alias_module(alias: str, target: str) -> None:
    if alias in sys.modules:
        return
    module = importlib.import_module(target)
    sys.modules[alias] = module


def pytest_configure() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    extra_paths = [
        repo_root,
        repo_root / "agentcicd" / "src",
    ]
    for path in extra_paths:
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)

    _alias_module("agentcicd_sql", "agentcicd.sql")


@pytest.fixture(autouse=True)
def _remove_deleted_spark_pyfiles():
    _prune_missing_zip_paths()
    yield
    _prune_missing_zip_paths()


def _prune_missing_zip_paths() -> None:
    sys.path[:] = [
        entry
        for entry in sys.path
        if not entry.endswith(".zip") or Path(entry).exists()
    ]
