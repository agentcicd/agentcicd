LOAD garage_raw FROM '{garage_jsonl}' WITH FORMAT = jsonl;
LOAD large_response_fixtures FROM '{large_responses_jsonl}' WITH FORMAT = jsonl;
LOAD small_response_fixtures FROM '{small_responses_jsonl}' WITH FORMAT = jsonl;

CREATE FUNCTION support_label(raw_label STRING)
RETURNS STRING
RETURN (
  CASE
    WHEN raw_label = 'ANSWER-THE-QUESTION' THEN 'answers_question'
    WHEN raw_label = 'RELATED-INFORMATION' THEN 'related_but_insufficient'
    WHEN raw_label = 'OUTDATED' THEN 'outdated'
    WHEN raw_label = 'UNKNOWN' THEN 'unknown'
    ELSE 'not_useful'
  END
);

CREATE FUNCTION relevance_label(raw_label STRING)
RETURNS STRING
RETURN (
  CASE
    WHEN raw_label = 'YES' THEN 'relevant'
    ELSE 'not_relevant'
  END
);

CREATE FUNCTION citation_label(raw_label STRING)
RETURNS STRING
RETURN (
  CASE
    WHEN raw_label = 'YES' THEN 'should_cite'
    ELSE 'should_not_cite'
  END
);

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

-- Stage 1: Prepare GaRAGe examples.
CREATE BATCH TABLE citation_examples
SELECT
  sample_id,
  question,
  answer_generate,
  passage_id,
  passage_text,
  passage_provider,
  passage_age,
  passage_date,
  question_type,
  question_complexity,
  question_category,
  question_popularity,
  question_sensitive,
  gold_relevance_label,
  gold_support_label,
  gold_citation_label
FROM (
  SELECT
    sample_id,
    question,
    answer_generate,
    posexplode(grounding) AS (pos, grounding_item),
    concat('cite_', CAST(pos + 1 AS STRING)) AS passage_id,
    coalesce(
      grounding_item.cite_1,
      grounding_item.cite_2,
      grounding_item.cite_3,
      grounding_item.cite_4,
      grounding_item.cite_5,
      grounding_item.cite_6,
      grounding_item.cite_7,
      grounding_item.cite_8,
      grounding_item.cite_9,
      grounding_item.cite_10,
      grounding_item.cite_11,
      grounding_item.cite_12,
      grounding_item.cite_13,
      grounding_item.cite_14,
      grounding_item.cite_15
    ) AS passage_text,
    grounding_item.provider AS passage_provider,
    grounding_item.age AS passage_age,
    grounding_item.date AS passage_date,
    question_type,
    question_complexity,
    question_category,
    question_popularity,
    question_sensitive,
    relevance_label(evidence_relevant[pos]) AS gold_relevance_label,
    support_label(evidence_correct[pos]) AS gold_support_label,
    citation_label(evidence_cited[pos]) AS gold_citation_label
  FROM (
    SELECT
      sample_id,
      question,
      answer_generate,
      grounding,
      evidence_relevant,
      evidence_correct,
      evidence_cited,
      question_type,
      question_complexity,
      question_category,
      question_popularity,
      question_sensitive
    FROM garage_raw
    WHERE question_valid = 'YES'
      AND answer_validate = 'YES'
  )
)
WHERE passage_text IS NOT NULL;

CREATE BATCH TABLE question_examples
SELECT
  sample_id,
  first(question) AS question,
  first(answer_generate) AS answer_generate,
  first(question_type) AS question_type,
  first(question_complexity) AS question_complexity,
  first(question_category) AS question_category,
  first(question_popularity) AS question_popularity,
  first(question_sensitive) AS question_sensitive,
  to_json(
    collect_list(
      named_struct(
        'passage_id', passage_id,
        'passage_text', passage_text,
        'passage_provider', passage_provider,
        'passage_age', passage_age,
        'passage_date', passage_date
      )
    )
  ) AS citations_json
FROM citation_examples
GROUP BY sample_id;

-- Stage 2: Use static judge fixtures.
CREATE BATCH TABLE large_judge_outputs
SELECT
  sample_id,
  parse_json(CAST(large_response AS STRING)) AS large_response
FROM large_response_fixtures;

CREATE BATCH TABLE small_judge_outputs
SELECT
  sample_id,
  passage_id,
  parse_json(CAST(small_response AS STRING)) AS small_response
FROM small_response_fixtures;

-- Stage 3: Parse judge outputs and usage.
CREATE BATCH TABLE parsed_large_judgments
SELECT
  sample_id,
  explode(parse_json(strip_json_fence(CAST(large_response['choices'][0]['message']['content'] AS STRING)))['citations']) AS judgment,
  CAST(judgment['passage_id'] AS STRING) AS passage_id,
  CAST(judgment['relevance_label'] AS STRING) AS large_relevance_label,
  CAST(judgment['support_label'] AS STRING) AS large_support_label,
  CAST(judgment['citation_label'] AS STRING) AS large_citation_label,
  CAST(judgment['confidence'] AS DOUBLE) AS large_confidence,
  CAST(judgment['rationale'] AS STRING) AS large_rationale
FROM large_judge_outputs;

CREATE BATCH TABLE parsed_small_judgments
SELECT
  sample_id,
  passage_id,
  parse_json(strip_json_fence(CAST(small_response['choices'][0]['message']['content'] AS STRING))) AS judgment,
  CAST(judgment['relevance_label'] AS STRING) AS small_relevance_label,
  CAST(judgment['support_label'] AS STRING) AS small_support_label,
  CAST(judgment['citation_label'] AS STRING) AS small_citation_label,
  CAST(judgment['confidence'] AS DOUBLE) AS small_confidence,
  CAST(judgment['rationale'] AS STRING) AS small_rationale
FROM small_judge_outputs;

CREATE BATCH TABLE judge_usage_summary
SELECT
  'all' AS metric_join_key,
  workflow,
  call_grain,
  count(*) AS call_count,
  coalesce(sum(input_tokens), 0.0) AS input_tokens,
  coalesce(sum(output_tokens), 0.0) AS output_tokens,
  coalesce(sum(coalesce(total_tokens, input_tokens + output_tokens, 0.0)), 0.0) AS total_tokens,
  coalesce(sum(coalesce(response_cost_usd, cost_usd, 0.0)), 0.0) AS cost_usd
FROM (
  SELECT
    sample_id,
    coalesce(
      CAST(try_variant_get(usage, '$.prompt_tokens') AS DOUBLE),
      CAST(try_variant_get(usage, '$.input_tokens') AS DOUBLE)
    ) AS input_tokens,
    coalesce(
      CAST(try_variant_get(usage, '$.completion_tokens') AS DOUBLE),
      CAST(try_variant_get(usage, '$.output_tokens') AS DOUBLE)
    ) AS output_tokens,
    CAST(try_variant_get(usage, '$.total_tokens') AS DOUBLE) AS total_tokens,
    workflow,
    call_grain,
    response_cost_usd,
    cost_usd
  FROM (
    SELECT
      sample_id,
      CAST(NULL AS STRING) AS passage_id,
      'global_large' AS workflow,
      'question' AS call_grain,
      large_response['usage'] AS usage,
      CAST(try_variant_get(large_response, '$.response_cost') AS DOUBLE) AS response_cost_usd,
      CAST(try_variant_get(large_response, '$.cost') AS DOUBLE) AS cost_usd
    FROM large_judge_outputs
    UNION ALL
    SELECT
      sample_id,
      passage_id,
      'local_small' AS workflow,
      'citation' AS call_grain,
      small_response['usage'] AS usage,
      CAST(try_variant_get(small_response, '$.response_cost') AS DOUBLE) AS response_cost_usd,
      CAST(try_variant_get(small_response, '$.cost') AS DOUBLE) AS cost_usd
    FROM small_judge_outputs
  )
)
GROUP BY workflow, call_grain;

-- Stage 4: Compare judge outputs against GaRAGe gold labels.
CREATE BATCH TABLE evaluated_citations
SELECT
  c.sample_id,
  c.passage_id,
  c.question,
  c.answer_generate,
  c.passage_text,
  c.passage_provider,
  c.question_type,
  c.question_complexity,
  c.question_category,
  c.question_popularity,
  c.question_sensitive,
  c.gold_relevance_label,
  c.gold_support_label,
  c.gold_citation_label,
  l.large_relevance_label,
  l.large_support_label,
  l.large_citation_label,
  l.large_confidence,
  l.large_rationale,
  s.small_relevance_label,
  s.small_support_label,
  s.small_citation_label,
  s.small_confidence,
  s.small_rationale,
  CASE WHEN l.large_relevance_label = c.gold_relevance_label THEN 1.0 ELSE 0.0 END AS large_relevance_correct,
  CASE WHEN l.large_support_label = c.gold_support_label THEN 1.0 ELSE 0.0 END AS large_support_correct,
  CASE WHEN l.large_citation_label = c.gold_citation_label THEN 1.0 ELSE 0.0 END AS large_citation_correct,
  CASE WHEN s.small_relevance_label = c.gold_relevance_label THEN 1.0 ELSE 0.0 END AS small_relevance_correct,
  CASE WHEN s.small_support_label = c.gold_support_label THEN 1.0 ELSE 0.0 END AS small_support_correct,
  CASE WHEN s.small_citation_label = c.gold_citation_label THEN 1.0 ELSE 0.0 END AS small_citation_correct,
  CASE
    WHEN l.large_relevance_label = s.small_relevance_label
     AND l.large_support_label = s.small_support_label
     AND l.large_citation_label = s.small_citation_label
    THEN 0.0
    ELSE 1.0
  END AS workflow_disagreed
FROM citation_examples c
LEFT JOIN parsed_large_judgments l
  ON c.sample_id = l.sample_id AND c.passage_id = l.passage_id
LEFT JOIN parsed_small_judgments s
  ON c.sample_id = s.sample_id AND c.passage_id = s.passage_id;

-- Stage 5: Publish metrics and issues.
CREATE BATCH TABLE score_row_structs
SELECT
  explode(metric_rows) AS metric_row,
  metric_row.metric,
  metric_row.metric_value AS value,
  metric_row.tags
FROM (
  SELECT
    array(
      named_struct('metric', 'large_support_accuracy', 'metric_value', coalesce(avg(large_support_correct), 0.0), 'tags', map('workflow', 'global_large', 'task', 'support')),
      named_struct('metric', 'small_citation_accuracy', 'metric_value', coalesce(avg(small_citation_correct), 0.0), 'tags', map('workflow', 'local_small', 'task', 'citation')),
      named_struct('metric', 'over_citation_rate', 'metric_value', coalesce(avg(CASE WHEN small_citation_label = 'should_cite' AND gold_citation_label = 'should_not_cite' THEN 1.0 ELSE 0.0 END), 0.0), 'tags', map('workflow', 'local_small', 'task', 'citation'))
    )
  AS metric_rows
  FROM evaluated_citations
)
UNION ALL
SELECT
  explode(
    array(
      named_struct('metric', 'judge_call_count', 'metric_value', CAST(call_count AS DOUBLE), 'tags', map('workflow', workflow, 'unit', 'calls', 'call_grain', call_grain)),
      named_struct('metric', 'judge_input_tokens', 'metric_value', input_tokens, 'tags', map('workflow', workflow, 'unit', 'tokens', 'token_type', 'input')),
      named_struct('metric', 'judge_output_tokens', 'metric_value', output_tokens, 'tags', map('workflow', workflow, 'unit', 'tokens', 'token_type', 'output')),
      named_struct('metric', 'judge_total_tokens', 'metric_value', total_tokens, 'tags', map('workflow', workflow, 'unit', 'tokens', 'token_type', 'total')),
      named_struct('metric', 'judge_cost_usd', 'metric_value', cost_usd, 'tags', map('workflow', workflow, 'unit', 'usd'))
    )
  ) AS metric_row,
  metric_row.metric,
  metric_row.metric_value AS value,
  metric_row.tags
FROM judge_usage_summary
UNION ALL
SELECT
  named_struct(
    'metric', 'judge_calls_per_citation',
    'metric_value', CASE WHEN c.citation_count > 0 THEN CAST(u.call_count AS DOUBLE) / CAST(c.citation_count AS DOUBLE) ELSE 0.0 END,
    'tags', map('workflow', u.workflow, 'unit', 'calls_per_citation', 'call_grain', u.call_grain)
  ) AS metric_row,
  'judge_calls_per_citation' AS metric,
  CASE WHEN c.citation_count > 0 THEN CAST(u.call_count AS DOUBLE) / CAST(c.citation_count AS DOUBLE) ELSE 0.0 END AS value,
  map('workflow', u.workflow, 'unit', 'calls_per_citation', 'call_grain', u.call_grain) AS tags
FROM judge_usage_summary u
JOIN (SELECT 'all' AS metric_join_key, count(*) AS citation_count FROM citation_examples) c
ON u.metric_join_key = c.metric_join_key
UNION ALL
SELECT
  named_struct(
    'metric', 'support_accuracy_by_gold_label',
    'metric_value', coalesce(avg(err_or(small_support_correct, NULL)), 0.0),
    'tags', map('workflow', 'local_small', 'task', 'support', 'gold_support_label', gold_support_label)
  ) AS metric_row,
  'support_accuracy_by_gold_label' AS metric,
  coalesce(avg(err_or(small_support_correct, NULL)), 0.0) AS value,
  map('workflow', 'local_small', 'task', 'support', 'gold_support_label', gold_support_label) AS tags
FROM evaluated_citations
GROUP BY gold_support_label;

CREATE BATCH TABLE score_rows
SELECT
  metric,
  value,
  tags
FROM score_row_structs;

CREATE BATCH TABLE issue_rows
SELECT
  sample_id,
  passage_id,
  'Global large and local small workflows disagree' AS title,
  'medium' AS severity,
  concat('Gold support label: ', gold_support_label, '. Large workflow: ', coalesce(large_support_label, 'null'), '. Small workflow: ', coalesce(small_support_label, 'null'), '.') AS description,
  question,
  passage_text,
  answer_generate,
  large_rationale,
  small_rationale
FROM evaluated_citations
WHERE workflow_disagreed = 1.0
LIMIT 100;

PUBLISH score_rows TO REPORTS WITH (COMPONENT = METRIC);
PUBLISH issue_rows TO REPORTS WITH (COMPONENT = ISSUE);
