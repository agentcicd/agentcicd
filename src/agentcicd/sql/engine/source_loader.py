from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pyspark.sql import functions as F

from agentcicd.sql.engine.objectstore_functions import (
    compile_filter_patterns,
    is_directory_format,
    normalize_filter_patterns,
)
from agentcicd.sql.engine.interfaces import BackendLayout, SourceLoader
from agentcicd.sql.ir.options import StatementOptions


def _physical_object_bucket(logical_bucket: str) -> str:
    return logical_bucket.strip().lower().replace(".", "-")


class SparkSourceLoader(SourceLoader):
    def __init__(self, layout: BackendLayout) -> None:
        self._layout = layout

    def load_dataframe(self, spark_session, path: str, options: StatementOptions):
        normalized_path = self._normalize_source_path(path)
        if self._is_http_url(normalized_path):
            return self._load_http_dataframe(spark_session, normalized_path, options)
        if self._is_agentcicd_dataset_uri(normalized_path):
            return self._load_agentcicd_dataframe(spark_session, normalized_path, options)
        source_format = self._resolve_source_format(normalized_path, options)
        reader = self._build_reader_for_format(spark_session, source_format)
        dataframe = reader.load(normalized_path)
        if is_directory_format(source_format):
            return self._apply_directory_load_filters(dataframe, options)
        return dataframe

    @staticmethod
    def _build_reader_for_format(spark_session, source_format: str):
        normalized = source_format.lower()
        if normalized in {"jsonl", "ndjson"}:
            return spark_session.read.format("json").option("multiLine", "false")
        if normalized == "json":
            return spark_session.read.format("json").option("multiLine", "true")
        if normalized == "csv":
            return (
                spark_session.read.format("csv")
                .option("header", "true")
                .option("inferSchema", "true")
                .option("escape", '"')
            )
        if is_directory_format(normalized):
            return spark_session.read.format("parquet")
        if normalized in {"parquet", "delta"}:
            return spark_session.read.format(normalized)
        raise ValueError(
            f"Unsupported LOAD format '{source_format}'. Supported formats: json, jsonl, ndjson, parquet, delta, csv."
        )

    def _load_http_dataframe(self, spark_session, path: str, options: StatementOptions):
        source_format = self._resolve_source_format(path, options, allow_infer_http=True)
        local_path = self._download_http_source(path)
        if local_path.lower().endswith(".gz"):
            uncompressed_path = self._strip_compression_suffix(local_path)
            if not os.path.exists(uncompressed_path):
                with gzip.open(local_path, "rb") as compressed_file, open(uncompressed_path, "wb") as output_file:
                    shutil.copyfileobj(compressed_file, output_file)
            local_path = uncompressed_path
        reader = self._build_reader_for_format(spark_session, source_format)
        return reader.load(local_path)

    def _load_agentcicd_dataframe(self, spark_session, path: str, options: StatementOptions):
        dataset_id = self._dataset_id_from_agentcicd_uri(path)
        source_path, dataset_format = self._resolve_agentcicd_dataset_source(dataset_id)
        source_format = self._resolve_source_format(source_path, options)
        if not isinstance(options.get("format"), str):
            source_format = str(dataset_format or source_format).lower()
        reader = self._build_reader_for_format(spark_session, source_format)
        dataframe = reader.load(source_path)
        if is_directory_format(source_format):
            return self._apply_directory_load_filters(dataframe, options)
        return dataframe

    def _resolve_source_format(
        self,
        path: str,
        options: StatementOptions,
        *,
        allow_infer_http: bool = False,
    ) -> str:
        option_value = options.get("format")
        if isinstance(option_value, str) and option_value.strip():
            return option_value.strip().lower()
        if allow_infer_http and self._is_http_url(path):
            return self._infer_remote_format(path)
        return self._infer_format_from_path(path)

    @staticmethod
    def _is_http_url(path: str) -> bool:
        lower = path.lower().strip()
        return lower.startswith("http://") or lower.startswith("https://")

    @staticmethod
    def _is_agentcicd_dataset_uri(path: str) -> bool:
        return path.lower().strip().startswith("agentcicd://")

    def _normalize_source_path(self, path: str) -> str:
        raw = str(path or "").strip()
        if not raw:
            return raw
        parsed = urlparse(raw)
        if parsed.scheme:
            return raw
        if self._looks_like_local_filesystem_path(raw):
            return raw
        return f"agentcicd://{raw}"

    @staticmethod
    def _looks_like_local_filesystem_path(path: str) -> bool:
        lowered = path.lower()
        if lowered.startswith("dataset."):
            return False
        if path.startswith(("/", "./", "../", "~/")):
            return True
        if "/" in path or "\\" in path:
            return True
        suffix = Path(path).suffix
        return bool(suffix)

    @staticmethod
    def _dataset_id_from_agentcicd_uri(path: str) -> str:
        dataset_id = path[len("agentcicd://"):].strip().strip("/")
        if not dataset_id:
            raise ValueError("agentcicd:// dataset paths must include a dataset id")
        return dataset_id

    def _resolve_agentcicd_dataset_source(self, dataset_id: str) -> tuple[str, str]:
        dataset = self._fetch_agentcicd_dataset_metadata(dataset_id)
        status = str(dataset.get("status") or "")
        if status != "active":
            raise RuntimeError(f"Dataset '{dataset_id}' is not active (status={status})")

        storage_uri = str(dataset.get("storage_uri") or "").strip()
        if not storage_uri:
            raise RuntimeError(f"Dataset '{dataset_id}' has no storage_uri")
        bucket, object_name = self._parse_agentcicd_object_storage_uri(storage_uri)
        source_path = self._s3a_path(bucket, object_name)
        return source_path, str(dataset.get("format") or self._infer_format_from_path(source_path)).lower()

    def _fetch_agentcicd_dataset_metadata(self, dataset_id: str) -> dict[str, object]:
        base_url = os.getenv("AGENTCICD_CP_INTERNAL_BASE_URL", "").rstrip("/")
        internal_token = os.getenv("AGENTCICD_CP_DP_INTERNAL_TOKEN", "").strip()
        if not base_url or not internal_token:
            raise RuntimeError("AGENTCICD_CP_INTERNAL_BASE_URL and AGENTCICD_CP_DP_INTERNAL_TOKEN must be set for agentcicd:// loads")
        request = Request(
            f"{base_url}/datasets/{dataset_id}/resolve",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AgentCICD-Internal-Token": internal_token,
            },
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Dataset resolution for '{dataset_id}' returned invalid payload")
        return payload

    @staticmethod
    def _s3a_path(bucket: str, object_name: str) -> str:
        return f"s3a://{_physical_object_bucket(bucket)}/{object_name.lstrip('/')}"

    @staticmethod
    def _parse_agentcicd_object_storage_uri(storage_uri: str) -> tuple[str, str]:
        parsed = urlparse(storage_uri)
        if parsed.scheme != "agentcicd-object":
            raise RuntimeError(f"Unsupported dataset storage uri '{storage_uri}'")
        bucket = (parsed.netloc or "").strip()
        object_name = (parsed.path or "").lstrip("/")
        if not bucket or not object_name:
            raise RuntimeError(f"Invalid dataset storage uri '{storage_uri}'")
        return bucket, object_name

    @staticmethod
    def _strip_compression_suffix(path: str) -> str:
        if path.lower().endswith(".gz"):
            return path[:-3]
        return path

    def _infer_remote_format(self, url: str) -> str:
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path or "").lower()
        filename = self._strip_compression_suffix(filename)
        if filename.endswith(".jsonl") or filename.endswith(".ndjson"):
            return "jsonl"
        if filename.endswith(".json"):
            return "json"
        if filename.endswith(".parquet"):
            return "parquet"
        if filename.endswith(".csv"):
            return "csv"
        raise ValueError(
            f"Could not infer remote format from URL '{url}'. "
            "Specify explicit WITH FORMAT='json|jsonl|parquet|csv'."
        )

    @staticmethod
    def _infer_format_from_path(path: str) -> str:
        parsed = urlparse(path)
        filename = os.path.basename(parsed.path or path).lower()
        if filename.endswith(".jsonl") or filename.endswith(".ndjson"):
            return "jsonl"
        if filename.endswith(".json"):
            return "json"
        if filename.endswith(".csv"):
            return "csv"
        if filename.endswith(".delta"):
            return "delta"
        return "parquet"

    def _apply_directory_load_filters(self, dataframe, options: StatementOptions):
        include_paths = normalize_filter_patterns(options.get("include_paths"))
        exclude_paths = normalize_filter_patterns(options.get("exclude_paths"))
        compile_filter_patterns(include_paths, "include_paths")
        compile_filter_patterns(exclude_paths, "exclude_paths")
        path_condition = _spark_path_filter_condition("dataset_path", include_paths, exclude_paths)
        return dataframe.filter(F.expr(path_condition))

    def _download_http_source(self, url: str) -> str:
        parsed = urlparse(url)
        file_name = os.path.basename(parsed.path or "").strip() or "download"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        cache_dir = Path(self._layout.http_cache_root)
        cache_dir.mkdir(parents=True, exist_ok=True)
        local_path = cache_dir / f"{digest}_{file_name}"
        if local_path.exists():
            return str(local_path)
        with urlopen(url, timeout=30) as response, open(local_path, "wb") as output_file:  # noqa: S310
            shutil.copyfileobj(response, output_file)
        return str(local_path)


def _spark_path_filter_condition(path_expression: str, include_patterns: tuple[str, ...], exclude_patterns: tuple[str, ...]) -> str:
    include_condition = "true"
    if include_patterns:
        include_condition = "(" + " OR ".join(
            f"{path_expression} RLIKE {_spark_string_literal(pattern)}" for pattern in include_patterns
        ) + ")"
    exclude_condition = "false"
    if exclude_patterns:
        exclude_condition = "(" + " OR ".join(
            f"{path_expression} RLIKE {_spark_string_literal(pattern)}" for pattern in exclude_patterns
        ) + ")"
    return f"({include_condition}) AND NOT ({exclude_condition})"


def _spark_string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
