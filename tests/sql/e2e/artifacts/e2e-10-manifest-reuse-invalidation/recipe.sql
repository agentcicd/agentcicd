LOAD raw FROM '{raw}' WITH FORMAT='parquet';

CREATE BATCH TABLE prepared
SELECT case_id, CAST(score AS DOUBLE) AS score FROM raw;

CREATE BATCH TABLE scored
SELECT case_id, score, score >= 0.7 AS passed FROM prepared;

CREATE BATCH TABLE summary
SELECT COUNT(*) AS rows, AVG(score) AS avg_score FROM scored;
