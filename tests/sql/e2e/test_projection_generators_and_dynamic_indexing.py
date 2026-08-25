from __future__ import annotations
from pathlib import Path

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.spark_backend import SparkExecutionBackend


def _execute(sql: str, *, spark, tmp_path: Path) -> None:
    backend = SparkExecutionBackend(spark, working_dir=str(tmp_path))
    EngineEntrypoint(sql).execute(backend, include_cells=True)


def _read_values(spark, tmp_path: Path, table: str, *columns: str) -> list[tuple]:
    rows = spark.read.parquet(str(tmp_path / "tables" / table)).collect()
    values = [tuple(row[column].value for column in columns) for row in rows]
    return sorted(values, key=lambda item: tuple("" if value is None else str(value) for value in item))


def _read_error_codes(spark, tmp_path: Path, table: str, column: str) -> list[list[str]]:
    rows = spark.read.parquet(str(tmp_path / "tables" / table)).collect()
    return sorted([[error.code for error in row[column].metadata.errors] for row in rows])


def test_projection_posexplode_with_sibling_dynamic_array_indexing(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE source_rows
    SELECT
      'case-1' AS case_id,
      array('alpha', 'beta') AS items,
      array('A', 'B') AS labels
    UNION ALL
    SELECT
      'case-2' AS case_id,
      array('gamma') AS items,
      array('C') AS labels;

    CREATE BATCH TABLE exploded_rows
    SELECT
      case_id,
      posexplode(items) AS (pos, item),
      labels[pos] AS aligned_label
    FROM source_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "exploded_rows", "case_id", "pos", "item", "aligned_label") == [
        ("case-1", 0, "alpha", "A"),
        ("case-1", 1, "beta", "B"),
        ("case-2", 0, "gamma", "C"),
    ]


def test_projection_explode_after_parse_json_static_path(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE raw_rows
    SELECT
      'case-1' AS case_id,
      '{"citations":[{"passage_id":"cite_1","label":"ok"},{"passage_id":"cite_2","label":"bad"}]}' AS payload;

    CREATE BATCH TABLE parsed_rows
    SELECT
      case_id,
      parse_json(payload) AS payload_json
    FROM raw_rows;

    CREATE BATCH TABLE exploded_rows
    SELECT
      case_id,
      explode(payload_json['citations']) AS citation,
      CAST(citation['passage_id'] AS STRING) AS passage_id,
      CAST(citation['label'] AS STRING) AS label
    FROM parsed_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "exploded_rows", "case_id", "passage_id", "label") == [
        ("case-1", "cite_1", "ok"),
        ("case-1", "cite_2", "bad"),
    ]


def test_dynamic_array_index_out_of_range_becomes_agentcicd_error(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE source_rows
    SELECT
      'case-1' AS case_id,
      array('only') AS labels,
      2 AS pos;

    CREATE BATCH TABLE indexed_rows
    SELECT
      case_id,
      labels[pos] AS aligned_label
    FROM source_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "indexed_rows", "case_id", "aligned_label") == [
        ("case-1", None),
    ]
    assert _read_error_codes(local_spark, tmp_path, "indexed_rows", "aligned_label") == [
        ["AGENTCICD_ACCESS_ERROR"],
    ]


def test_explicit_get_out_of_range_returns_null_without_error(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE source_rows
    SELECT
      'case-1' AS case_id,
      array('only') AS labels,
      2 AS pos;

    CREATE BATCH TABLE indexed_rows
    SELECT
      case_id,
      get(labels, pos) AS aligned_label
    FROM source_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "indexed_rows", "case_id", "aligned_label") == [
        ("case-1", None),
    ]
    assert _read_error_codes(local_spark, tmp_path, "indexed_rows", "aligned_label") == [
        [],
    ]


def test_projection_posexplode_sibling_reference_before_generator(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE source_rows
    SELECT
      'case-1' AS case_id,
      array('alpha', 'beta') AS items,
      array('A', 'B') AS labels;

    CREATE BATCH TABLE exploded_rows
    SELECT
      case_id,
      labels[pos] AS aligned_label,
      posexplode(items) AS (pos, item)
    FROM source_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "exploded_rows", "case_id", "pos", "item", "aligned_label") == [
        ("case-1", 0, "alpha", "A"),
        ("case-1", 1, "beta", "B"),
    ]


def test_missing_variant_path_becomes_agentcicd_error(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE raw_rows
    SELECT
      'case-1' AS case_id,
      '{"other":[]}' AS payload;

    CREATE BATCH TABLE extracted_rows
    SELECT
      case_id,
      CAST(parse_json(payload)['choices'][0]['message']['content'] AS STRING) AS content
    FROM raw_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "extracted_rows", "case_id", "content") == [
        ("case-1", None),
    ]
    assert _read_error_codes(local_spark, tmp_path, "extracted_rows", "content") == [
        ["AGENTCICD_JSON_ACCESS_ERROR"],
    ]


def test_variant_json_null_value_does_not_become_access_error(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE raw_rows
    SELECT
      'case-1' AS case_id,
      '{"choices":[{"message":{"content":null}}]}' AS payload;

    CREATE BATCH TABLE extracted_rows
    SELECT
      case_id,
      parse_json(payload)['choices'][0]['message']['content'] AS content
    FROM raw_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_error_codes(local_spark, tmp_path, "extracted_rows", "content") == [
        [],
    ]


def test_variant_null_base_becomes_access_error(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE raw_rows
    SELECT
      'case-1' AS case_id,
      'null' AS payload;

    CREATE BATCH TABLE extracted_rows
    SELECT
      case_id,
      parse_json(payload)['choices'] AS choices
    FROM raw_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "extracted_rows", "case_id", "choices") == [
        ("case-1", None),
    ]
    assert _read_error_codes(local_spark, tmp_path, "extracted_rows", "choices") == [
        ["AGENTCICD_JSON_ACCESS_ERROR"],
    ]


def test_explicit_try_variant_get_missing_path_returns_null_without_error(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE raw_rows
    SELECT
      'case-1' AS case_id,
      '{"other":[]}' AS payload;

    CREATE BATCH TABLE extracted_rows
    SELECT
      case_id,
      CAST(try_variant_get(parse_json(payload), '$.choices[0].message.content') AS STRING) AS content
    FROM raw_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "extracted_rows", "case_id", "content") == [
        ("case-1", None),
    ]
    assert _read_error_codes(local_spark, tmp_path, "extracted_rows", "content") == [
        [],
    ]


def test_static_then_dynamic_variant_object_key_access(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE raw_rows
    SELECT
      'case-1' AS case_id,
      'question41' AS question_id,
      '{"answers":{"question41":{"choice":"C","value":82950},"question50":{"choice":"A","value":12}}}' AS payload
    UNION ALL
    SELECT
      'case-2' AS case_id,
      'question50' AS question_id,
      '{"answers":{"question41":{"choice":"D","value":0},"question50":{"choice":"B","value":5753961}}}' AS payload;

    CREATE BATCH TABLE extracted_rows
    SELECT
      case_id,
      question_id,
      CAST(parse_json(payload)['answers'][question_id]['choice'] AS STRING) AS predicted_choice,
      CAST(parse_json(payload)['answers'][question_id]['value'] AS STRING) AS predicted_value
    FROM raw_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(
        local_spark,
        tmp_path,
        "extracted_rows",
        "case_id",
        "question_id",
        "predicted_choice",
        "predicted_value",
    ) == [
        ("case-1", "question41", "C", "82950"),
        ("case-2", "question50", "B", "5753961"),
    ]
    assert _read_error_codes(local_spark, tmp_path, "extracted_rows", "predicted_choice") == [
        [],
        [],
    ]


def test_missing_dynamic_variant_object_key_becomes_agentcicd_error(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE raw_rows
    SELECT
      'case-1' AS case_id,
      'question99' AS question_id,
      '{"answers":{"question41":{"choice":"C"}}}' AS payload;

    CREATE BATCH TABLE extracted_rows
    SELECT
      case_id,
      CAST(parse_json(payload)['answers'][question_id]['choice'] AS STRING) AS predicted_choice
    FROM raw_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "extracted_rows", "case_id", "predicted_choice") == [
        ("case-1", None),
    ]
    assert _read_error_codes(local_spark, tmp_path, "extracted_rows", "predicted_choice") == [
        ["AGENTCICD_JSON_ACCESS_ERROR"],
    ]


def test_missing_projection_variant_field_becomes_agentcicd_error(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE raw_rows
    SELECT
      'case-1' AS case_id,
      '{"citations":[{"label":"missing-id"}]}' AS payload;

    CREATE BATCH TABLE parsed_rows
    SELECT
      case_id,
      parse_json(payload) AS payload_json
    FROM raw_rows;

    CREATE BATCH TABLE exploded_rows
    SELECT
      case_id,
      explode(payload_json['citations']) AS judgment,
      CAST(judgment['passage_id'] AS STRING) AS passage_id
    FROM parsed_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "exploded_rows", "case_id", "passage_id") == [
        ("case-1", None),
    ]
    assert _read_error_codes(local_spark, tmp_path, "exploded_rows", "passage_id") == [
        ["AGENTCICD_JSON_ACCESS_ERROR"],
    ]


def test_explicit_try_variant_get_projection_missing_field_returns_null_without_error(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE raw_rows
    SELECT
      'case-1' AS case_id,
      '{"citations":[{"label":"missing-id"}]}' AS payload;

    CREATE BATCH TABLE parsed_rows
    SELECT
      case_id,
      parse_json(payload) AS payload_json
    FROM raw_rows;

    CREATE BATCH TABLE exploded_rows
    SELECT
      case_id,
      explode(payload_json['citations']) AS judgment,
      CAST(try_variant_get(judgment, '$.passage_id') AS STRING) AS passage_id
    FROM parsed_rows;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "exploded_rows", "case_id", "passage_id") == [
        ("case-1", None),
    ]
    assert _read_error_codes(local_spark, tmp_path, "exploded_rows", "passage_id") == [
        [],
    ]


def test_projection_generator_inside_cte(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE source_rows
    SELECT
      'case-1' AS case_id,
      array('alpha', 'beta') AS items,
      array('A', 'B') AS labels;

    CREATE BATCH TABLE scored_rows
    WITH exploded AS (
      SELECT
        case_id,
        labels,
        posexplode(items) AS (pos, item)
      FROM source_rows
    )
    SELECT
      case_id,
      item,
      labels[pos] AS aligned_label
    FROM exploded;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "scored_rows", "case_id", "item", "aligned_label") == [
        ("case-1", "alpha", "A"),
        ("case-1", "beta", "B"),
    ]


def test_projection_generator_inside_nested_query(local_spark, tmp_path):
    sql = """
    CREATE BATCH TABLE source_rows
    SELECT
      'case-1' AS case_id,
      array('alpha', 'beta') AS items,
      array('A', 'B') AS labels;

    CREATE BATCH TABLE scored_rows
    SELECT
      case_id,
      item,
      labels[pos] AS aligned_label
    FROM (
      SELECT
        case_id,
        labels,
        posexplode(items) AS (pos, item)
      FROM source_rows
    ) exploded;
    """

    _execute(sql, spark=local_spark, tmp_path=tmp_path)

    assert _read_values(local_spark, tmp_path, "scored_rows", "case_id", "item", "aligned_label") == [
        ("case-1", "alpha", "A"),
        ("case-1", "beta", "B"),
    ]
