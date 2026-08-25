import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend
from agentcicd.sql.surface.top_level_parser import TopLevelParser


class _FakeSpark:
    class _Read:
        def format(self, value):
            self._format = value
            return self

        def option(self, key, value):
            return self

        def load(self, path):
            raise AssertionError("load should not be reached for unsupported formats")

    def __init__(self):
        self.read = self._Read()


def test_invalid_load_option_fragment_raises():
    script = """
    LOAD sales FROM 's3://bucket/sales'
    WITH FORMAT='csv', BROKEN;
    """

    with pytest.raises(ValueError, match="Invalid option fragment"):
        TopLevelParser(script).parse()


def test_spark_backend_rejects_unsupported_load_format(tmp_path):
    backend = SparkExecutionBackend(_FakeSpark(), working_dir=str(tmp_path))

    with pytest.raises(ValueError, match="Unsupported LOAD format"):
        backend.load_table("raw", "/tmp/raw.xyz", {"format": "xyz"})


def test_validation_reports_invalid_lowered_sql():
    script = """
    CREATE BATCH TABLE out
    SELECT ) FROM prepared;
    """

    with pytest.raises(Exception):
        EngineEntrypoint(script).parse()
