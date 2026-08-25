LOAD raw_scores FROM '{scores_csv}'
WITH FORMAT='csv', HEADER=true, INFER_SCHEMA=true;

CREATE BATCH TABLE parsed
SELECT
  row_id,
  CAST(score_text AS DOUBLE) AS score_cast,
  parse_json(payload_text) AS payload,
  extract_json_score(payload_text) AS score_from_udf
FROM raw_scores;

CREATE BATCH TABLE recovered
SELECT
  row_id,
  err_or(score_cast, -1.0) AS score_cast_safe,
  err_or(score_from_udf, -2.0) AS score_udf_safe,
  is_err(score_cast) AS cast_failed,
  is_err(score_from_udf) AS udf_failed
FROM parsed;
