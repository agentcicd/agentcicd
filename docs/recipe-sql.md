# AgentCICD SQL

AgentCICD recipes are SQL scripts with workflow statements layered on top of Spark SQL. The parser turns recipe text into immutable IR statements, validates dependencies and contracts, lowers stages, and executes them through the selected backend.

## Minimal Recipe

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

Named tables are evaluation evidence. Prefer separate tables for preparation, target calls, parsing, scoring, and aggregate reporting so a release decision can be traced back to rows.

## Declared Inputs

Declare recipe dependencies with `DECLARE INPUT`:

```sql
DECLARE INPUT target_url STRING;
DECLARE INPUT provider_key SECRET;
DECLARE INPUT threshold FLOAT DEFAULT 0.8;
```

Inputs are supplied locally with `inputs.yaml` or `input.properties`. Supported input families in the current validator include scalar SQL types, `DATASET`, `AISYSTEM`, `SECRET`, `RATELIMIT`, `POOL`, and `VARIANT`.

## Batch And Stream Tables

Use named table statements for executable stages:

```sql
CREATE BATCH TABLE prepared
SELECT case_id, message
FROM cases;
```

```sql
CREATE STREAM TABLE events OPTIONS (BATCH_SIZE = 25)
SELECT *
FROM source_events;
```

`CREATE BATCH TABLE` and `CREATE STREAM TABLE` are the recipe stage forms. Free-standing query statements are not the recommended workflow surface for evaluations.

## Fixture Calls

Python fixtures are called from SQL with the `local.` namespace:

```sql
CREATE BATCH TABLE normalized
SELECT local.normalize_answer(value = message) AS normalized_message
FROM cases;
```

Fixture calls use typed signatures from registered fixture functions. Runtime-control inputs such as `RATELIMIT` and `POOL` are handled as control arguments instead of ordinary data values.

## Publish Statements

Publish report rows with:

```sql
PUBLISH metric_rows TO REPORTS WITH (COMPONENT = METRIC);
```

Current report components are:

- `METRIC`: source table must expose `metric` and `value` columns.
- `ISSUE`: source table is written to report issue artifacts.
- `CHART`: source table is written to report chart artifacts and requires chart options such as chart type and axes.

The parser also has workflow forms for publishing datasets and annotation queues. Keep user-facing recipes focused on report publishing unless the destination contract is part of the workflow you are documenting.
