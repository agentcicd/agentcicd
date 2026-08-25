from __future__ import annotations

import threading

import pytest

from agentcicd.sql.engine.backends.spark.table_registry import SparkTableRegistry


pytestmark = pytest.mark.smoke


def test_table_registry_records_paths_and_schemas():
    registry = SparkTableRegistry()
    schema = object()

    registry.record("cases", "/tmp/tables/cases", schema=schema)

    assert registry.entry("cases") == ("/tmp/tables/cases", schema)
    assert registry.schema("cases") is schema
    assert registry.snapshot() == ([("cases", "/tmp/tables/cases")], {"cases": schema})


def test_table_registry_snapshot_isolated_from_concurrent_mutation():
    registry = SparkTableRegistry()

    def record(index: int) -> None:
        registry.record(f"table_{index}", f"/tmp/table_{index}", schema={"index": index})

    threads = [threading.Thread(target=record, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    paths, schemas = registry.snapshot()

    assert len(paths) == 20
    assert len(schemas) == 20
    assert dict(paths)["table_7"] == "/tmp/table_7"
    assert schemas["table_7"] == {"index": 7}
