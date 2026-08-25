DECLARE INPUT policy_version STRING DEFAULT 'v2';
DECLARE INPUT min_priority INT DEFAULT 1;
DECLARE INPUT min_pass_score DOUBLE DEFAULT 0.70;

LOAD cases FROM '{cases_csv}'
WITH FORMAT='csv', HEADER=true, INFER_SCHEMA=true;

LOAD policies FROM '{policies_jsonl}'
WITH FORMAT='jsonl';

LOAD baseline FROM '{baseline_parquet}'
WITH FORMAT='parquet';

CREATE BATCH TABLE prepared_cases
SELECT
  case_id,
  customer_id,
  policy_id,
  CAST(priority AS INT) AS priority,
  created_day,
  normalize_text(customer_message) AS message_norm,
  LENGTH(err_or(normalize_text(customer_message), '')) AS message_length,
  CASE
    WHEN CAST(priority AS INT) >= 4 THEN 'urgent'
    WHEN CAST(priority AS INT) >= 2 THEN 'normal'
    ELSE 'low'
  END AS priority_bucket
FROM cases
WHERE CAST(priority AS INT) >= min_priority
ORDER BY CAST(priority AS INT), case_id;

CREATE BATCH TABLE active_policies
SELECT
  policy_id,
  segment,
  CAST(threshold AS DOUBLE) AS threshold,
  steps,
  array_size(steps) AS step_count
FROM policies
WHERE active = true AND array_size(steps) > 0;

CREATE BATCH TABLE case_policy_joined
SELECT
  c.case_id,
  c.customer_id,
  c.policy_id,
  c.priority,
  c.priority_bucket,
  c.message_norm,
  p.segment,
  p.threshold,
  p.steps,
  p.step_count,
  b.baseline_score,
  c.priority + p.step_count AS routing_weight
FROM prepared_cases c
JOIN active_policies p
  ON c.policy_id = p.policy_id
LEFT JOIN baseline b
  ON c.policy_id = b.policy_id
WHERE err_or(c.message_length, 0) > 0;

CREATE BATCH TABLE simulated
SELECT
  case_id,
  customer_id,
  policy_id,
  segment,
  priority_bucket,
  routing_weight,
  threshold,
  baseline_score,
  embed.text(text = err_or(message_norm, 'fallback message'), model = 'bge') AS embedding,
  support.simulate_turn(
    case_id = case_id,
    message = err_or(message_norm, 'fallback message'),
    policy = map('policy_id', policy_id, 'segment', segment),
    policy_version = policy_version
  ) AS simulation
FROM case_policy_joined;

CREATE BATCH TABLE judged
WITH judge_inputs AS (
  SELECT
    case_id,
    customer_id,
    policy_id,
    segment,
    priority_bucket,
    routing_weight,
    threshold,
    baseline_score,
    embedding,
    simulation,
    simulation['history'] AS history,
    simulation['intent'] AS intent,
    array_size(simulation['history']) AS turn_count
  FROM simulated
),
judged_raw AS (
  SELECT
    case_id,
    customer_id,
    policy_id,
    segment,
    priority_bucket,
    routing_weight,
    threshold,
    baseline_score,
    intent,
    turn_count,
    embedding,
    judge.policy_adherence(
      case_id = case_id,
      history = history,
      policy = map('policy_id', policy_id, 'segment', segment)
    ) AS adherence
  FROM judge_inputs
)
SELECT
  case_id,
  customer_id,
  policy_id,
  segment,
  priority_bucket,
  routing_weight,
  threshold,
  baseline_score,
  intent,
  turn_count,
  embedding,
  adherence,
  CAST(adherence['score'] AS DOUBLE) AS score,
  CAST(adherence['pass'] AS BOOLEAN) AS judge_pass,
  CAST(CAST(adherence['score'] AS DOUBLE) >= threshold AS BOOLEAN) AS threshold_pass,
  CAST(CAST(adherence['score'] AS DOUBLE) - err_or(baseline_score, 0.0) AS DOUBLE) AS delta_from_baseline
FROM judged_raw;

CREATE BATCH TABLE ranked_cases
SELECT
  case_id,
  customer_id,
  policy_id,
  segment,
  priority_bucket,
  score,
  threshold_pass,
  delta_from_baseline,
  ROW_NUMBER() OVER (PARTITION BY segment ORDER BY score DESC, routing_weight DESC) AS segment_rank,
  LAG(score) OVER (PARTITION BY segment ORDER BY score DESC, routing_weight DESC) AS previous_score,
  CASE
    WHEN is_err(score) THEN 'score-error'
    WHEN threshold_pass = true THEN 'pass'
    ELSE 'needs-review'
  END AS outcome
FROM judged
ORDER BY segment, segment_rank;

CREATE BATCH TABLE segment_summary
SELECT
  lower(segment) AS segment,
  COUNT(*) AS case_count,
  COUNT(score) AS scored_count,
  AVG(score) AS avg_score,
  MIN(score) AS min_score,
  MAX(score) AS max_score,
  AVG(delta_from_baseline) AS avg_delta,
  SUM(CASE WHEN threshold_pass = true THEN 1 ELSE 0 END) AS pass_count
FROM ranked_cases
GROUP BY lower(segment)
HAVING COUNT(*) > 0 AND AVG(err_or(score, 0.0)) >= min_pass_score;

CREATE BATCH TABLE annotation_candidates
SELECT
  case_id,
  customer_id,
  segment,
  score,
  outcome,
  map(
    'segment', CAST(segment AS STRING),
    'score', CAST(err_or(score, -1.0) AS STRING),
    'outcome', CAST(outcome AS STRING)
  ) AS task_payload
FROM ranked_cases
WHERE outcome = 'needs-review' OR is_err(score);

PUBLISH annotation_candidates TO ANNOTATION QUEUE 'support policy review' AS support_policy_review
WITH CONSENSUS='majority';

RETRIEVE ANNOTATION RESULTS reviewed
FROM ANNOTATION REQUEST '{annotation_request_id}';

CREATE BATCH TABLE final_case_report
SELECT
  r.case_id,
  r.customer_id,
  r.segment,
  r.score,
  r.outcome,
  reviewed.label AS review_label,
  CASE
    WHEN is_err(reviewed.label) THEN 'review-error'
    WHEN reviewed.label IS NULL THEN 'review-pending'
    ELSE reviewed.label
  END AS review_status
FROM ranked_cases r
LEFT JOIN reviewed
  ON r.case_id = reviewed.case_id;

CREATE BATCH TABLE report_metrics
SELECT
  'segment_avg_score' AS metric,
  segment AS dimension,
  avg_score AS value,
  map('case_count', CAST(case_count AS STRING), 'pass_count', CAST(pass_count AS STRING)) AS tags
FROM segment_summary
UNION ALL
SELECT
  'case_score' AS metric,
  case_id AS dimension,
  score AS value,
  map('outcome', CAST(outcome AS STRING), 'segment', CAST(segment AS STRING)) AS tags
FROM ranked_cases;

PUBLISH report_metrics TO REPORTS
WITH COMPONENT='metric', CHART='table';

PUBLISH final_case_report TO DATASET 'support-eval-final-cases';
