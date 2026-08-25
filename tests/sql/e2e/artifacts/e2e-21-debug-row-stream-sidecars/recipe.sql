LOAD cases FROM '{cases_csv}'
WITH FORMAT='csv', HEADER=true, INFER_SCHEMA=true;

CREATE BATCH TABLE evaluated
SELECT
  case_id,
  CAST(score AS DOUBLE) AS score,
  CAST(score >= 0.7 AS BOOLEAN) AS passed,
  note
FROM cases
ORDER BY case_id;
