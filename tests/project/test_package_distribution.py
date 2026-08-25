from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from agentcicd.sql.runtime import package_distribution
from agentcicd.sql.runtime.package_distribution import _local_package_roots, _package_source_root


def test_package_distribution_includes_consolidated_runtime_package() -> None:
    roots = dict(_local_package_roots())

    assert (roots["agentcicd"] / "agentcicd").is_dir()
    for root in roots.values():
        assert isinstance(root, Path)


def test_package_source_root_ignores_spark_pyfile_archives(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archive_path = tmp_path / "example_pkg_src.zip"
    archive_path.write_bytes(b"zip-placeholder")
    source_root = tmp_path / "src"
    package_dir = source_root / "example_pkg"
    package_dir.mkdir(parents=True)

    spec = importlib.machinery.ModuleSpec("example_pkg", loader=None, is_package=True)
    spec.submodule_search_locations = [
        str(archive_path / "example_pkg"),
        str(package_dir),
    ]

    def find_spec(name: str) -> importlib.machinery.ModuleSpec | None:
        if name == "example_pkg":
            return spec
        return importlib.util.find_spec(name)

    monkeypatch.setattr(package_distribution.importlib.util, "find_spec", find_spec)

    assert _package_source_root("example_pkg") == source_root.resolve()
