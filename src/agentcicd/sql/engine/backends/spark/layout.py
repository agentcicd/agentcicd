from __future__ import annotations

import os
from urllib.parse import urlparse

from agentcicd.sql.engine.interfaces import BackendLayout


SparkBackendPaths = BackendLayout


def join_path(root: str, *parts: str) -> str:
    if urlparse(root).scheme:
        return "/".join([root.rstrip("/"), *(part.strip("/") for part in parts)])
    return os.path.join(root, *parts)


def is_uri_path(path: str) -> bool:
    return bool(urlparse(path).scheme)


def default_backend_paths(
    working_dir: str,
    *,
    tables_root: str | None = None,
    checkpoints_root: str | None = None,
) -> SparkBackendPaths:
    return SparkBackendPaths(
        working_dir=working_dir,
        tables_root=tables_root or os.path.join(working_dir, "tables"),
        sources_root=os.path.join(working_dir, "sources"),
        outputs_root=os.path.join(working_dir, "outputs"),
        publish_root=os.path.join(working_dir, "published"),
        checkpoints_root=checkpoints_root or os.path.join(working_dir, "checkpoints"),
        stream_batches_root=os.path.join(working_dir, "stream_batches"),
        http_cache_root=os.path.join(working_dir, "http_cache"),
        annotation_tasks_root=os.path.join(working_dir, "annotation_tasks"),
    )
