from __future__ import annotations

import re
from decimal import Decimal

import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.engine.spark_udf import (
    _column_to_pylist,
    _normalize_nulls,
    _to_pyarrow_columns,
)
from agentcicd.sql.wrapped_validation import WrappedValidationError


def _variant(json_text: str):
    pyspark_types = pytest.importorskip("pyspark.sql.types")
    return pyspark_types.VariantVal.parseJson(json_text)


def _strip_none_fields(value):
    if isinstance(value, dict):
        return {key: _strip_none_fields(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_strip_none_fields(item) for item in value]
    return value


def _assert_no_native_variant_brackets(lowered: str, *column_refs: str) -> None:
    for column_ref in column_refs:
        assert f"{column_ref}['" not in lowered
        assert f'{column_ref}["' not in lowered
        assert f"{column_ref}[0]" not in lowered


@pytest.mark.parametrize(
    ("json_text", "expected"),
    [
        ('{"type":"json_object","nested":{"ok":true}}', {"type": "json_object", "nested": {"ok": True}}),
        ('["a", {"b": 2}, null]', ["a", {"b": 2}, None]),
        ("123", 123),
        ("null", None),
    ],
)
def test_normalize_nulls_converts_variantval_to_plain_python(json_text, expected):
    normalized = _normalize_nulls(_variant(json_text))
    assert normalized == expected


def test_column_to_pylist_handles_scalar_decimal():
    assert _column_to_pylist(Decimal("0.0")) == [Decimal("0.0")]


@pytest.mark.parametrize(
    ("column_factory", "expected"),
    [
        (
            lambda variant_a, variant_b: [variant_a, variant_b],
            [{"type": "json_object"}, {"items": [1, 2, 3]}],
        ),
        (
            lambda variant_a, variant_b: pytest.importorskip("pandas").Series([variant_a, variant_b]),
            [{"type": "json_object"}, {"items": [1, 2, 3]}],
        ),
        (
            lambda variant_a, variant_b: pytest.importorskip("pandas").DataFrame(
                [{"payload": variant_a}, {"payload": variant_b}]
            ),
            [{"payload": {"type": "json_object"}}, {"payload": {"items": [1, 2, 3]}}],
        ),
    ],
)
def test_column_to_pylist_handles_variant_inputs_across_column_shapes(column_factory, expected):
    variant_a = _variant('{"type":"json_object"}')
    variant_b = _variant('{"items":[1,2,3]}')

    normalized = _column_to_pylist(column_factory(variant_a, variant_b))

    assert normalized == expected


@pytest.mark.parametrize(
    ("columns_factory", "expected"),
    [
        (
            lambda variant_a, variant_b: ([variant_a, variant_b],),
            [[{"type": "json_object"}, {"items": [1, 2, 3]}]],
        ),
        (
            lambda variant_a, variant_b: (
                pytest.importorskip("pandas").Series([variant_a, variant_b]),
                pytest.importorskip("pandas").Series([variant_b, variant_a]),
            ),
            [
                [{"type": "json_object"}, {"items": [1, 2, 3]}],
                [{"items": [1, 2, 3]}, {"type": "json_object"}],
            ],
        ),
    ],
)
def test_to_pyarrow_columns_accepts_variant_inputs(columns_factory, expected):
    pytest.importorskip("pyarrow")

    variant_a = _variant('{"type":"json_object"}')
    variant_b = _variant('{"items":[1,2,3]}')

    arrays = _to_pyarrow_columns(columns_factory(variant_a, variant_b))

    assert [_strip_none_fields(array.to_pylist()) for array in arrays] == expected


def test_cell_lowering_casts_variant_path_before_safe_parse_json():
    script = """
    CREATE BATCH TABLE out
    SELECT parse_json(payload['choices'][0]['message']['content'])['score'] AS score
    FROM prepared;
    """

    lowered = EngineEntrypoint(script, external_tables=["prepared"]).lower_script(include_cells=True)[0]

    assert "TRY_PARSE_JSON(CAST(" in lowered


def test_cell_lowering_casts_qualified_variant_path_before_safe_parse_json():
    script = """
    CREATE BATCH TABLE judged_rows
    SELECT
      case_id,
      parse_json(response_json) AS judge_response
    FROM raw_judgments;

    CREATE BATCH TABLE evaluated_rows
    SELECT
      d.case_id,
      CAST(parse_json(j.judge_response['choices'][0]['message']['content'])['policy_score'] AS DOUBLE) AS score
    FROM deterministic_checks d
    JOIN judged_rows j ON d.case_id = j.case_id;
    """

    lowered = EngineEntrypoint(
        script,
        external_tables=["raw_judgments", "deterministic_checks"],
    ).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(j.judge_response.value, '$.choices[0].message.content')" in lowered
    assert (
        "TRY_PARSE_JSON(CAST(TRY_CAST(TRY_VARIANT_GET(j.judge_response.value, '$.choices[0].message.content') "
        "AS STRING) AS STRING))"
    ) in lowered
    assert "j.judge_response.value['choices']" not in lowered


def test_json_path_lowering_expands_local_sql_function_inside_parse_json():
    script = r"""
    CREATE FUNCTION strip_json_fence(content STRING)
    RETURNS STRING
    RETURN trim(
      regexp_replace(
        regexp_replace(
          trim(coalesce(content, '')),
          '(?is)^\\x60\\x60\\x60\\s*json\\s*',
          ''
        ),
        '(?is)\\s*\\x60\\x60\\x60\\s*$',
        ''
      )
    );

    CREATE BATCH TABLE scores
    WITH parsed AS (
      SELECT
        CAST(
          parse_json(strip_json_fence(CAST(o.llm_response['choices'][0]['message']['content'] AS STRING)))['score']
          AS DOUBLE
        ) AS score
      FROM raw_outputs o
    )
    SELECT score FROM parsed;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_outputs"]).lower_script(include_cells=True)[0]

    assert "STRIP_JSON_FENCE" not in lowered.upper()
    assert "REGEXP_REPLACE" in lowered
    assert "TRY_VARIANT_GET(TRY_PARSE_JSON(" in lowered


def test_cell_lowering_qualified_variant_path_without_parse_json():
    script = """
    CREATE BATCH TABLE payloads
    SELECT
      case_id,
      parse_json(response_json) AS payload
    FROM raw_payloads;

    CREATE BATCH TABLE extracted
    SELECT
      p.case_id,
      CAST(p.payload['result']['final_text'] AS STRING) AS final_text
    FROM payloads p;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_payloads"]).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(p.payload.value, '$.result.final_text')" in lowered
    assert "p.payload.value['result']" not in lowered


def test_cell_lowering_does_not_treat_same_named_non_variant_alias_as_variant():
    script = """
    CREATE BATCH TABLE left_rows
    SELECT
      case_id,
      parse_json(response_json) AS payload
    FROM raw_payloads;

    CREATE BATCH TABLE right_rows
    SELECT
      case_id,
      payload
    FROM raw_strings;

    CREATE BATCH TABLE extracted
    SELECT
      l.case_id,
      CAST(l.payload['result']['final_text'] AS STRING) AS final_text,
      CAST(r.payload['plain'] AS STRING) AS raw_plain
    FROM left_rows l
    JOIN right_rows r ON l.case_id = r.case_id;
    """

    lowered = EngineEntrypoint(
        script,
        external_tables=["raw_payloads", "raw_strings"],
    ).lower_script(include_cells=True)[2]

    assert "TRY_VARIANT_GET(l.payload.value, '$.result.final_text')" in lowered
    assert "r.payload.value['plain']" in lowered
    assert "TRY_VARIANT_GET(r.payload.value" not in lowered


def test_cell_lowering_qualified_runtime_variant_path():
    script = """
    CREATE BATCH TABLE judged_rows
    SELECT
      case_id,
      remote.judge(input = prompt) AS judge_response
    FROM prompts;

    CREATE BATCH TABLE evaluated_rows
    SELECT
      j.case_id,
      CAST(parse_json(j.judge_response['choices'][0]['message']['content'])['helpfulness_score'] AS DOUBLE) AS score
    FROM judged_rows j;
    """

    lowered = EngineEntrypoint(
        script,
        external_tables=["prompts"],
        registered_functions=[
            {
                "name": "remote_judge",
                "type": "py",
                "call_name": "remote.judge",
                "runtime_alias": "remote_judge",
                "signature": {"parameters": [{"name": "input", "type_sql": "ANY"}]},
                "output_schema": {"type": "json"},
            }
        ],
    ).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(j.judge_response.value, '$.choices[0].message.content')" in lowered
    assert "j.judge_response.value['choices']" not in lowered


def test_cell_lowering_handles_json_string_inside_json_string_inside_variant():
    script = """
    CREATE BATCH TABLE judged_rows
    SELECT
      case_id,
      parse_json(response_json) AS judge_response
    FROM raw_judgments;

    CREATE BATCH TABLE evaluated_rows
    SELECT
      j.case_id,
      CAST(
        parse_json(
          CAST(
            parse_json(
              CAST(j.judge_response['choices'][0]['message']['content'] AS STRING)
            )['nested_result_json']
            AS STRING
          )
        )['metrics']['policy_score']
        AS DOUBLE
      ) AS policy_score
    FROM judged_rows j;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_judgments"]).lower_script(include_cells=True)[1]

    assert "TRY_PARSE_JSON" in lowered
    assert "TRY_VARIANT_GET" in lowered
    assert "'$.nested_result_json'" in lowered
    assert "'$.metrics.policy_score'" in lowered
    _assert_no_native_variant_brackets(lowered, "j.judge_response.value")


def test_cell_lowering_handles_mixed_scalar_casts_from_qualified_variant():
    script = """
    CREATE BATCH TABLE payloads
    SELECT
      case_id,
      parse_json(payload_json) AS payload
    FROM raw_payloads;

    CREATE BATCH TABLE typed
    SELECT
      p.case_id,
      CAST(p.payload['numbers']['score'] AS DOUBLE) AS score,
      CAST(p.payload['numbers']['count'] AS INT) AS count,
      CAST(p.payload['flags']['passed'] AS BOOLEAN) AS passed,
      CAST(p.payload['labels'][0] AS STRING) AS first_label
    FROM payloads p;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_payloads"]).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(p.payload.value, '$.numbers.score')" in lowered
    assert "TRY_VARIANT_GET(p.payload.value, '$.numbers.count')" in lowered
    assert "TRY_VARIANT_GET(p.payload.value, '$.flags.passed')" in lowered
    assert "TRY_VARIANT_GET(p.payload.value, '$.labels[0]')" in lowered
    assert "TRY_CAST(" in lowered
    _assert_no_native_variant_brackets(lowered, "p.payload.value")


def test_cell_lowering_handles_variant_paths_in_case_and_boolean_predicates():
    script = """
    CREATE BATCH TABLE payloads
    SELECT
      case_id,
      parse_json(payload_json) AS payload
    FROM raw_payloads;

    CREATE BATCH TABLE scored
    SELECT
      p.case_id,
      CASE
        WHEN CAST(p.payload['flags']['blocked'] AS BOOLEAN) THEN 0.0
        WHEN CAST(p.payload['metrics']['score'] AS DOUBLE) >= 0.8 THEN 1.0
        ELSE 0.5
      END AS normalized_score
    FROM payloads p
    WHERE CAST(p.payload['flags']['eligible'] AS BOOLEAN) = true;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_payloads"]).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(p.payload.value, '$.flags.blocked')" in lowered
    assert "TRY_VARIANT_GET(p.payload.value, '$.metrics.score')" in lowered
    assert "TRY_VARIANT_GET(p.payload.value, '$.flags.eligible')" in lowered
    assert ">= 0.8" in lowered
    _assert_no_native_variant_brackets(lowered, "p.payload.value")


def test_cell_lowering_handles_coalesce_and_arithmetic_over_variant_casts():
    script = """
    CREATE BATCH TABLE payloads
    SELECT
      case_id,
      parse_json(payload_json) AS payload
    FROM raw_payloads;

    CREATE BATCH TABLE scored
    SELECT
      p.case_id,
      coalesce(CAST(p.payload['scores']['judge'] AS DOUBLE), 0.0) * 0.7
        + coalesce(CAST(p.payload['scores']['deterministic'] AS DOUBLE), 0.0) * 0.3 AS blended_score
    FROM payloads p;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_payloads"]).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(p.payload.value, '$.scores.judge')" in lowered
    assert "TRY_VARIANT_GET(p.payload.value, '$.scores.deterministic')" in lowered
    assert "* 0.7" in lowered
    assert "* 0.3" in lowered
    _assert_no_native_variant_brackets(lowered, "p.payload.value")


def test_cell_lowering_handles_variant_arrays_and_size():
    script = """
    CREATE BATCH TABLE payloads
    SELECT
      case_id,
      parse_json(payload_json) AS payload
    FROM raw_payloads;

    CREATE BATCH TABLE extracted
    SELECT
      p.case_id,
      size(p.payload['events']) AS event_count,
      CAST(p.payload['events'][1]['name'] AS STRING) AS second_event_name,
      CAST(p.payload['events'][0]['scores'][1] AS DOUBLE) AS second_score
    FROM payloads p;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_payloads"]).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(p.payload.value, '$.events')" in lowered
    assert "TRY_VARIANT_GET(p.payload.value, '$.events[1].name')" in lowered
    assert "TRY_VARIANT_GET(p.payload.value, '$.events[0].scores[1]')" in lowered
    _assert_no_native_variant_brackets(lowered, "p.payload.value")


def test_cell_lowering_handles_negative_variant_array_index_in_wrapped_mode():
    script = """
    CREATE BATCH TABLE payloads
    SELECT
      case_id,
      parse_json(payload_json) AS payload
    FROM raw_payloads;

    CREATE BATCH TABLE extracted
    SELECT
      p.case_id,
      CAST(p.payload['events'][-1]['name'] AS STRING) AS final_event_name
    FROM payloads p;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_payloads"]).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(p.payload.value, '$.events[-1].name')" in lowered
    _assert_no_native_variant_brackets(lowered, "p.payload.value")


def test_cell_lowering_handles_aggregate_err_or_over_qualified_variant_scores():
    script = """
    CREATE BATCH TABLE payloads
    SELECT
      case_id,
      parse_json(payload_json) AS payload
    FROM raw_payloads;

    CREATE BATCH TABLE metrics
    SELECT
      'overall' AS metric,
      coalesce(avg(err_or(CAST(p.payload['scores']['overall'] AS DOUBLE), NULL)), 0.0) AS value
    FROM payloads p;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_payloads"]).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(p.payload.value, '$.scores.overall')" in lowered
    assert "AVG(" in lowered
    assert "CASE WHEN SIZE(" in lowered
    _assert_no_native_variant_brackets(lowered, "p.payload.value")


def test_cell_lowering_handles_variant_access_through_cte_alias():
    script = """
    CREATE BATCH TABLE payloads
    SELECT
      case_id,
      parse_json(payload_json) AS payload
    FROM raw_payloads;

    CREATE BATCH TABLE extracted
    WITH filtered AS (
      SELECT case_id, payload
      FROM payloads
      WHERE CAST(payload['flags']['eligible'] AS BOOLEAN) = true
    )
    SELECT
      f.case_id,
      CAST(f.payload['result']['answer'] AS STRING) AS answer
    FROM filtered f;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_payloads"]).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(payload.value, '$.flags.eligible')" in lowered
    assert "TRY_VARIANT_GET(f.payload.value, '$.result.answer')" in lowered
    _assert_no_native_variant_brackets(lowered, "payload.value", "f.payload.value")


def test_cell_lowering_handles_explicit_try_variant_get_then_parse_json():
    script = """
    CREATE BATCH TABLE judged_rows
    SELECT
      case_id,
      parse_json(response_json) AS judge_response
    FROM raw_judgments;

    CREATE BATCH TABLE evaluated_rows
    SELECT
      j.case_id,
      CAST(
        parse_json(CAST(try_variant_get(j.judge_response, '$.choices[0].message.content') AS STRING))['score']
        AS DOUBLE
      ) AS score
    FROM judged_rows j;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_judgments"]).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(j.judge_response.value, '$.choices[0].message.content')" in lowered
    assert "TRY_PARSE_JSON(CAST(TRY_CAST(TRY_VARIANT_GET(j.judge_response.value, '$.choices[0].message.content') AS STRING) AS STRING))" in lowered
    assert "'$.score'" in lowered
    _assert_no_native_variant_brackets(lowered, "j.judge_response.value")


def test_cell_lowering_rewrites_tolerant_get_on_variant_to_try_variant_get():
    script = """
    CREATE BATCH TABLE judged_rows
    SELECT
      case_id,
      parse_json(response_json) AS judge_response
    FROM raw_judgments;

    CREATE BATCH TABLE usage_rows
    SELECT
      case_id,
      CAST(get(get(judge_response, 'usage'), 'prompt_tokens') AS DOUBLE) AS prompt_tokens
    FROM judged_rows;
    """

    lowered = EngineEntrypoint(script, external_tables=["raw_judgments"]).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(TRY_VARIANT_GET(judge_response.value, '$.usage'), '$.prompt_tokens')" in lowered
    assert "AGENTCICD_JSON_ACCESS_ERROR" not in lowered


def test_cell_lowering_keeps_tolerant_get_native_for_collections():
    script = """
    CREATE BATCH TABLE aligned
    SELECT
      case_id,
      get(labels, pos) AS aligned_label
    FROM label_rows;
    """

    lowered = EngineEntrypoint(script, external_tables=["label_rows"]).lower_script(include_cells=True)[0]

    assert "GET(labels.value, pos.value)" in lowered
    assert "TRY_VARIANT_GET" not in lowered


def test_cell_lowering_preserves_tolerant_get_variant_output_through_union():
    script = """
    CREATE BATCH TABLE envelopes
    SELECT
      case_id,
      get(parse_json(left_response), 'usage') AS usage
    FROM left_responses
    UNION ALL
    SELECT
      case_id,
      get(parse_json(right_response), 'usage') AS usage
    FROM right_responses;

    CREATE BATCH TABLE usage_rows
    SELECT
      case_id,
      CAST(get(usage, 'prompt_tokens') AS DOUBLE) AS prompt_tokens
    FROM envelopes;
    """

    lowered = EngineEntrypoint(
        script,
        external_tables=["left_responses", "right_responses"],
    ).lower_script(include_cells=True)[1]

    assert "TRY_VARIANT_GET(usage.value, '$.prompt_tokens')" in lowered
    assert not re.search(r"(?<!VARIANT_)\bGET\(usage\.value", lowered)
    assert "AGENTCICD_JSON_ACCESS_ERROR" not in lowered


def test_cell_lowering_preserves_runtime_variant_alias_for_nested_brackets():
    script = """
    CREATE BATCH TABLE out
    SELECT
      remote.simulate(intent = user_message) AS simulation_result,
      CAST(simulation_result['result']['response'] AS STRING) AS response
    FROM prepared;
    """

    lowered = EngineEntrypoint(
        script,
        external_tables=["prepared"],
        registered_functions=[
            {
                "name": "remote_simulate",
                "type": "py",
                "call_name": "remote.simulate",
                "runtime_alias": "remote_simulate",
                "signature": {"parameters": [{"name": "intent", "type_sql": "ANY"}]},
                "output_schema": {"type": "json"},
            }
        ],
    ).lower_script(include_cells=True)[0]

    assert "TRY_VARIANT_GET(simulation_result.value, '$.result.response')" in lowered
    assert "simulation_result.value['result']" not in lowered
