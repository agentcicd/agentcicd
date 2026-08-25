LOAD expression_inputs FROM '{expression_inputs_csv}'
WITH FORMAT='csv', HEADER=true, INFER_SCHEMA=true;

CREATE BATCH TABLE normalized
SELECT
  case_id,
  CAST(score_text AS DOUBLE) AS score,
  NOT flag AS not_flag,
  normalize_text(message) AS message_norm,
  parse_json(payload_json) AS payload
FROM expression_inputs;

CREATE BATCH TABLE composed
WITH scored AS (
  SELECT
    case_id,
    score,
    message_norm,
    payload,
    CAST(payload['boost'] AS DOUBLE) AS boost,
    array_size(payload['items']) AS item_count,
    embed.text(text = err_or(message_norm, 'fallback text'), model = 'bge') AS embedding
  FROM normalized
)
SELECT
  case_id,
  -err_or(score, 0.0) AS neg_score,
  err_or(score, 0.0) + err_or(boost, 0.0) * 2 AS weighted_score,
  lower(err_or(message_norm, 'fallback text')) || ':' || CAST(err_or(item_count, 0) AS STRING) AS label,
  (err_or(score, 0.0) >= 0.7 AND NOT is_err(embedding)) OR err_or(item_count, 0) > 1 AS eligible,
  err_or(embedding['model'], 'fallback-model') AS embedding_model,
  CASE
    WHEN is_err(score) THEN 'score-error'
    WHEN is_err(message_norm) THEN 'message-error'
    WHEN is_err(embedding) THEN 'embedding-error'
    ELSE 'clean'
  END AS route
FROM scored;
