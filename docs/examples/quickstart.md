# Quickstart Example

The included quickstart lives at:

```text
examples/quickstart/
  recipe.sql
```

Its recipe creates two cases, counts them, and publishes the count as a report metric.

```sql
CREATE BATCH TABLE cases
SELECT * FROM VALUES
  ('case-001', 'How do I reset my password?'),
  ('case-002', 'Where is my order?')
AS cases(case_id, message);

CREATE BATCH TABLE metric_rows
SELECT
  'case_count' AS metric,
  COUNT(*) AS value
FROM cases;

PUBLISH metric_rows TO REPORTS WITH (COMPONENT = METRIC);
```

Validate it:

```bash
agentcicd validate examples/quickstart
```

Run it with Spark:

```bash
agentcicd run examples/quickstart --backend spark --open
```
