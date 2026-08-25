from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.ir.statements import BatchTableStmt
from agentcicd.sql.lowering.segment_lowering import lower_statement_cells_sql


def _lower_cells_sql(script: str) -> str:
    entrypoint = EngineEntrypoint(script)
    statements = entrypoint.resolve()
    registry = entrypoint.registry()
    batch_stmt = next(statement for statement in statements if isinstance(statement, BatchTableStmt))
    return lower_statement_cells_sql(batch_stmt, registry)


def test_basic_operations_case_coalesce_cast_and_limit():
    script = """
    CREATE BATCH TABLE out
    SELECT
      CASE WHEN score > 0 THEN score ELSE 0 END AS score_norm,
      COALESCE(name, 'unknown') AS safe_name,
      CAST(age AS STRING) AS age_text
    FROM prepared
    WHERE active = true
    ORDER BY score DESC
    LIMIT 10;
    """

    lowered_sql = _lower_cells_sql(script)

    assert "CASE WHEN score.value > 0 THEN score.value ELSE 0 END" in lowered_sql
    assert "COALESCE(name.value, 'unknown')" in lowered_sql
    assert "CAST(age.value AS STRING)" in lowered_sql
    assert "active.value = TRUE" in lowered_sql
    assert "score.value ELSE score.value END DESC" in lowered_sql
    assert "LIMIT 10" in lowered_sql


def test_basic_operations_having_distinct_and_cte():
    script = """
    CREATE BATCH TABLE out
    WITH scored AS (
      SELECT DISTINCT customer_id, segment, score
      FROM prepared
    )
    SELECT segment, AVG(score) AS avg_score
    FROM scored
    GROUP BY segment
    HAVING AVG(score) > 0;
    """

    lowered_sql = _lower_cells_sql(script)

    assert "WITH scored AS" in lowered_sql
    assert "metadata.lineage" not in lowered_sql
    assert "GROUP BY __agentcicd_set_0_value, __agentcicd_set_1_value, __agentcicd_set_2_value" in lowered_sql
    assert "AVG(score.value)" in lowered_sql
    assert "segment.value" in lowered_sql
    assert "AVG(score.value) > 0" in lowered_sql


def test_basic_operations_union_all_lowers_both_branches_to_cells():
    script = """
    CREATE BATCH TABLE out
    SELECT id, score
    FROM left_scores
    UNION ALL
    SELECT id, score
    FROM right_scores;
    """

    lowered_sql = _lower_cells_sql(script)

    assert lowered_sql.count("NAMED_STRUCT('value'") == 0
    assert "id AS id" in lowered_sql
    assert "score AS score" in lowered_sql
    assert "FROM left_scores" in lowered_sql
    assert "FROM right_scores" in lowered_sql
    assert "UNION ALL" in lowered_sql


def test_basic_operations_array_map_and_struct_construction():
    script = """
    CREATE BATCH TABLE out
    SELECT
      ARRAY(score, bonus) AS score_array,
      MAP('score', score, 'bonus', bonus) AS score_map,
      NAMED_STRUCT('id', id, 'segment', segment) AS summary
    FROM prepared;
    """

    lowered_sql = _lower_cells_sql(script)

    assert "ARRAY(score.value, bonus.value)" in lowered_sql
    assert "MAP('score', score.value, 'bonus', bonus.value)" in lowered_sql
    assert "NAMED_STRUCT('id', id.value, 'segment', segment.value)" in lowered_sql
