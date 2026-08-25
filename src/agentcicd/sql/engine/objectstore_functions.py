from __future__ import annotations

import re
from typing import Any

from pyspark.sql.types import ArrayType, BooleanType, LongType, StringType, StructField, StructType

from agentcicd.fixtures.functions import objectstore as fixture_objectstore

DIRECTORY_ENTRY_STRUCT = StructType(
    [
        StructField("content_type", StringType(), True),
        StructField("dataset_path", StringType(), False),
        StructField("entry_type", StringType(), False),
        StructField("is_empty_dir", BooleanType(), False),
        StructField("name", StringType(), False),
        StructField("object_uri", StringType(), True),
        StructField("parent_path", StringType(), True),
        StructField("path", StringType(), False),
        StructField("schema_version", StringType(), False),
        StructField("sha256", StringType(), True),
        StructField("size_bytes", LongType(), True),
    ]
)

is_directory_format = fixture_objectstore.is_directory_format
normalize_filter_patterns = fixture_objectstore.normalize_filter_patterns
compile_filter_patterns = fixture_objectstore.compile_filter_patterns

_OBJECTSTORE_REPLACEMENTS = {
    "objectstore.write_json": "objectstore_write_json",
    "objectstore.write_text": "objectstore_write_text",
    "objectstore.read_json": "objectstore_read_json",
    "objectstore.read_text": "objectstore_read_text",
    "objectstore.exists": "objectstore_exists",
    "objectstore.find": "objectstore_find",
    "objectstore.glob": "objectstore_glob",
    "objectstore.entry": "objectstore_entry",
}
_OBJECTSTORE_CALL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])objectstore[._]"
    r"(write_json|write_text|read_json|read_text|exists|find|glob|entry)\s*\(",
    flags=re.IGNORECASE,
)


def register_objectstore_functions(spark_session: Any) -> None:
    if not hasattr(spark_session, "udf"):
        return
    spark_session.udf.register("objectstore_write_json", fixture_objectstore.write_json, ArrayType(DIRECTORY_ENTRY_STRUCT))
    spark_session.udf.register("objectstore_write_text", fixture_objectstore.write_text, ArrayType(DIRECTORY_ENTRY_STRUCT))
    spark_session.udf.register("objectstore_read_json", fixture_objectstore.read_json, StringType())
    spark_session.udf.register("objectstore_read_text", fixture_objectstore.read_text, StringType())
    spark_session.udf.register("objectstore_exists", fixture_objectstore.exists, BooleanType())
    spark_session.udf.register("objectstore_find", fixture_objectstore.find, DIRECTORY_ENTRY_STRUCT)
    spark_session.udf.register("objectstore_glob", fixture_objectstore.glob, ArrayType(DIRECTORY_ENTRY_STRUCT))
    spark_session.udf.register("objectstore_entry", fixture_objectstore.entry, DIRECTORY_ENTRY_STRUCT)


def rewrite_objectstore_function_calls(sql: str) -> str:
    rewritten = sql
    for surface_name, registered_name in _OBJECTSTORE_REPLACEMENTS.items():
        rewritten = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(surface_name)}\s*\(",
            f"{registered_name}(",
            rewritten,
            flags=re.IGNORECASE,
        )
    return rewritten


def sql_uses_objectstore_functions(sql: str) -> bool:
    return _OBJECTSTORE_CALL_PATTERN.search(sql) is not None
