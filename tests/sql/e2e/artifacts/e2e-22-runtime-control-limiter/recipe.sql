DECLARE INPUT echo_limiter RATELIMIT DEFAULT 2;

LOAD cases FROM '{cases_csv}'
WITH FORMAT='csv', HEADER=true, INFER_SCHEMA=true;

CREATE BATCH TABLE echoed
OPTIONS (DESCRIPTION = 'Exercises a local UDF with an injected runtime control limiter.')
SELECT
  case_id,
  controlled_echo(text = phrase, limiter = echo_limiter) AS echoed_text
FROM cases
ORDER BY case_id;

CREATE BATCH TABLE checked
OPTIONS (DESCRIPTION = 'Confirms the UDF result is a value cell rather than a runtime error cell.')
SELECT
  case_id,
  echoed_text,
  is_err(echoed_text) AS echoed_failed,
  err_or(echoed_text, 'failed') AS echoed_value
FROM echoed
ORDER BY case_id;
