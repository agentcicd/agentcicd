from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatch
import hashlib
import importlib
import json
import mimetypes
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Tuple
from urllib.parse import urlparse

from agentcicd.fixtures.core.function import RowFunction
from agentcicd.fixtures.core.tracing import runtime_trace_span
from agentcicd.fixtures.core.types import ArrayType, BooleanType, DType, FType, JsonType, StringType
from agentcicd.fixtures.core.udf import Udf
from agentcicd.fixtures.environments.core.errors import ActionFailed, PolicyViolation
from agentcicd.fixtures.types import MaterializedDirectory

try:
    from agentcicd_dp_common.object_store import object_store_from_env
except ImportError:  # pragma: no cover - optional outside DP runtime images
    object_store_from_env = None  # type: ignore[assignment]
_IMPORTED_OBJECT_STORE_FACTORY = object_store_from_env


MAX_INLINE_READ_BYTES = 30 * 1024 * 1024
FILESYSTEM_DATASET_FORMATS = {"directory", "filesystem", "filesystem_dataset"}
DIRECTORY_ENTRY_SCHEMA_VERSION = "agentcicd.directory.entry.v1"


@dataclass(frozen=True)
class DirectoryEntry:
    path: str
    name: str
    parent_path: str | None
    entry_type: str
    size_bytes: int | None
    content_type: str | None
    sha256: str | None
    object_uri: str | None
    is_empty_dir: bool


async def materialize(tree: object, target_dir: str | os.PathLike[str] = ".") -> Any:
    with runtime_trace_span("objectstore.materialize", {"method": "materialize"}):
        entries = _coerce_tree_entries(tree)
        if not entries:
            raise ActionFailed("tree must contain directory entries")
        target = _local_path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        store = _require_object_store()
        materialized_entries: list[dict[str, Any]] = []
        for entry in entries:
            relative_path = str(entry.get("path") or entry.get("dataset_path") or "").strip()
            if not relative_path:
                continue
            normalized_relative = _normalize_tree_path(relative_path)
            target_path = target / PurePosixPath(normalized_relative)
            entry_type = str(entry.get("entry_type") or entry.get("type") or "").strip().lower()
            if entry_type == "directory" or bool(entry.get("is_empty_dir")):
                target_path.mkdir(parents=True, exist_ok=True)
                materialized_entries.append(_directory_entry(normalized_relative, entry_type="directory", is_empty_dir=True))
                continue
            object_uri = str(entry.get("object_uri") or entry.get("uri") or "").strip()
            if not object_uri:
                continue
            payload = store.get_bytes(object_uri)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(payload)
            content_type = mimetypes.guess_type(normalized_relative)[0] or "application/octet-stream"
            materialized_entries.append(
                _directory_entry(
                    normalized_relative,
                    entry_type="file",
                    size_bytes=len(payload),
                    content_type=content_type,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    object_uri=object_uri,
                    is_empty_dir=False,
                )
            )
        return MaterializedDirectory(
            root=str(target.resolve()),
            target_dir=str(target),
            target_path=str(target.resolve()),
            entries=materialized_entries,
        )


async def upload(
    path: str | os.PathLike[str],
    artifact_path: str | None = None,
    content_type: str | None = None,
) -> list[dict[str, object]]:
    with runtime_trace_span("objectstore.upload", {"method": "upload"}):
        source = _local_path(path)
        if source.is_symlink():
            raise PolicyViolation(f"refusing to upload symlink: {source}")
        if source.is_file():
            return [upload_file(source, artifact_path or source.name, content_type)]
        if source.is_dir():
            return upload_all(source, artifact_path)
        raise ActionFailed(f"path does not exist: {source}")


async def download(entries: object, local_path: str | os.PathLike[str] | None = None) -> str:
    with runtime_trace_span("objectstore.download", {"method": "download"}):
        return download_all(entries, local_path or ".")


def is_directory_format(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in FILESYSTEM_DATASET_FORMATS


def normalize_filter_patterns(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    stripped = str(value).strip()
    return (stripped,) if stripped else ()


def compile_filter_patterns(patterns: tuple[str, ...], label: str) -> tuple[Any, ...]:
    import re

    compiled: list[Any] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ValueError(f"Invalid regex in {label}: {pattern}: {exc}") from exc
    return tuple(compiled)


def _runtime_metadata(*, sql_enabled: bool) -> dict[str, object]:
    placements = ["local_python"]
    if sql_enabled:
        placements.append("spark_executor")
    metadata: dict[str, object] = {
        "execution_runtime": "local_python",
        "placements": placements,
        "default_python_placement": "local_python",
        "requires": ["run_object_store", "local_filesystem"],
        "sql_enabled": sql_enabled,
    }
    if sql_enabled:
        metadata["default_sql_placement"] = "spark_executor"
    return metadata


def _sql_runtime_metadata() -> dict[str, object]:
    return _runtime_metadata(sql_enabled=True)


def _python_runtime_metadata() -> dict[str, object]:
    return _runtime_metadata(sql_enabled=False)


def entry(
    dataset_path: str,
    name: str | None = None,
    parent_path: str | None = None,
    entry_type: str = "file",
    size_bytes: int | None = None,
    content_type: str | None = None,
    sha256: str | None = None,
    object_uri: str | None = None,
    is_empty_dir: bool = False,
) -> dict[str, Any]:
    with runtime_trace_span("objectstore.entry", {"path": dataset_path, "entry_type": entry_type}):
        return _directory_entry(
            dataset_path,
            name=name,
            parent_path=parent_path,
            entry_type=entry_type,
            size_bytes=size_bytes,
            content_type=content_type,
            sha256=sha256,
            object_uri=object_uri,
            is_empty_dir=is_empty_dir,
        )


def exists(entries: Any, path: str) -> bool:
    with runtime_trace_span("objectstore.exists", {"path": path}):
        return _find_entry(entries, _normalize_tree_path(path)) is not None


def find(entries: Any, path: str) -> dict[str, Any] | None:
    with runtime_trace_span("objectstore.find", {"path": path}):
        found = _find_entry(entries, _normalize_tree_path(path))
        return _entry_to_mapping(found) if found is not None else None


def glob(entries: Any, pattern: str) -> list[dict[str, Any]]:
    with runtime_trace_span("objectstore.glob", {"pattern": pattern}):
        normalized_pattern = _normalize_workspace_path(pattern)
        if normalized_pattern == ".":
            normalized_pattern = "*"
        return [
            _entry_to_mapping(item)
            for item in _coerce_directory_entries(entries)
            if fnmatch(item.path, normalized_pattern)
        ]


def read_text(entries: Any, path: str) -> str | None:
    with runtime_trace_span("objectstore.read_text", {"path": path}):
        file_entry = _require_file_entry(entries, path)
        if not file_entry.object_uri:
            raise FileNotFoundError(path)
        if file_entry.size_bytes is not None and file_entry.size_bytes > MAX_INLINE_READ_BYTES:
            raise ValueError(f"File '{path}' is too large for inline text reads")
        payload = _read_object_uri(file_entry.object_uri)
        if len(payload) > MAX_INLINE_READ_BYTES:
            raise ValueError(f"File '{path}' is too large for inline text reads")
        return payload.decode("utf-8")


def read_json(entries: Any, path: str) -> str | None:
    with runtime_trace_span("objectstore.read_json", {"path": path}):
        text = read_text(entries, path)
        if text is None:
            return None
        return json.dumps(json.loads(text), separators=(",", ":"), sort_keys=True)


def write_text(path: str, content: str, content_type: str | None = None) -> list[dict[str, Any]]:
    with runtime_trace_span("objectstore.write_text", {"path": path, "content_type": content_type}):
        normalized = _normalize_tree_path(path)
        payload = str(content).encode("utf-8")
        return [_put_run_artifact(normalized, payload, content_type or _guess_content_type(normalized))]


def write_json(path: str, value: Any) -> list[dict[str, Any]]:
    with runtime_trace_span("objectstore.write_json", {"path": path}):
        normalized = _normalize_tree_path(path)
        parsed = json.loads(value) if isinstance(value, str) else value
        payload = json.dumps(parsed, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return [_put_run_artifact(normalized, payload, "application/json")]


def upload_file(local_path: str | os.PathLike[str], artifact_path: str | None = None, content_type: str | None = None) -> dict[str, Any]:
    source = _local_path(local_path)
    if source.is_symlink():
        raise ValueError(f"Refusing to upload symlink: {local_path}")
    if not source.is_file():
        raise FileNotFoundError(str(local_path))
    normalized_artifact_path = _normalize_tree_path(artifact_path or source.name)
    return _put_run_artifact(
        normalized_artifact_path,
        source.read_bytes(),
        content_type or _guess_content_type(normalized_artifact_path),
    )


def upload_all(local_root: str | os.PathLike[str], artifact_prefix: str | None = None) -> list[dict[str, Any]]:
    root = _local_path(local_root)
    if root.is_symlink():
        raise ValueError(f"Refusing to upload symlink root: {local_root}")
    if not root.is_dir():
        raise NotADirectoryError(str(local_root))
    prefix = _normalize_workspace_path(artifact_prefix or root.name)
    entries: list[dict[str, Any]] = [
        _directory_entry(prefix if prefix != "." else root.name, entry_type="directory", is_empty_dir=not any(root.iterdir()))
    ]
    seen: set[str] = {str(entries[0]["path"])}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Refusing to upload symlink: {path}")
        relative = path.relative_to(root).as_posix()
        artifact_path = relative if prefix == "." else f"{prefix}/{relative}"
        normalized = _normalize_tree_path(artifact_path)
        if normalized in seen:
            raise ValueError(f"Duplicate artifact path: {normalized}")
        seen.add(normalized)
        if path.is_dir():
            entries.append(_directory_entry(normalized, entry_type="directory", is_empty_dir=not any(path.iterdir())))
        elif path.is_file():
            entries.append(upload_file(str(path), normalized, None))
        else:
            raise ValueError(f"Unsupported filesystem entry: {path}")
    return entries


def download_file(entry_value: Any, local_path: str | os.PathLike[str]) -> str:
    target = _local_path(local_path)
    file_entry = _entry_from_value_or_uri(entry_value)
    if file_entry.entry_type != "file":
        raise IsADirectoryError(file_entry.path)
    if not file_entry.object_uri:
        raise FileNotFoundError(file_entry.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_read_object_uri(file_entry.object_uri))
    return str(target)


def download_all(entries: Any, local_root: str | os.PathLike[str]) -> str:
    root = _local_path(local_root)
    for item in _coerce_directory_entries(entries):
        target = root / item.path
        if item.entry_type == "directory":
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not item.object_uri:
            raise FileNotFoundError(item.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_read_object_uri(item.object_uri))
    return str(root)


class ObjectstoreEntryFunction(RowFunction):
    def transform(
        self,
        dataset_path: str,
        name: str | None = None,
        parent_path: str | None = None,
        entry_type: str = "file",
        size_bytes: int | None = None,
        content_type: str | None = None,
        sha256: str | None = None,
        object_uri: str | None = None,
        is_empty_dir: bool = False,
    ) -> dict[str, Any]:
        return entry(
            dataset_path=dataset_path,
            name=name,
            parent_path=parent_path,
            entry_type=entry_type,
            size_bytes=size_bytes,
            content_type=content_type,
            sha256=sha256,
            object_uri=object_uri,
            is_empty_dir=is_empty_dir,
        )


class ObjectstoreUploadFunction(RowFunction):
    def transform(
        self,
        local_path: str,
        artifact_path: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        return upload_file(local_path, artifact_path, content_type)


class ObjectstoreUploadAllFunction(RowFunction):
    def transform(self, local_root: str, artifact_prefix: str | None = None) -> list[dict[str, Any]]:
        return upload_all(local_root, artifact_prefix)


class ObjectstoreDownloadFunction(RowFunction):
    def transform(self, entry_value: Any, local_path: str) -> str:
        return download_file(entry_value, local_path)


class ObjectstoreDownloadAllFunction(RowFunction):
    def transform(self, entries: Any, local_root: str) -> str:
        return download_all(entries, local_root)


class ObjectstoreExistsFunction(RowFunction):
    def transform(self, entries: Any, path: str) -> bool:
        return exists(entries, path)


class ObjectstoreFindFunction(RowFunction):
    def transform(self, entries: Any, path: str) -> dict[str, Any] | None:
        return find(entries, path)


class ObjectstoreGlobFunction(RowFunction):
    def transform(self, entries: Any, pattern: str) -> list[dict[str, Any]]:
        return glob(entries, pattern)


class ObjectstoreReadTextFunction(RowFunction):
    def transform(self, entries: Any, path: str) -> str | None:
        return read_text(entries, path)


class ObjectstoreReadJsonFunction(RowFunction):
    def transform(self, entries: Any, path: str) -> str | None:
        return read_json(entries, path)


class ObjectstoreWriteTextFunction(RowFunction):
    def transform(self, path: str, content: str, content_type: str | None = None) -> list[dict[str, Any]]:
        return write_text(path, content, content_type)


class ObjectstoreWriteJsonFunction(RowFunction):
    def transform(self, path: str, value: Any) -> list[dict[str, Any]]:
        return write_json(path, value)


class ObjectstoreEntryUdf(Udf, name="objectstore.entry"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (
            StringType(),
            StringType(),
            StringType(),
            StringType(),
            JsonType(),
            StringType(),
            StringType(),
            StringType(),
            JsonType(),
        )

    def input_args(self) -> Tuple[str, ...]:
        return (
            "dataset_path",
            "name",
            "parent_path",
            "entry_type",
            "size_bytes",
            "content_type",
            "sha256",
            "object_uri",
            "is_empty_dir",
        )

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreEntryFunction

    def metadata(self) -> dict[str, object]:
        return _sql_runtime_metadata()


class ObjectstoreUploadUdf(Udf, name="objectstore.upload"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("local_path", "artifact_path", "content_type")

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreUploadFunction

    def metadata(self) -> dict[str, object]:
        return _python_runtime_metadata()


class ObjectstoreUploadAllUdf(Udf, name="objectstore.upload_all"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("local_root", "artifact_prefix")

    def output_schema(self) -> DType:
        return ArrayType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreUploadAllFunction

    def metadata(self) -> dict[str, object]:
        return _python_runtime_metadata()


class ObjectstoreDownloadUdf(Udf, name="objectstore.download"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (JsonType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("entry", "local_path")

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreDownloadFunction

    def metadata(self) -> dict[str, object]:
        return _python_runtime_metadata()


class ObjectstoreDownloadAllUdf(Udf, name="objectstore.download_all"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (ArrayType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("entries", "local_root")

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreDownloadAllFunction

    def metadata(self) -> dict[str, object]:
        return _python_runtime_metadata()


class ObjectstoreExistsUdf(Udf, name="objectstore.exists"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (ArrayType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("entries", "path")

    def output_schema(self) -> DType:
        return BooleanType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreExistsFunction

    def metadata(self) -> dict[str, object]:
        return _sql_runtime_metadata()


class ObjectstoreFindUdf(Udf, name="objectstore.find"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (ArrayType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("entries", "path")

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreFindFunction

    def metadata(self) -> dict[str, object]:
        return _sql_runtime_metadata()


class ObjectstoreGlobUdf(Udf, name="objectstore.glob"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (ArrayType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("entries", "pattern")

    def output_schema(self) -> DType:
        return ArrayType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreGlobFunction

    def metadata(self) -> dict[str, object]:
        return _sql_runtime_metadata()


class ObjectstoreReadTextUdf(Udf, name="objectstore.read_text"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (ArrayType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("entries", "path")

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreReadTextFunction

    def metadata(self) -> dict[str, object]:
        return _sql_runtime_metadata()


class ObjectstoreReadJsonUdf(Udf, name="objectstore.read_json"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (ArrayType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("entries", "path")

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreReadJsonFunction

    def metadata(self) -> dict[str, object]:
        return _sql_runtime_metadata()


class ObjectstoreWriteTextUdf(Udf, name="objectstore.write_text"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return ("path", "content", "content_type")

    def output_schema(self) -> DType:
        return ArrayType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreWriteTextFunction

    def metadata(self) -> dict[str, object]:
        return _sql_runtime_metadata()


class ObjectstoreWriteJsonUdf(Udf, name="objectstore.write_json"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), JsonType())

    def input_args(self) -> Tuple[str, ...]:
        return ("path", "value")

    def output_schema(self) -> DType:
        return ArrayType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., RowFunction]:
        return ObjectstoreWriteJsonFunction

    def metadata(self) -> dict[str, object]:
        return _sql_runtime_metadata()


def _coerce_tree_entries(tree: object) -> list[dict[str, Any]]:
    payload = _coerce_json(tree)
    if isinstance(payload, Mapping):
        candidates = payload.get("tree") or payload.get("entries") or payload.get("files")
        if candidates is None and {"path", "object_uri"} & set(payload.keys()):
            candidates = [payload]
    else:
        candidates = payload
    if isinstance(candidates, str):
        candidates = json.loads(candidates)
    entries: list[dict[str, Any]] = []
    for item in candidates or []:
        if isinstance(item, Mapping):
            entries.append(dict(item))
            continue
        as_dict = getattr(item, "asDict", None)
        if callable(as_dict):
            entries.append(dict(as_dict(recursive=True)))
    return entries


def _coerce_json(value: object) -> object:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    as_dict = getattr(value, "asDict", None)
    if callable(as_dict):
        return as_dict(recursive=True)
    return value


def _normalize_workspace_path(path: str | None) -> str:
    normalized = str(path or ".").replace("\\", "/").strip("/")
    if not normalized or normalized == ".":
        return "."
    return _normalize_tree_path(normalized)


def _local_path(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and ".." in candidate.parts:
        raise PolicyViolation(f"path escapes workspace: {str(path)!r}")
    return candidate


def _normalize_tree_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    if (
        not normalized
        or normalized == "."
        or normalized.startswith("../")
        or "/../" in f"/{normalized}/"
        or normalized.startswith("~/")
    ):
        raise PolicyViolation(f"path escapes workspace: {path!r}")
    return normalized


def _directory_entry(
    path: str,
    *,
    name: str | None = None,
    parent_path: str | None = None,
    entry_type: str = "file",
    size_bytes: int | None = None,
    content_type: str | None = None,
    sha256: str | None = None,
    object_uri: str | None = None,
    is_empty_dir: bool = False,
) -> dict[str, Any]:
    normalized = _normalize_tree_path(path)
    normalized_type = str(entry_type or "").strip().lower()
    if normalized_type not in {"file", "directory"}:
        raise ValueError(f"Unsupported objectstore entry type: {entry_type}")
    parent = parent_path if parent_path is not None else (normalized.rsplit("/", 1)[0] if "/" in normalized else None)
    if parent:
        parent = _normalize_tree_path(parent)
    return {
        "schema_version": DIRECTORY_ENTRY_SCHEMA_VERSION,
        "path": normalized,
        "dataset_path": normalized,
        "name": str(name or PurePosixPath(normalized).name),
        "parent_path": parent,
        "entry_type": normalized_type,
        "size_bytes": int(size_bytes) if size_bytes is not None else None,
        "content_type": str(content_type) if content_type else None,
        "sha256": str(sha256) if sha256 else None,
        "object_uri": str(object_uri) if object_uri else None,
        "is_empty_dir": bool(is_empty_dir),
    }


def _require_file_entry(entries: Any, path: str) -> DirectoryEntry:
    normalized = _normalize_tree_path(path)
    item = _find_entry(entries, normalized)
    if item is None:
        raise FileNotFoundError(normalized)
    if item.entry_type != "file":
        raise IsADirectoryError(normalized)
    return item


def _find_entry(entries: Any, path: str) -> DirectoryEntry | None:
    for item in _coerce_directory_entries(entries):
        if item.path == path:
            return item
    return None


def _coerce_directory_entries(entries: Any) -> list[DirectoryEntry]:
    if entries is None:
        return []
    raw_entries = _coerce_tree_entries(entries)
    return [_entry_from_mapping(item) for item in raw_entries]


def _entry_from_value_or_uri(value: Any) -> DirectoryEntry:
    if isinstance(value, str):
        _parse_object_uri(value)
        path = PurePosixPath(urlparse(value).path.lstrip("/")).name
        return _entry_from_mapping(_directory_entry(path, entry_type="file", object_uri=value))
    if isinstance(value, Mapping):
        return _entry_from_mapping(dict(value))
    as_dict = getattr(value, "asDict", None)
    if callable(as_dict):
        mapping = as_dict(recursive=True)
        if isinstance(mapping, Mapping):
            return _entry_from_mapping(dict(mapping))
    raise ValueError("Expected objectstore.entry or agentcicd-object URI")


def _entry_from_mapping(value: Mapping[str, Any]) -> DirectoryEntry:
    normalized = _directory_entry(
        str(value.get("dataset_path") or value.get("path") or ""),
        name=str(value.get("name") or "") or None,
        parent_path=str(value["parent_path"]) if value.get("parent_path") is not None else None,
        entry_type=str(value.get("entry_type") or ""),
        size_bytes=int(value["size_bytes"]) if isinstance(value.get("size_bytes"), (int, float)) else None,
        content_type=str(value["content_type"]) if value.get("content_type") is not None else None,
        sha256=str(value["sha256"]) if value.get("sha256") is not None else None,
        object_uri=str(value["object_uri"]) if value.get("object_uri") is not None else None,
        is_empty_dir=bool(value.get("is_empty_dir")),
    )
    return DirectoryEntry(
        path=str(normalized["path"]),
        name=str(normalized["name"]),
        parent_path=str(normalized["parent_path"]) if normalized.get("parent_path") is not None else None,
        entry_type=str(normalized["entry_type"]),
        size_bytes=int(normalized["size_bytes"]) if normalized.get("size_bytes") is not None else None,
        content_type=str(normalized["content_type"]) if normalized.get("content_type") is not None else None,
        sha256=str(normalized["sha256"]) if normalized.get("sha256") is not None else None,
        object_uri=str(normalized["object_uri"]) if normalized.get("object_uri") is not None else None,
        is_empty_dir=bool(normalized["is_empty_dir"]),
    )


def _entry_to_mapping(item: DirectoryEntry) -> dict[str, Any]:
    return {
        "schema_version": DIRECTORY_ENTRY_SCHEMA_VERSION,
        "dataset_path": item.path,
        "path": item.path,
        "name": item.name,
        "parent_path": item.parent_path,
        "entry_type": item.entry_type,
        "size_bytes": item.size_bytes,
        "content_type": item.content_type,
        "sha256": item.sha256,
        "object_uri": item.object_uri,
        "is_empty_dir": item.is_empty_dir,
    }


def _put_run_artifact(artifact_path: str, payload: bytes, content_type: str) -> dict[str, Any]:
    normalized = _normalize_tree_path(artifact_path)
    object_uri = _run_artifact_object_uri(normalized)
    store = _require_object_store()
    if store.exists(object_uri):
        raise FileExistsError(normalized)
    store.put_bytes(object_uri, payload, content_type=content_type)
    return _directory_entry(
        normalized,
        entry_type="file",
        size_bytes=len(payload),
        content_type=content_type,
        sha256=hashlib.sha256(payload).hexdigest(),
        object_uri=object_uri,
        is_empty_dir=False,
    )


def _read_object_uri(object_uri: str) -> bytes:
    _parse_object_uri(object_uri)
    return _require_object_store().get_bytes(object_uri)


def _parse_object_uri(object_uri: str) -> tuple[str, str]:
    parsed = urlparse(object_uri)
    if parsed.scheme != "agentcicd-object":
        raise ValueError(f"Unsupported objectstore URI: {object_uri}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise ValueError(f"Invalid objectstore URI: {object_uri}")
    return bucket, key


def _guess_content_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _run_artifact_object_uri(artifact_path: str) -> str:
    run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
    if not run_object_uri:
        raise RuntimeError("AGENTCICD_RUN_OBJECT_URI is required for objectstore.upload")
    return f"{run_object_uri.rstrip('/')}/artifacts/{_normalize_tree_path(artifact_path)}"


def _require_object_store() -> Any:
    if object_store_from_env is not None and object_store_from_env is not _IMPORTED_OBJECT_STORE_FACTORY:
        return object_store_from_env()
    module = sys.modules.get("agentcicd_dp_common.object_store")
    module_factory = getattr(module, "object_store_from_env", None) if module is not None else None
    if module_factory is not None and module_factory is not _IMPORTED_OBJECT_STORE_FACTORY:
        return module_factory()
    if object_store_from_env is not None:
        return object_store_from_env()
    try:
        module = importlib.import_module("agentcicd_dp_common.object_store")
    except ImportError:
        raise
    factory = getattr(module, "object_store_from_env", None)
    if factory is None:
        raise RuntimeError("agentcicd_dp_common.object_store.object_store_from_env is unavailable")
    return factory()
