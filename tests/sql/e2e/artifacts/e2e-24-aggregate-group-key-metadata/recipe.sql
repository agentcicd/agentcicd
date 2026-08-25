LOAD metric_rows_raw FROM '{metric_rows_csv}'
WITH FORMAT='csv', HEADER=true, INFER_SCHEMA=true;

CREATE BATCH TABLE scored
SELECT
  workflow,
  task,
  score
FROM metric_rows_raw;

CREATE BATCH TABLE metric_summary
SELECT
  named_struct(
    'metric', 'judge_macro_f1',
    'metric_value', avg(score),
    'tags', map('workflow', workflow, 'task', task)
  ) AS metric_row,
  'judge_macro_f1' AS metric,
  avg(score) AS value,
  map('workflow', workflow, 'task', task) AS tags
FROM scored
GROUP BY workflow, task
ORDER BY workflow, task;
