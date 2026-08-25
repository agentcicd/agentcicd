from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcicd.sql import api
from agentcicd.sql.engine.objectstore_functions import (
    DIRECTORY_ENTRY_STRUCT,
    rewrite_objectstore_function_calls,
    sql_uses_objectstore_functions,
)
from agentcicd_dp_common.object_store import FakeObjectStore
from agentcicd.fixtures.environments.core.errors import PolicyViolation
from agentcicd.fixtures.functions import objectstore


TREE = [
    {
        "path": "input.txt",
        "name": "input.txt",
        "parent_path": None,
        "entry_type": "file",
        "size_bytes": 5,
        "content_type": "text/plain",
        "sha256": "abc",
        "object_uri": "agentcicd-object://org.test/dataset/dataset.test/raw/case_1/input.txt",
        "is_empty_dir": False,
    },
    {
        "path": "workspace",
        "name": "workspace",
        "parent_path": None,
        "entry_type": "directory",
        "size_bytes": None,
        "content_type": None,
        "sha256": None,
        "object_uri": None,
        "is_empty_dir": False,
    },
    {
        "path": "workspace/expected.json",
        "name": "expected.json",
        "parent_path": "workspace",
        "entry_type": "file",
        "size_bytes": 12,
        "content_type": "application/json",
        "sha256": "def",
        "object_uri": "agentcicd-object://org.test/dataset/dataset.test/raw/case_1/workspace/expected.json",
        "is_empty_dir": False,
    },
]


def test_objectstore_helpers_find_and_glob_entries() -> None:
    assert objectstore.exists(TREE, "input.txt") is True
    assert objectstore.exists(TREE, "missing.txt") is False
    assert objectstore.find(TREE, "workspace/expected.json")["dataset_path"] == "workspace/expected.json"
    assert [entry["dataset_path"] for entry in objectstore.glob(TREE, "workspace/*.json")] == [
        "workspace/expected.json"
    ]


def test_objectstore_file_reads_reject_directories_and_escape_paths() -> None:
    with pytest.raises(IsADirectoryError):
        objectstore.read_text(TREE, "workspace")
    with pytest.raises(PolicyViolation, match="escapes workspace"):
        objectstore.read_text(TREE, "../input.txt")


def test_objectstore_read_json_validates_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(objectstore, "_read_object_uri", lambda _object_uri: json.dumps({"ok": True}).encode("utf-8"))
    assert objectstore.read_json(TREE, "workspace/expected.json") == '{"ok":true}'


def test_rewrite_objectstore_function_calls() -> None:
    sql = "SELECT objectstore.exists(tree, 'x'), objectstore.glob(tree, 'answer/*') FROM cases"
    assert (
        rewrite_objectstore_function_calls(sql)
        == "SELECT objectstore_exists(tree, 'x'), objectstore_glob(tree, 'answer/*') FROM cases"
    )
    assert (
        rewrite_objectstore_function_calls("SELECT OBJECTSTORE.EXISTS(tree, 'x') FROM cases")
        == "SELECT objectstore_exists(tree, 'x') FROM cases"
    )
    assert rewrite_objectstore_function_calls("SELECT directory.read_text(tree, 'input.txt') FROM cases") == (
        "SELECT directory.read_text(tree, 'input.txt') FROM cases"
    )
    assert sql_uses_objectstore_functions(sql) is True
    assert sql_uses_objectstore_functions("SELECT objectstore_exists(tree, 'answer/answer.json') FROM cases") is True
    assert sql_uses_objectstore_functions("SELECT directory.read_text(tree, 'input.txt') FROM cases") is False
    assert sql_uses_objectstore_functions("SELECT objectstore.download(entry, 'local/path') FROM cases") is False
    assert (
        rewrite_objectstore_function_calls("SELECT objectstore.download(entry, 'local/path') FROM cases")
        == "SELECT objectstore.download(entry, 'local/path') FROM cases"
    )


def test_static_validation_accepts_objectstore_surface_functions() -> None:
    result = api.validate_recipe(
        """
        CREATE BATCH TABLE checked
        SELECT objectstore.exists(array(), 'answer/answer.json') AS answer_exists;
        """
    )

    assert result.validation_mode == "static"


def test_static_validation_rejects_removed_directory_surface_functions() -> None:
    with pytest.raises(ValueError, match="Unknown registered function 'directory.exists'"):
        api.validate_recipe(
            """
            CREATE BATCH TABLE checked
            SELECT directory.exists(array(), 'answer/answer.json') AS answer_exists;
            """
        )


def test_static_validation_rejects_python_only_objectstore_functions() -> None:
    with pytest.raises(ValueError, match="Unknown registered function 'objectstore.download'"):
        api.validate_recipe(
            """
            CREATE BATCH TABLE checked
            SELECT objectstore.download(named_struct('path', 'answer.txt'), 'answer.txt') AS local_path;
            """
        )


def test_objectstore_entry_spark_struct_matches_fixture_artifact_order() -> None:
    assert DIRECTORY_ENTRY_STRUCT.fieldNames() == [
        "content_type",
        "dataset_path",
        "entry_type",
        "is_empty_dir",
        "name",
        "object_uri",
        "parent_path",
        "path",
        "schema_version",
        "sha256",
        "size_bytes",
    ]


def test_objectstore_entry_builds_canonical_struct() -> None:
    entry = objectstore.entry(
        dataset_path="answer/plot.png",
        entry_type="file",
        size_bytes=3,
        content_type="image/png",
        sha256="abc",
        object_uri="agentcicd-object://org.test/runs/run.test/attempt_1/artifacts/answer/plot.png",
    )

    assert entry == {
        "schema_version": "agentcicd.directory.entry.v1",
        "dataset_path": "answer/plot.png",
        "path": "answer/plot.png",
        "name": "plot.png",
        "parent_path": "answer",
        "entry_type": "file",
        "size_bytes": 3,
        "content_type": "image/png",
        "sha256": "abc",
        "object_uri": "agentcicd-object://org.test/runs/run.test/attempt_1/artifacts/answer/plot.png",
        "is_empty_dir": False,
    }


def test_objectstore_upload_and_download_use_run_object_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeObjectStore()
    source = tmp_path / "plot.png"
    source.write_bytes(b"png")
    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org.test/runs/run.test/attempt_1")
    monkeypatch.setattr(objectstore, "object_store_from_env", lambda: store)

    entry = objectstore.upload_file(str(source), "answer/plot.png", "image/png")

    assert entry["object_uri"] == "agentcicd-object://org.test/runs/run.test/attempt_1/artifacts/answer/plot.png"
    assert entry["size_bytes"] == 3
    assert entry["sha256"]
    assert store.get_bytes(entry["object_uri"]) == b"png"

    cwd = Path.cwd()
    try:
        monkeypatch.chdir(tmp_path)
        local_path = objectstore.download_file(entry, "downloaded/plot.png")
        assert local_path == "downloaded/plot.png"
        assert (tmp_path / "downloaded" / "plot.png").read_bytes() == b"png"
    finally:
        monkeypatch.chdir(cwd)


def test_objectstore_upload_all_and_download_all_reject_escape_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeObjectStore()
    root = tmp_path / "answer"
    root.mkdir()
    (root / "plot.png").write_bytes(b"png")
    monkeypatch.setenv("AGENTCICD_RUN_OBJECT_URI", "agentcicd-object://org.test/runs/run.test/attempt_1")
    monkeypatch.setattr(objectstore, "object_store_from_env", lambda: store)

    entries = objectstore.upload_all(str(root), "answer")
    assert [entry["dataset_path"] for entry in entries] == ["answer", "answer/plot.png"]

    with pytest.raises(PolicyViolation, match="escapes workspace"):
        objectstore.download_file(entries[1], "../plot.png")

    monkeypatch.chdir(tmp_path)
    assert objectstore.download_all(entries, "restored") == "restored"
    assert (tmp_path / "restored" / "answer" / "plot.png").read_bytes() == b"png"
