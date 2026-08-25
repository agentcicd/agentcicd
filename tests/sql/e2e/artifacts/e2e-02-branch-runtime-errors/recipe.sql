LOAD wrapped_messages FROM '{wrapped_messages_parquet}'
WITH FORMAT='parquet';

CREATE BATCH TABLE inspected
SELECT
  case_id,
  is_err(customer_message) AS input_is_err,
  err_or(customer_message, 'fallback-message') AS safe_message,
  is_err(embed.text(text = customer_message, model = 'bge')) AS embed_failed,
  err_or(embed.text(text = customer_message, model = 'bge')['model'], 'fallback-model') AS safe_model,
  CASE
    WHEN is_err(customer_message) THEN 'input-error'
    WHEN is_err(embed.text(text = customer_message, model = 'bge')) THEN 'embed-error'
    ELSE 'clean'
  END AS route
FROM wrapped_messages
WHERE is_err(customer_message) OR err_or(customer_message, '') IS NOT NULL;
