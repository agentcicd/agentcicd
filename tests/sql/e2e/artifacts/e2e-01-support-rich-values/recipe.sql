DECLARE INPUT policy_version STRING DEFAULT 'v1';
DECLARE INPUT min_pass_score DOUBLE DEFAULT 0.70;

LOAD cases FROM '{cases_csv}'
WITH FORMAT='csv', HEADER=true, INFER_SCHEMA=true;

LOAD policy_defaults FROM '{policy_defaults_jsonl}'
WITH FORMAT='jsonl';

CREATE BATCH TABLE prepared
SELECT
  case_id,
  policy_id,
  normalize_text(customer_message) AS normalized_message,
  parse_json(policy_json) AS policy,
  CAST(priority AS INT) AS priority
FROM cases;

CREATE BATCH TABLE enriched
SELECT
  p.case_id,
  p.policy_id,
  p.normalized_message,
  p.policy,
  p.priority,
  CAST(d.threshold AS DOUBLE) AS threshold,
  d.segment
FROM prepared p
JOIN policy_defaults d
  ON p.policy_id = d.policy_id;

CREATE BATCH TABLE simulated
SELECT
  case_id,
  policy_id,
  segment,
  normalized_message,
  threshold,
  support.simulate_turn(
    case_id = case_id,
    message = normalized_message,
    policy = policy,
    policy_version = policy_version
  ) AS simulation
FROM enriched
ORDER BY case_id;

CREATE BATCH TABLE judged
WITH judged_once AS (
  SELECT
    case_id,
    policy_id,
    segment,
    simulation['intent'] AS intent,
    simulation['history'] AS history,
    threshold,
    judge.policy_adherence(
      case_id = case_id,
      history = simulation['history'],
      policy = map('policy_id', policy_id, 'segment', segment)
    ) AS adherence
  FROM simulated
)
SELECT
  case_id AS evaluation_case_id,
  segment,
  intent,
  history,
  adherence,
  CAST(adherence['score'] AS DOUBLE) AS score,
  CAST(CAST(adherence['score'] AS DOUBLE) >= min_pass_score AS BOOLEAN) AS passed
FROM judged_once;

CREATE BATCH TABLE report_rows
SELECT
  'policy_adherence' AS metric,
  evaluation_case_id AS case_id,
  score AS value,
  map('policy_version', policy_version, 'segment', CAST(segment AS STRING)) AS tags
FROM judged;

PUBLISH report_rows TO REPORTS
WITH COMPONENT='metric', CHART='table';
