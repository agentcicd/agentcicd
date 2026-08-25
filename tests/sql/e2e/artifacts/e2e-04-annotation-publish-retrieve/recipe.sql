LOAD judged_seed FROM '{judged_seed_parquet}'
WITH FORMAT='parquet';

CREATE BATCH TABLE annotation_candidates
SELECT
  evaluation_case_id AS case_id,
  intent,
  score,
  passed,
  map('score', CAST(err_or(score, -1.0) AS STRING), 'intent', CAST(intent AS STRING)) AS task_payload
FROM judged_seed
WHERE err_or(passed, false) = false OR is_err(score);

PUBLISH annotation_candidates TO ANNOTATION QUEUE 'policy review' AS policy_review
WITH CONSENSUS='majority';

RETRIEVE ANNOTATION RESULTS reviewed
FROM ANNOTATION REQUEST '{annotation_request_id}';

CREATE BATCH TABLE final_report
SELECT
  annotation_candidates.case_id,
  annotation_candidates.score,
  reviewed.label AS review_label,
  is_err(reviewed.label) AS review_failed
FROM annotation_candidates
LEFT JOIN reviewed ON annotation_candidates.case_id = reviewed.case_id;

CREATE BATCH TABLE review_metrics
SELECT 'annotation_candidates' AS metric, CAST(COUNT(*) AS DOUBLE) AS value
FROM annotation_candidates;

PUBLISH final_report TO DATASET 'policy-review-results';
PUBLISH review_metrics TO REPORTS WITH COMPONENT='metric';
