from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from agentcicd.sql.engine.backends.spark.common import F

try:
    from agentcicd_dp_common.object_store import object_store_from_env
except Exception:  # pragma: no cover - optional outside DP runtime images
    object_store_from_env = None  # type: ignore[assignment]

DEFAULT_LOAD_TABLE_DEBUG_ROW_STREAM_LIMIT = 200
STREAM_DEBUG_LATEST_PART = "stream_latest.jsonl"


class SparkDebugStreamsMixin:
    def _write_debug_row_streams(
        self,
        name: str,
        dataframe: Any,
        *,
        row_count: int | None,
        stage_kind: str | None = None,
    ) -> dict[str, Any]:
        if not self._debug_options.get("table_row_streams"):
            return {}
        if self._is_uri_path(str(self._paths.working_dir)):
            return {}

        row_limit = self._debug_row_stream_limit(stage_kind)
        stream_dataframe = dataframe
        emitted_rows = row_count
        if row_limit is not None:
            stream_dataframe = dataframe.limit(row_limit)
            emitted_rows = min(row_count, row_limit) if row_count is not None else row_limit

        row_stream: dict[str, Any] = {
            "format": "jsonl",
            "content_type": "application/x-ndjson",
            "paths": {},
            "total_rows": emitted_rows,
            "source_total_rows": row_count,
        }
        if row_limit is not None:
            row_stream["row_limit"] = row_limit
        relative_dir = Path("debug") / "tables" / name / "rows"
        output_dir = Path(self._paths.working_dir) / relative_dir
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        self._debug_row_stream_dataframe(stream_dataframe).write.mode("overwrite").option("ignoreNullFields", "false").json(str(output_dir))
        parts = _rename_json_parts_to_jsonl(output_dir)
        object_parts = _mirror_debug_row_stream_parts_to_object_store(relative_dir, parts)
        _delete_debug_row_stream_part_from_object_store(relative_dir, STREAM_DEBUG_LATEST_PART)
        row_stream["path"] = relative_dir.as_posix()
        row_stream["parts"] = [
            {
                "path": object_parts.get(part.name) or (relative_dir / part.name).as_posix(),
            }
            for part in parts
        ]
        return {"row_stream": row_stream}

    def _debug_row_stream_limit(self, stage_kind: str | None) -> int | None:
        global_limit = _optional_positive_int(self._debug_options.get("row_stream_limit"))
        if global_limit is not None:
            return global_limit
        if stage_kind == "load_table":
            return _optional_positive_int(self._debug_options.get("load_table_row_stream_limit"))
        return None

    def _debug_row_stream_dataframe(self, dataframe: Any):
        columns = []
        for field in dataframe.schema.fields:
            column_name = field.name
            if column_name == "__agentcicd_row_id":
                continue
            columns.append(F.col(column_name).alias(column_name))
        return dataframe.select(*columns)

    def _start_stream_debug_row_observer(self, name: str, table_path: str, schema: Any):
        if not self._debug_options.get("table_row_streams"):
            return _NoopStreamDebugRowObserver()
        if self._is_uri_path(str(self._paths.working_dir)):
            return _NoopStreamDebugRowObserver()
        observer = _StreamDebugRowObserver(
            name=name,
            table_path=table_path,
            working_dir=str(self._paths.working_dir),
            row_limit=self._debug_row_stream_limit("stream"),
            read_table=lambda: self._read_table_path(table_path, schema=schema),
            debug_dataframe=self._debug_row_stream_dataframe,
        )
        return observer


class _NoopStreamDebugRowObserver:
    def start(self) -> None:
        return None

    def stop_and_flush(self) -> None:
        return None


class _StreamDebugRowObserver:
    def __init__(
        self,
        *,
        name: str,
        table_path: str,
        working_dir: str,
        row_limit: int | None,
        read_table: Callable[[], Any],
        debug_dataframe: Callable[[Any], Any],
    ) -> None:
        self._name = name
        self._table_path = table_path
        self._working_dir = Path(working_dir)
        self._row_limit = row_limit
        self._read_table = read_table
        self._debug_dataframe = debug_dataframe
        self._poll_seconds = _optional_positive_float(os.getenv("AGENTCICD_STREAM_DEBUG_ROW_POLL_SECONDS"), default=5.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_signature: tuple[str, ...] = ()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"agentcicd-stream-debug-{self._name}",
            daemon=True,
        )
        self._thread.start()

    def stop_and_flush(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, min(30.0, self._poll_seconds + 5.0)))
        self._observe_once(force=True)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            self._observe_once(force=False)

    def _observe_once(self, *, force: bool) -> None:
        signature = self._output_signature()
        if not signature:
            return
        if not force and signature == self._last_signature:
            return
        try:
            dataframe = self._read_table()
            if self._row_limit is not None:
                dataframe = dataframe.limit(self._row_limit)
            self._write_latest_snapshot(dataframe)
            self._last_signature = signature
        except Exception:
            return

    def _write_latest_snapshot(self, dataframe: Any) -> None:
        relative_dir = Path("debug") / "tables" / self._name / "rows"
        output_dir = self._working_dir / relative_dir
        temp_dir = output_dir / "_stream_latest_tmp"
        latest_path = output_dir / STREAM_DEBUG_LATEST_PART
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (
            self._debug_dataframe(dataframe)
            .coalesce(1)
            .write.mode("overwrite")
            .option("ignoreNullFields", "false")
            .json(str(temp_dir))
        )
        lines: list[str] = []
        for part in sorted(temp_dir.glob("part-*.json")):
            lines.extend(line for line in part.read_text(encoding="utf-8").splitlines() if line.strip())
        latest_path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        shutil.rmtree(temp_dir, ignore_errors=True)
        _mirror_debug_row_stream_parts_to_object_store(relative_dir, [latest_path])

    def _output_signature(self) -> tuple[str, ...]:
        if _is_s3a_path(self._table_path):
            return _s3a_output_signature(self._table_path)
        if _is_uri_path(self._table_path):
            return ()
        table_root = Path(self._table_path)
        if not table_root.exists():
            return ()
        files: list[str] = []
        for path in table_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                relative_parts = path.relative_to(table_root).parts
            except ValueError:
                continue
            if "_temporary" in relative_parts:
                continue
            if path.name.startswith(".") or path.name.endswith(".crc"):
                continue
            files.append(path.relative_to(table_root).as_posix())
        return tuple(sorted(files))

def _normalize_debug_options(debug: bool | Mapping[str, Any] | None) -> dict[str, Any]:
    if debug is True:
        return {
            "table_row_streams": True,
            "store_intermediate_tables": True,
            "format": "jsonl",
            "load_table_row_stream_limit": DEFAULT_LOAD_TABLE_DEBUG_ROW_STREAM_LIMIT,
        }
    if not debug:
        return {
            "table_row_streams": False,
            "store_intermediate_tables": False,
            "format": "jsonl",
            "load_table_row_stream_limit": DEFAULT_LOAD_TABLE_DEBUG_ROW_STREAM_LIMIT,
        }
    store_intermediate_tables = bool(
        debug.get(
            "store_intermediate_tables",
            debug.get("table_row_streams", bool(debug.get("enabled", False))),
        )
    )
    load_table_row_stream_limit = _optional_positive_int(
        debug.get("load_table_row_stream_limit"),
        default=DEFAULT_LOAD_TABLE_DEBUG_ROW_STREAM_LIMIT,
    )
    row_stream_limit = _optional_positive_int(debug.get("row_stream_limit"))
    if "row_stream_limit" not in debug:
        row_stream_limit = _optional_positive_int(debug.get("table_row_stream_limit"))
    return {
        "table_row_streams": store_intermediate_tables,
        "store_intermediate_tables": store_intermediate_tables,
        "format": "jsonl",
        "load_table_row_stream_limit": load_table_row_stream_limit,
        "row_stream_limit": row_stream_limit,
    }

def _rename_json_parts_to_jsonl(output_dir: Path) -> list[Path]:
    parts: list[Path] = []
    for index, path in enumerate(sorted(output_dir.glob("part-*.json"))):
        target = path.with_suffix(".jsonl")
        if target.exists():
            target.unlink()
        path.rename(target)
        parts.append(target)
    return parts


def _mirror_debug_row_stream_parts_to_object_store(relative_dir: Path, parts: list[Path]) -> dict[str, str]:
    run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
    if not run_object_uri or object_store_from_env is None:
        return {}
    mirrored: dict[str, str] = {}
    try:
        store = object_store_from_env()
        for part in parts:
            relative_path = (relative_dir / part.name).as_posix()
            store.put_bytes(
                f"{run_object_uri.rstrip('/')}/{relative_path}",
                part.read_bytes(),
                content_type="application/x-ndjson",
            )
            mirrored[part.name] = relative_path
    except Exception:
        return mirrored
    return mirrored


def _delete_debug_row_stream_part_from_object_store(relative_dir: Path, part_name: str) -> None:
    run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
    if not run_object_uri or object_store_from_env is None:
        return
    try:
        object_store_from_env().delete(f"{run_object_uri.rstrip('/')}/{(relative_dir / part_name).as_posix()}")
    except Exception:
        return


def _is_uri_path(path: str) -> bool:
    return bool(urlparse(path).scheme)


def _is_s3a_path(path: str) -> bool:
    return urlparse(path).scheme == "s3a"


def _s3a_output_signature(table_path: str) -> tuple[str, ...]:
    run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
    if not run_object_uri or object_store_from_env is None:
        return ()
    parsed_table = urlparse(table_path)
    parsed_run = urlparse(run_object_uri)
    table_key = parsed_table.path.lstrip("/").rstrip("/")
    run_key = parsed_run.path.lstrip("/").rstrip("/")
    if not table_key.startswith(run_key.rstrip("/") + "/"):
        return ()
    prefix_uri = f"{run_object_uri.rstrip('/')}/{table_key[len(run_key):].strip('/')}/"
    try:
        store = object_store_from_env()
        files = []
        for item in store.list(prefix_uri):
            relative = item.ref.key.removeprefix(table_key.rstrip("/") + "/")
            name = Path(relative).name
            if relative and not name.startswith(".") and not name.endswith(".crc"):
                files.append(f"{relative}:{item.size or 0}:{item.modified_at or ''}")
        return tuple(sorted(files))
    except Exception:
        return ()


def _optional_positive_int(value: Any, *, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else None


def _optional_positive_float(value: Any, *, default: float) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
