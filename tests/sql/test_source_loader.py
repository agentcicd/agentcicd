from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcicd.sql.engine.interfaces import BackendLayout
from agentcicd.sql.engine.source_loader import SparkSourceLoader
from agentcicd.sql.ir.options import StatementOptions


class _FakeFrame:
    def __init__(self, label: str):
        self.label = label
        self.filters: list[str] = []

    def filter(self, condition):
        self.filters.append(str(condition))
        return self


class _FakeRead:
    def __init__(self):
        self.calls: list[tuple[str, str, str] | tuple[str, str, str, str]] = []
        self._format = ""

    def format(self, value: str):
        self._format = value
        self.calls.append(("format", value, ""))
        return self

    def option(self, key: str, value: str):
        self.calls.append(("option", self._format, key, value))
        return self

    def load(self, path: str):
        self.calls.append(("load", self._format, path))
        return _FakeFrame(path)


class _FakeSpark:
    def __init__(self):
        self.read = _FakeRead()


def _layout(tmp_path: Path) -> BackendLayout:
    return BackendLayout(
        working_dir=str(tmp_path),
        tables_root=str(tmp_path / "tables"),
        sources_root=str(tmp_path / "sources"),
        outputs_root=str(tmp_path / "outputs"),
        publish_root=str(tmp_path / "published"),
        checkpoints_root=str(tmp_path / "checkpoints"),
        stream_batches_root=str(tmp_path / "stream_batches"),
        http_cache_root=str(tmp_path / "http_cache"),
        annotation_tasks_root=str(tmp_path / "annotation_tasks"),
    )


def test_source_loader_loads_local_filesystem_path_directly(tmp_path: Path):
    loader = SparkSourceLoader(_layout(tmp_path))
    spark = _FakeSpark()

    loader.load_dataframe(spark, "/tmp/raw.parquet", StatementOptions.from_mapping({"format": "parquet"}))

    assert ("load", "parquet", "/tmp/raw.parquet") in spark.read.calls


def test_source_loader_keeps_relative_file_path_local(tmp_path: Path):
    loader = SparkSourceLoader(_layout(tmp_path))
    spark = _FakeSpark()

    loader.load_dataframe(spark, "rows.csv", StatementOptions.from_mapping({"format": "csv"}))

    assert ("load", "csv", "rows.csv") in spark.read.calls


def test_source_loader_preserves_explicit_non_agentcicd_scheme(tmp_path: Path):
    loader = SparkSourceLoader(_layout(tmp_path))
    spark = _FakeSpark()

    loader.load_dataframe(spark, "s3://bucket/raw.parquet", StatementOptions.from_mapping({"format": "parquet"}))

    assert ("load", "parquet", "s3://bucket/raw.parquet") in spark.read.calls


def test_source_loader_resolves_agentcicd_dataset_uri_to_object_store_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    loader = SparkSourceLoader(_layout(tmp_path))
    spark = _FakeSpark()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "status": "active",
                    "format": "parquet",
                    "storage_uri": "agentcicd-object://org.093ea7db6a5a/dataset/dataset.123/data.parquet",
                }
            ).encode("utf-8")

    monkeypatch.setenv("AGENTCICD_CP_INTERNAL_BASE_URL", "http://cp.internal")
    monkeypatch.setenv("AGENTCICD_CP_DP_INTERNAL_TOKEN", "token")
    monkeypatch.setattr("agentcicd.sql.engine.source_loader.urlopen", lambda request, timeout=30: _Response())

    loader.load_dataframe(spark, "agentcicd://dataset.123", StatementOptions.from_mapping({"format": "parquet"}))

    assert ("load", "parquet", "s3a://org-093ea7db6a5a/dataset/dataset.123/data.parquet") in spark.read.calls


def test_source_loader_treats_bare_dataset_id_as_agentcicd_dataset_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    loader = SparkSourceLoader(_layout(tmp_path))
    spark = _FakeSpark()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "status": "active",
                    "format": "parquet",
                    "storage_uri": "agentcicd-object://datasets/dataset.456/data.parquet",
                }
            ).encode("utf-8")

    monkeypatch.setenv("AGENTCICD_CP_INTERNAL_BASE_URL", "http://cp.internal")
    monkeypatch.setenv("AGENTCICD_CP_DP_INTERNAL_TOKEN", "token")
    monkeypatch.setattr("agentcicd.sql.engine.source_loader.urlopen", lambda request, timeout=30: _Response())

    loader.load_dataframe(spark, "dataset.456", StatementOptions.from_mapping({"format": "parquet"}))

    assert ("load", "parquet", "s3a://datasets/dataset.456/data.parquet") in spark.read.calls


def test_source_loader_requires_internal_settings_for_agentcicd_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    loader = SparkSourceLoader(_layout(tmp_path))
    spark = _FakeSpark()
    monkeypatch.delenv("AGENTCICD_CP_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("AGENTCICD_CP_DP_INTERNAL_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="AGENTCICD_CP_INTERNAL_BASE_URL and AGENTCICD_CP_DP_INTERNAL_TOKEN"):
        loader.load_dataframe(spark, "agentcicd://dataset.123", StatementOptions.from_mapping({"format": "parquet"}))


def test_source_loader_directory_filters_path_entry_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    loader = SparkSourceLoader(_layout(tmp_path))
    spark = _FakeSpark()
    monkeypatch.setattr("agentcicd.sql.engine.source_loader.F.expr", lambda value: value)

    frame = loader.load_dataframe(
        spark,
        "/tmp/filesystem-entries",
        StatementOptions.from_mapping(
            {
                "format": "directory",
                "include_paths": "^data/00000004/",
                "exclude_paths": "\\.tmp$",
            }
        ),
    )

    assert isinstance(frame, _FakeFrame)
    assert frame.filters == ["((dataset_path RLIKE '^data/00000004/')) AND NOT ((dataset_path RLIKE '\\\\.tmp$'))"]
