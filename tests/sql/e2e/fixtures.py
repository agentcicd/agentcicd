from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agentcicd.sql.engine.runtime_functions import _cell_return_type
from agentcicd.sql.testing.e2e_udfs import ControlledEchoUdf, ExtractJsonScoreUdf, NormalizeTextUdf
from agentcicd.sql.udf_registry import clear_registered_udfs, register_udf
from pyspark.sql import Row
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DoubleType,
    MapType,
    StringType as SparkStringType,
    StructField,
    StructType,
)


ERROR_TYPE = ArrayType(
    StructType(
        [
            StructField("code", SparkStringType(), False),
            StructField("message", SparkStringType(), False),
            StructField("source", SparkStringType(), True),
            StructField("path", SparkStringType(), True),
            StructField("recoverable", BooleanType(), False),
            StructField("cause_code", SparkStringType(), True),
            StructField("cause_message", SparkStringType(), True),
            StructField("details", MapType(SparkStringType(), SparkStringType(), True), False),
        ]
    ),
    False,
)

def cell_type(value_type):
    return _cell_return_type(value_type)


def clean_cell(value: Any) -> dict[str, Any]:
    return {
        "cell_id": None,
        "value": value,
        "metadata": {
            "errors": [],
            "latency_ms": None,
        },
        "__agentcicd_cell": True,
    }


def error_cell(code: str, message: str, source: str) -> dict[str, Any]:
    return {
        "cell_id": None,
        "value": None,
        "metadata": {
            "errors": [
                {
                    "code": code,
                    "message": message,
                    "source": source,
                    "path": None,
                    "recoverable": True,
                    "cause_code": None,
                    "cause_message": None,
                    "details": {},
                }
            ],
            "latency_ms": None,
        },
        "__agentcicd_cell": True,
    }


def row_cells_by_value(rows) -> list[dict[str, Any]]:
    return [row.asDict(recursive=True) for row in rows]


def write_wrapped_table(spark, path: Path, rows: list[dict[str, Any]], value_schema: dict[str, Any]) -> None:
    schema = StructType([StructField(name, cell_type(value_type), True) for name, value_type in value_schema.items()])
    spark.createDataFrame([Row(**row) for row in rows], schema=schema).write.mode("overwrite").parquet(str(path))


def read_table_cells(spark, tmp_path: Path, table_name: str) -> list[dict[str, Any]]:
    rows = spark.read.parquet(str(tmp_path / "tables" / table_name)).collect()
    return row_cells_by_value(rows)


def assert_stage_completed(tmp_path: Path, stage_name: str) -> dict[str, Any]:
    payload = json.loads((tmp_path / "outputs" / f"stage_{stage_name}.json").read_text(encoding="utf-8"))
    assert payload["stage_name"] == stage_name
    assert payload["status"] == "completed"
    assert payload["wrapped_mode"] is True
    assert "cell_schema_version" not in payload
    return payload


def assert_schema_sidecar(tmp_path: Path, table_name: str) -> dict[str, Any]:
    payload = json.loads((tmp_path / "outputs" / "schemas" / f"{table_name}.json").read_text(encoding="utf-8"))
    assert payload["table"] == table_name
    assert "cell_metadata_schema_version" not in payload
    assert "wrapped_schema" in payload
    assert "value_schema" in payload
    return payload


def register_local_e2e_udfs() -> None:
    register_udf(NormalizeTextUdf, "normalize_text")
    register_udf(ExtractJsonScoreUdf, "extract_json_score")
    register_udf(ControlledEchoUdf, "controlled_echo")


def clear_local_e2e_udfs() -> None:
    clear_registered_udfs()


def local_udf_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "normalize_text",
            "type": "python",
            "call_name": "normalize_text",
            "runtime_alias": "normalize_text",
            "signature": {"parameters": [{"name": "text", "type_sql": "STRING"}]},
            "return_type_sql": "STRING",
        },
        {
            "name": "extract_json_score",
            "type": "python",
            "call_name": "extract_json_score",
            "runtime_alias": "extract_json_score",
            "signature": {"parameters": [{"name": "text", "type_sql": "STRING"}]},
            "return_type_sql": "DOUBLE",
        },
        {
            "name": "controlled_echo",
            "type": "python",
            "call_name": "controlled_echo",
            "runtime_alias": "controlled_echo",
            "signature": {
                "parameters": [
                    {"name": "text", "type_sql": "STRING"},
                    {"name": "limiter", "type_sql": "ANY", "has_default": True},
                ]
            },
            "return_type_sql": "STRING",
        },
    ]


@dataclass
class RuntimeRequestRecorder:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, function_name: str, args: dict[str, Any]) -> None:
        self.calls.append({"function_name": function_name, "case_id": args.get("case_id"), "args": args})

    def calls_for(self, function_name: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["function_name"] == function_name]


class E2ERuntimeServer:
    def __init__(self) -> None:
        self.recorder = RuntimeRequestRecorder()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "E2ERuntimeServer":
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def calls_for(self, function_name: str) -> list[dict[str, Any]]:
        return self.recorder.calls_for(function_name)

    def _handler_class(self):
        recorder = self.recorder

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw_body or "{}")
                args = payload.get("args") if isinstance(payload, dict) else {}
                if not isinstance(args, dict):
                    args = {}
                function_name = self.path.removeprefix("/invoke/")
                recorder.record(function_name, args)
                case_id = str(args.get("case_id") or "")
                if case_id.endswith("http_500"):
                    self._send_json(500, {"error": "requested failure"})
                    return
                if case_id.endswith("invalid_json"):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{")
                    return
                if case_id.endswith("bad_payload"):
                    self._send_json(200, {"result": {"unexpected": object().__class__.__name__}})
                    return
                if function_name == "support.simulate_turn":
                    self._send_json(200, {"result": _simulate_turn(args)})
                    return
                if function_name == "judge.policy_adherence":
                    self._send_json(200, {"result": _policy_adherence(args)})
                    return
                if function_name == "judge.citation":
                    failing_cases = {
                        item.strip()
                        for item in os.getenv("AGENTCICD_E2E_CITATION_FAIL_CASES", "").split(",")
                        if item.strip()
                    }
                    if str(args.get("case_id") or "").endswith("http_500") or str(args.get("case_id") or "") in failing_cases:
                        self._send_json(500, {"error": "requested citation judge failure"})
                    else:
                        self._send_json(200, {"result": _citation_judgment(args)})
                    return
                if function_name == "embed.text":
                    if "embed_fail" in str(args.get("text") or "").lower():
                        self._send_json(500, {"error": "embedding failed"})
                    else:
                        self._send_json(200, {"result": {"vector": [1.0, float(len(str(args.get("text") or "")))], "model": args.get("model") or "bge"}})
                    return
                if function_name == "run_task":
                    self._send_json(
                        200,
                        {
                            "result": {
                                "status": "completed",
                                "final_output": f"resolved:{args.get('task')}",
                                "transcript": [],
                                "artifacts": [],
                                "error": None,
                                "duration_ms": 1,
                                "metadata": {"harness": "codex", "returncode": 0},
                            }
                        },
                    )
                    return
                self._send_json(404, {"error": f"unknown function {function_name}"})

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _Handler


def _simulate_turn(args: dict[str, Any]) -> dict[str, Any]:
    message = str(args.get("message") or "")
    intent = "refund" if "refund" in message else "cancel" if "cancel" in message else "order_status"
    return {
        "history": [
            {"role": "user", "content": message},
            {"role": "assistant", "content": f"handled:{intent}"},
        ],
        "intent": intent,
        "terminated_by": "assistant",
        "score": 0.9 if intent != "refund" else 0.68,
    }


def _policy_adherence(args: dict[str, Any]) -> dict[str, Any]:
    case_id = str(args.get("case_id") or "")
    score = 0.65 if "002" in case_id else 0.9
    if case_id.endswith("invalid_score"):
        score = "not-a-number"  # type: ignore[assignment]
    return {
        "score": score,
        "pass": isinstance(score, float) and score >= 0.7,
        "reasons": ["deterministic"],
        "tags": {"fixture": "policy_adherence"},
    }


def _citation_judgment(args: dict[str, Any]) -> dict[str, Any]:
    passage_text = str(args.get("passage_text") or "").lower()
    judge_version = str(args.get("judge_version") or "v1")
    relevant = "warranty" in passage_text or "oil" in passage_text or "battery" in passage_text
    if judge_version == "strict":
        score = 0.95 if "warranty" in passage_text else 0.58 if relevant else 0.1
    else:
        score = 0.9 if relevant else 0.2
    return {
        "relevance_label": "relevant" if relevant else "not_relevant",
        "support_label": "answers_question" if score >= 0.7 else "related_but_insufficient",
        "citation_label": "should_cite" if score >= 0.7 else "should_not_cite",
        "confidence": score,
        "rationale": f"{judge_version}:{args.get('passage_id')}",
    }


def runtime_specs(base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "support.simulate_turn",
            "type": "remote",
            "runtime_alias": "support_simulate_turn",
            "base_url": base_url,
            "invoke_path": "/invoke/support.simulate_turn",
            "signature": {
                "parameters": [
                    {"name": "case_id", "type_sql": "STRING"},
                    {"name": "message", "type_sql": "STRING"},
                    {"name": "policy", "type_sql": "VARIANT"},
                    {"name": "policy_version", "type_sql": "STRING"},
                ]
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "history": {"type": "array", "items": {"type": "object", "additionalProperties": {"type": "json"}}},
                    "intent": {"type": "string"},
                    "terminated_by": {"type": "string"},
                    "score": {"type": "number"},
                },
            },
        },
        {
            "name": "judge.policy_adherence",
            "type": "remote",
            "runtime_alias": "judge_policy_adherence",
            "base_url": base_url,
            "invoke_path": "/invoke/judge.policy_adherence",
            "signature": {
                "parameters": [
                    {"name": "case_id", "type_sql": "STRING"},
                    {"name": "history", "type_sql": "VARIANT"},
                    {"name": "policy", "type_sql": "VARIANT"},
                ]
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "score": {"type": "number"},
                    "pass": {"type": "boolean"},
                    "reasons": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "object", "additionalProperties": {"type": "string"}},
                },
            },
        },
        {
            "name": "embed.text",
            "type": "remote",
            "runtime_alias": "embed_text",
            "base_url": base_url,
            "invoke_path": "/invoke/embed.text",
            "signature": {
                "parameters": [
                    {"name": "text", "type_sql": "STRING"},
                    {"name": "model", "type_sql": "STRING"},
                ]
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "vector": {"type": "array", "items": {"type": "number"}},
                    "model": {"type": "string"},
                },
            },
        },
        {
            "name": "judge.citation",
            "type": "remote",
            "runtime_alias": "judge_citation",
            "base_url": base_url,
            "invoke_path": "/invoke/judge.citation",
            "signature": {
                "parameters": [
                    {"name": "case_id", "type_sql": "STRING"},
                    {"name": "sample_id", "type_sql": "STRING"},
                    {"name": "passage_id", "type_sql": "STRING"},
                    {"name": "passage_text", "type_sql": "STRING"},
                    {"name": "judge_version", "type_sql": "STRING"},
                ]
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "relevance_label": {"type": "string"},
                    "support_label": {"type": "string"},
                    "citation_label": {"type": "string"},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
            },
            "cache": True,
            "version": "e2e-v1",
        },
    ]
