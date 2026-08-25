LOAD garage_raw FROM '{garage_raw}' WITH FORMAT='parquet';

CREATE BATCH TABLE citation_examples
SELECT
  case_id,
  sample_id,
  question,
  passage_id,
  passage_text,
  expected_label,
  judge_version
FROM garage_raw;

CREATE BATCH TABLE judged_citations
SELECT
  case_id,
  sample_id,
  passage_id,
  expected_label,
  judge.citation(
    case_id = case_id,
    sample_id = sample_id,
    passage_id = passage_id,
    passage_text = passage_text,
    judge_version = judge_version
  ) AS judgment
FROM citation_examples;

CREATE BATCH TABLE evaluated_citations
SELECT
  case_id,
  sample_id,
  passage_id,
  expected_label,
  CAST(judgment['relevance_label'] AS STRING) AS predicted_label,
  CAST(judgment['confidence'] AS DOUBLE) AS confidence
FROM judged_citations;

CREATE BATCH TABLE score_rows
SELECT
  sample_id,
  COUNT(*) AS citation_count,
  AVG(CASE WHEN predicted_label = expected_label THEN 1.0 ELSE 0.0 END) AS accuracy
FROM evaluated_citations
GROUP BY sample_id;

