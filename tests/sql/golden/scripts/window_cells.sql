CREATE BATCH TABLE out
SELECT
  customer_id,
  ROW_NUMBER() OVER (PARTITION BY segment ORDER BY score DESC) AS rn
FROM prepared;
