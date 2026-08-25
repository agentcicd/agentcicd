LOAD raw_outputs FROM '{raw_outputs}' WITH FORMAT='parquet';

CREATE FUNCTION strip_json_fence(content STRING)
RETURNS STRING
RETURN trim(
  regexp_replace(
    regexp_replace(
      trim(coalesce(content, '')),
      '(?is)^\\x60\\x60\\x60\\s*json\\s*',
      ''
    ),
    '(?is)\\s*\\x60\\x60\\x60\\s*$',
    ''
  )
);

CREATE STREAM TABLE streamed_scores
OPTIONS (BATCH_SIZE=1)
SELECT
  item_id,
  CAST(
    parse_json(
      strip_json_fence(
        CAST(parse_json(llm_response_json)['choices'][0]['message']['content'] AS STRING)
      )
    )['score']
    AS DOUBLE
  ) AS score,
  CAST(
    parse_json(
      strip_json_fence(
        CAST(parse_json(llm_response_json)['choices'][0]['message']['content'] AS STRING)
      )
    )['confidence']
    AS DOUBLE
  ) AS confidence,
  CAST(
    parse_json(
      strip_json_fence(
        CAST(parse_json(llm_response_json)['choices'][0]['message']['content'] AS STRING)
      )
    )['rationale']
    AS STRING
  ) AS rationale
FROM raw_outputs;

CREATE BATCH TABLE final_scores
WITH adjusted_scores AS (
  SELECT
    item_id,
    score + 0.5 AS score,
    confidence,
    rationale
  FROM streamed_scores
  WHERE item_id = 'item-001'
),
cascade_scores AS (
  SELECT
    'adjusted' AS workflow,
    s.item_id,
    coalesce(a.score, s.score) AS score,
    coalesce(a.confidence, s.confidence) AS confidence,
    coalesce(a.rationale, s.rationale) AS rationale
  FROM streamed_scores s
  LEFT JOIN adjusted_scores a
    ON s.item_id = a.item_id
  WHERE s.item_id = 'item-001'
)
SELECT
  'streamed' AS workflow,
  item_id,
  score,
  confidence,
  rationale
FROM streamed_scores
UNION ALL
SELECT
  workflow,
  item_id,
  score,
  confidence,
  rationale
FROM cascade_scores
ORDER BY workflow, item_id;
