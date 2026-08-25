from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path


class SparkWorkerPackageDistributor:
    def __init__(self) -> None:
        self._distributed_context_ids: set[int] = set()

    def ensure_distributed(self, spark_session) -> None:
        if not hasattr(spark_session, "sparkContext"):
            return
        context_id = id(spark_session.sparkContext)
        if context_id in self._distributed_context_ids:
            return

        pyfiles_dir = Path(tempfile.gettempdir()) / f"agentcicd_ir_pyfiles_{context_id}"
        pyfiles_dir.mkdir(parents=True, exist_ok=True)
        for label, package_root in _local_package_roots():
            if not package_root.is_dir():
                continue
            archive_base = pyfiles_dir / f"{label}_src"
            archive_path = shutil.make_archive(
                str(archive_base),
                "zip",
                root_dir=str(package_root.parent),
                base_dir=package_root.name,
            )
            spark_session.sparkContext.addPyFile(archive_path)
        self._distributed_context_ids.add(context_id)


def _local_package_roots() -> list[tuple[str, Path]]:
    package_names = [
        "agentcicd",
        "agentcicd_fixtures",
        "agentcicd_sandbox",
        "agentcicd_eval_sql",
    ]
    roots: list[tuple[str, Path]] = []
    for package_name in package_names:
        root = _package_source_root(package_name)
        if root is not None:
            roots.append((package_name, root))
    return roots


def _package_source_root(package_name: str) -> Path | None:
    search_path = [
        entry
        for entry in sys.path
        if not entry.endswith(".zip") or Path(entry).exists()
    ]
    try:
        spec = importlib.util.find_spec(package_name)
    except (ImportError, OSError):
        try:
            spec = importlib.machinery.PathFinder.find_spec(package_name, search_path)
        except (ImportError, OSError):
            return None
    root = _source_root_from_spec(package_name, spec)
    if root is not None:
        return root
    try:
        fallback_spec = importlib.machinery.PathFinder.find_spec(package_name, search_path)
    except (ImportError, OSError):
        return None
    return _source_root_from_spec(package_name, fallback_spec)


def _source_root_from_spec(package_name: str, spec: importlib.machinery.ModuleSpec | None) -> Path | None:
    if spec is None:
        return None
    package_path = Path(*package_name.split("."))
    if spec.origin:
        origin_package_dir = Path(spec.origin).resolve().parent
        origin_package_root = origin_package_dir.parent
        if origin_package_root.is_dir() and (origin_package_root / package_path).is_dir():
            return origin_package_root
    if spec.submodule_search_locations is None:
        return None
    for raw_location in spec.submodule_search_locations:
        package_dir = Path(raw_location).resolve()
        package_root = package_dir.parent
        if package_root.is_dir() and (package_root / package_path).is_dir():
            return package_root
    return None
