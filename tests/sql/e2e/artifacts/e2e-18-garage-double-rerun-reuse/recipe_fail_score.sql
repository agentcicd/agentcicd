LOAD garage_raw FROM '{garage_raw}' WITH FORMAT='parquet';

CREATE BATCH TABLE citation_examples
SELECT sample_id, passage_id, expected_label, response_label FROM garage_raw;

CREATE BATCH TABLE parsed_small_judgments
SELECT
  sample_id,
  passage_id,
  expected_label,
  CASE
    WHEN response_label = 'MALFORMED' THEN 'relevant'
    ELSE response_label
  END AS small_label
FROM citation_examples;

CREATE BATCH TABLE evaluated_citations
SELECT sample_id, passage_id, small_label = expected_label AS correct
FROM parsed_small_judgments;

CREATE BATCH TABLE score_rows
SELECT
  sample_id,
  CASE
    WHEN sample_id = 'q2' THEN raise_error('score rerun failure')
    ELSE CAST(AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END) AS STRING)
  END AS accuracy_text
FROM evaluated_citations
GROUP BY sample_id;
