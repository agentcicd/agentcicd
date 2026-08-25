LOAD current_scores FROM '{current_scores_parquet}' WITH FORMAT='parquet';
LOAD baseline_scores FROM '{baseline_scores_parquet}' WITH FORMAT='parquet';

CREATE BATCH TABLE joined_scores
SELECT
  c.case_id,
  c.segment,
  c.score AS current_score,
  b.score AS baseline_score,
  c.score - b.score AS delta
FROM current_scores c
LEFT JOIN baseline_scores b
  ON c.case_id = b.case_id;

CREATE BATCH TABLE segment_summary
SELECT
  segment,
  COUNT(*) AS row_count,
  COUNT(score) AS scored_count,
  AVG(score) AS avg_score,
  MIN(score) AS min_score,
  MAX(score) AS max_score
FROM current_scores
GROUP BY segment
HAVING COUNT(*) > 0
ORDER BY segment;
