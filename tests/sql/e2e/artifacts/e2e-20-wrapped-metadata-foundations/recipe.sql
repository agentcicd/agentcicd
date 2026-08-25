LOAD metadata_cases FROM '{metadata_cases_csv}'
WITH FORMAT='csv', HEADER=true, INFER_SCHEMA=true;

LOAD baseline_scores FROM '{baseline_scores_csv}'
WITH FORMAT='csv', HEADER=true, INFER_SCHEMA=true;

CREATE BATCH TABLE prepared
WITH base AS (
  SELECT
    case_id,
    segment,
    score_text,
    message,
    payload_json,
    split(labels, '\\|') AS label_array,
    chosen_pos
  FROM metadata_cases
)
SELECT
  case_id AS id,
  segment,
  CAST(score_text AS DOUBLE) AS score,
  CAST(score_text AS DOUBLE) + 1.0 AS score_plus_one,
  1 AS literal_one,
  normalize_text(message) AS normalized_message,
  CAST(parse_json(payload_json)['category'] AS STRING) AS category,
  CAST(parse_json(payload_json)['boost'] AS DOUBLE) AS boost,
  label_array[chosen_pos] AS chosen_label,
  payload_json
FROM base;

CREATE BATCH TABLE parsed_payloads
SELECT
  case_id AS id,
  parse_json(payload_json) AS payload
FROM metadata_cases;

CREATE BATCH TABLE embedded
SELECT
  id,
  embed.text(text = normalized_message, model = 'bge') AS embedding,
  err_or(embed.text(text = normalized_message, model = 'bge')['model'], 'fallback') AS embedding_model
FROM prepared
WHERE id != 'lin-003'
ORDER BY id;

CREATE BATCH TABLE exploded_items
SELECT
  id,
  posexplode(payload['items']) AS (item_pos, item),
  CAST(item AS STRING) AS item_text,
  CAST(payload['category'] AS STRING) AS category
FROM parsed_payloads;

CREATE BATCH TABLE joined_scores
SELECT
  p.id,
  p.segment,
  p.score,
  b.baseline_score,
  p.score - b.baseline_score AS delta
FROM prepared p
LEFT JOIN baseline_scores b
  ON p.id = b.case_id;

CREATE BATCH TABLE segment_summary
SELECT
  segment,
  COUNT(*) AS row_count,
  AVG(score) AS avg_score
FROM prepared
GROUP BY segment
ORDER BY segment;

CREATE BATCH TABLE unioned_scores
SELECT id, score, 'prepared' AS source_branch
FROM prepared
WHERE id = 'lin-001'
UNION ALL
SELECT id, baseline_score AS score, 'baseline' AS source_branch
FROM joined_scores
WHERE id = 'lin-001';
