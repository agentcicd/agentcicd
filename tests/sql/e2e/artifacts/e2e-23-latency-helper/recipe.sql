LOAD latency_inputs FROM '{latency_inputs_csv}'
WITH FORMAT='csv', HEADER=true, INFER_SCHEMA=true;

CREATE BATCH TABLE prepared
SELECT
  case_id,
  message,
  rand() AS random_value,
  length(message) AS message_length
FROM latency_inputs;

CREATE BATCH TABLE fixture_outputs
SELECT
  case_id,
  message,
  random_value,
  message_length,
  embed.text(text = message, model = 'bge') AS embedding
FROM prepared;

CREATE BATCH TABLE latency_checks
SELECT
  case_id,
  latency('literal') IS NULL AS literal_latency_is_null,
  latency(random_value) IS NULL AS random_latency_is_null,
  latency(message_length + 1) IS NULL AS derived_latency_is_null,
  latency(embedding) IS NOT NULL AS fixture_latency_present,
  latency(embedding) >= 0 AS fixture_latency_non_negative,
  err_or(embedding['model'], 'missing') AS embedding_model
FROM fixture_outputs;

CREATE BATCH TABLE latency_summary
SELECT
  COUNT(*) AS row_count,
  SUM(CASE WHEN fixture_latency_present THEN 1 ELSE 0 END) AS fixture_latency_count,
  SUM(CASE WHEN literal_latency_is_null AND random_latency_is_null AND derived_latency_is_null THEN 1 ELSE 0 END) AS null_latency_count,
  AVG(message_length) AS avg_message_length,
  latency(AVG(message_length)) IS NULL AS aggregate_latency_is_null
FROM latency_checks
JOIN prepared ON latency_checks.case_id = prepared.case_id;
