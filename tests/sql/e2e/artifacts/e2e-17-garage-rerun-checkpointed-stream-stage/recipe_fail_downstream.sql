LOAD garage_raw FROM '{garage_raw}' WITH FORMAT='parquet';

CREATE STREAM TABLE citation_examples_stream
OPTIONS (BATCH_SIZE=1)
SELECT
  sample_id,
  passage_id,
  passage_text,
  response_label
FROM garage_raw;

CREATE BATCH TABLE parsed_small_judgments
SELECT
  sample_id,
  passage_id,
  CASE
    WHEN sample_id = 'q2' THEN raise_error('stream downstream failure')
    ELSE response_label
  END AS small_label
FROM citation_examples_stream;

CREATE BATCH TABLE score_rows
SELECT COUNT(*) AS citation_count
FROM parsed_small_judgments;

