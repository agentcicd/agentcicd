CREATE BATCH TABLE out
SELECT price + tax AS total
FROM prepared
WHERE price > 0;
