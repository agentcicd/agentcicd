import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import typer

from agentcicd.sql.runtime.udf_compat.runtime_control import start_driver_runtime_control_server
from agentcicd.sql.engine.runner import EngineRunConfig, run_script_with_new_engine
from agentcicd.sql.engine.table_format_types import TableFormat
from agentcicd.sql.logging_utils import configure_application_logging, configure_object_store_logging
try:
    from agentcicd_dp_common.object_store import object_store_from_env
except Exception:  # pragma: no cover - optional outside the Spark image
    object_store_from_env = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
TRANSIENT_WORKING_DIRS = ("sources", "stream_batches")
_STANDALONE_LIMIT_ROWS_PATTERN = re.compile(r"(?m)^([ \t]*)\$LIMIT_ROWS\s*;?\s*$")


def _configure_file_logging(working_dir: Path) -> None:
    configure_application_logging(working_dir, primary_log_name="run.log")
    run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
    if run_object_uri and object_store_from_env is not None:
        configure_object_store_logging(
            log_uri=f"{run_object_uri.rstrip('/')}/logs/app.log",
            store=object_store_from_env(),
        )


def _clear_transient_working_dirs(working_dir: Path) -> None:
    for directory_name in TRANSIENT_WORKING_DIRS:
        directory_path = working_dir / directory_name
        if directory_path.exists():
            shutil.rmtree(directory_path, ignore_errors=True)


def _prepare_working_dir(working_dir: Path) -> None:
    _clear_transient_working_dirs(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)
    _configure_file_logging(working_dir)


def _require_table_format_support(table_format: TableFormat, enable_delta: bool) -> str:
    normalized_table_format = table_format.value
    if table_format == TableFormat.DELTA and not enable_delta:
        raise typer.BadParameter("--table-format=delta requires --enable-delta.")
    return normalized_table_format


def _parse_macro_definitions(definitions: List[str]) -> Dict[str, str]:
    macros: Dict[str, str] = {}
    for definition in definitions:
        if "=" not in definition:
            raise typer.BadParameter(f"Macro '{definition}' must be in KEY=VALUE format.")
        key, value = definition.split("=", 1)
        key = key.strip()
        if not key:
            raise typer.BadParameter(f"Macro '{definition}' is missing a key.")
        macros[key] = value
    return macros


def _parse_input_definitions(definitions: List[str]) -> Dict[str, str]:
    inputs: Dict[str, str] = {}
    for definition in definitions:
        if "=" not in definition:
            raise typer.BadParameter(f"Input '{definition}' must be in KEY=VALUE format.")
        key, value = definition.split("=", 1)
        key = key.strip()
        if not key:
            raise typer.BadParameter(f"Input '{definition}' is missing a key.")
        inputs[key] = value
    return inputs


def _apply_macros(script: str, macros: Dict[str, str]) -> str:
    rendered = script
    # Standalone row-limit placeholders must become valid SQL before parsing.
    if "LIMIT_ROWS" in macros:
        rendered = _STANDALONE_LIMIT_ROWS_PATTERN.sub(
            lambda m: f"{m.group(1)}LIMIT {macros['LIMIT_ROWS']};",
            rendered,
        )
    for key, value in macros.items():
        if key == "LIMIT_ROWS":
            continue
        rendered = rendered.replace(f"${key}", value)
    return rendered


def _load_script(sql_file: Path, macros: Optional[List[str]]) -> str:
    query_uri = os.getenv("AGENTCICD_QUERY_URI", "").strip()
    if query_uri:
        if object_store_from_env is None:
            raise RuntimeError("AGENTCICD_QUERY_URI requires agentcicd_dp_common object-store support")
        script = object_store_from_env().get_text(query_uri)
    else:
        script = sql_file.read_text()
    macro_map = _parse_macro_definitions(macros or [])
    if not macro_map:
        return script
    return _apply_macros(script, macro_map)


def _load_runtime_config_from_object_storage() -> dict[str, object]:
    config_uri = os.getenv("AGENTCICD_RUNTIME_CONFIG_URI", "").strip()
    if not config_uri:
        return {}
    if object_store_from_env is None:
        raise RuntimeError("AGENTCICD_RUNTIME_CONFIG_URI requires agentcicd_dp_common object-store support")
    payload = object_store_from_env().get_json(config_uri)
    if not isinstance(payload, dict):
        raise RuntimeError("AGENTCICD_RUNTIME_CONFIG_URI did not resolve to a JSON object")
    env_payload = payload.get("env")
    if isinstance(env_payload, dict):
        for key, value in env_payload.items():
            if not isinstance(key, str) or not key.startswith("AGENTCICD_"):
                continue
            if value is None:
                continue
            os.environ[key] = str(value)
    return payload


app = typer.Typer(help="Execute multi-phase SQL scripts with the IR engine.")


@app.command()
def run(
    sql_file: Path = typer.Argument(..., help="Path to SQL script, or placeholder when AGENTCICD_QUERY_URI is set."),
    working_dir: Path = typer.Option(
        Path("./working_dir"), help="Working directory for managed tables"
    ),
    tables_dir: Optional[str] = typer.Option(
        None,
        "--tables-dir",
        help="Managed table directory, local or object-store path such as s3a://...",
    ),
    checkpoint_dir: Optional[str] = typer.Option(
        None,
        "--checkpoint-dir",
        help="Checkpoint directory used by streaming execution, local or object-store path.",
    ),
    table_format: TableFormat = typer.Option(
        TableFormat.PARQUET,
        "--table-format",
        envvar="AGENTCICD_TABLE_FORMAT",
        help="Managed table storage format: parquet or delta.",
    ),
    enable_delta: bool = typer.Option(
        False,
        "--enable-delta/--disable-delta",
        envvar="AGENTCICD_ENABLE_DELTA",
        help="Enable Delta Spark integration (required when --table-format=delta).",
    ),
    macros: Optional[List[str]] = typer.Option(
        None,
        "--macro",
        "-M",
        help="Define SQL macros as KEY=VALUE pairs to replace prior to parsing",
    ),
    inputs: Optional[List[str]] = typer.Option(
        None,
        "--input",
        "-I",
        help="Bind declared SQL inputs as KEY=VALUE pairs",
    ),
    progress_file: Optional[Path] = typer.Option(
        None,
        "--progress-file",
        help="Optional JSONL file to emit run progress events.",
    ),
    debug_json: Optional[str] = typer.Option(
        None,
        "--debug-json",
        envvar="AGENTCICD_RUN_DEBUG_JSON",
        help="Resolved run debug options as JSON.",
    ),
) -> None:
    runtime_config = _load_runtime_config_from_object_storage()
    script = _load_script(sql_file, macros)
    input_values = _parse_input_definitions(inputs or [])
    debug_options = _parse_debug_json(debug_json)
    _apply_fixture_trace_debug_env(debug_options)
    _prepare_working_dir(working_dir)
    rate_limit_server = _start_rate_limit_server_if_configured(runtime_config)
    logger.info(
        "Starting SQL run run_id=%s attempt=%s mode=%s",
        os.getenv("AGENTCICD_RUN_ID", ""),
        os.getenv("AGENTCICD_RUN_ATTEMPT", ""),
        os.getenv("AGENTCICD_SPARK_RUN_MODE", ""),
    )

    normalized_table_format = _require_table_format_support(table_format, enable_delta)
    try:
        run_script_with_new_engine(
            script,
            EngineRunConfig(
                working_dir=str(working_dir),
                table_format=normalized_table_format,
                enable_delta=enable_delta,
                include_cells=True,
                progress_file=str(progress_file) if progress_file is not None else None,
                tables_root=tables_dir,
                checkpoints_root=checkpoint_dir,
                input_values=input_values,
                debug=debug_options,
            ),
        )
    except Exception:
        logger.exception("SQL run failed")
        _write_app_log("failed")
        raise
    finally:
        if rate_limit_server is not None:
            rate_limit_server.shutdown()
            rate_limit_server.server_close()
    logger.info("SQL run completed")
    _write_app_log("completed")


def _start_rate_limit_server_if_configured(runtime_config: dict[str, object] | None = None):
    if os.getenv("AGENTCICD_RATE_LIMITER_MODE", "").strip().lower() != "driver":
        return None
    port = int(os.getenv("AGENTCICD_RATE_LIMITER_PORT", "18080"))
    pool_nodes = []
    if isinstance(runtime_config, dict) and isinstance(runtime_config.get("pool_nodes"), list):
        pool_nodes = [item for item in runtime_config["pool_nodes"] if isinstance(item, dict)]
    server = start_driver_runtime_control_server(port=port, pool_nodes=pool_nodes)
    os.environ["AGENTCICD_RATE_LIMITER_BASE_URL"] = f"http://127.0.0.1:{port}"
    logger.info(
        "Started driver rate-limit lease server on port %s with max_in_flight=%s",
        port,
        os.getenv("AGENTCICD_FIXTURE_MAX_IN_FLIGHT", ""),
    )
    return server


def _parse_debug_json(value: str | None) -> dict[str, object] | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--debug-json must be valid JSON: {exc}") from exc
    if isinstance(payload, bool):
        return {"store_intermediate_tables": payload, "format": "jsonl"}
    if not isinstance(payload, dict):
        raise typer.BadParameter("--debug-json must be a boolean or object")
    return payload


def _apply_fixture_trace_debug_env(debug_options: dict[str, object] | None) -> None:
    if not debug_options:
        return
    tracing = debug_options.get("fixture_call_tracing")
    enabled = False
    max_preview_bytes = None
    if isinstance(tracing, dict):
        enabled = bool(tracing.get("enabled", False))
        max_preview_bytes = tracing.get("max_preview_bytes")
    elif isinstance(tracing, bool):
        enabled = tracing
    if enabled:
        os.environ["AGENTCICD_FIXTURE_CALL_TRACING_ENABLED"] = "1"
    if max_preview_bytes is not None:
        try:
            os.environ["AGENTCICD_FIXTURE_TRACE_MAX_PREVIEW_BYTES"] = str(max(1, int(max_preview_bytes)))
        except (TypeError, ValueError):
            pass


def _write_app_log(status: str) -> None:
    run_object_uri = os.getenv("AGENTCICD_RUN_OBJECT_URI", "").strip()
    if not run_object_uri or object_store_from_env is None:
        return
    payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "status": status,
        "run_id": os.getenv("AGENTCICD_RUN_ID", ""),
        "attempt": os.getenv("AGENTCICD_RUN_ATTEMPT", ""),
        "run_mode": os.getenv("AGENTCICD_SPARK_RUN_MODE", ""),
    }
    app_log_uri = f"{run_object_uri.rstrip('/')}/logs/app.jsonl"
    store = object_store_from_env()
    try:
        existing = store.get_text(app_log_uri)
    except Exception:
        existing = ""
    store.put_text(
        app_log_uri,
        existing + json.dumps(payload, separators=(",", ":")) + "\n",
        content_type="application/x-ndjson",
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
