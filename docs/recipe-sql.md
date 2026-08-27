# AgentCICD SQL

AgentCICD recipes are SQL scripts for evaluation workflows. Use them to declare inputs, create named stages, call fixtures, publish reports, and coordinate review or runtime controls.

The goal is to keep the evaluation readable. SQL describes the dataflow; Python fixtures handle custom behavior.

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

Named tables are evaluation evidence. Prefer separate tables for preparation, target calls, parsing, scoring, and aggregate reporting so a result can be traced back to rows.

## Declared Inputs

Declare recipe dependencies with `DECLARE INPUT`:

```sql
DECLARE INPUT target_url STRING;
DECLARE INPUT provider_key SECRET;
DECLARE INPUT threshold FLOAT DEFAULT 0.8;
```

Inputs are supplied locally with `inputs.yaml` or `input.properties`. Supported input families in the current validator include scalar SQL types, `DATASET`, `AISYSTEM`, `SECRET`, `RATELIMIT`, `POOL`, and `VARIANT`.

See [Inputs and secrets](inputs-and-secrets.md).

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

See [Python fixtures](fixtures.md).

## Publish Statements

Publish report rows with:

```sql
PUBLISH metric_rows TO REPORTS WITH (COMPONENT = METRIC);
```

Current report components are:

- `METRIC`: source table must expose `metric` and `value` columns.
- `ISSUE`: source table is written to report issue artifacts.
- `CHART`: source table is written to report chart artifacts and requires chart options such as chart type and axes.

Publish a dataset artifact with:

```sql
PUBLISH scored_cases TO DATASET "support-smoke-cases";
```

Use report publishing for values you want surfaced in the inspector's published results view.

## Annotation Queues

Publish rows for human review:

```sql
PUBLISH cases TO ANNOTATION QUEUE review_queue AS support_review
WITH (
  TEMPLATE = '<View><Text name="prompt" value="$prompt"/></View>',
  REVIEWERS_PER_TASK = 1
);
```

The local inspector exposes annotation requests and tasks. Review output is saved with the run and can be finalized into result artifacts.

Use this when an evaluation needs human judgment before downstream results should be trusted.

## Runtime Controls

Runtime controls are declared as inputs and passed into fixture calls.

```sql
DECLARE INPUT judge_limit RATELIMIT DEFAULT '{"key":"judge","max_in_flight":2}';
DECLARE INPUT browser_pool POOL DEFAULT '{"kind":"session","name":"browser"}';

CREATE BATCH TABLE judged
SELECT local.judge_answer(answer = response, rate_limit = judge_limit) AS judgment
FROM responses;
```

Use `RATELIMIT` when an evaluation should control request speed or concurrency. Use `POOL` when fixture calls should be routed through a named executor pool.

Pool and rate-limit values live in the recipe input layer, not a separate pool-specific configuration file.

## Naming Guidance

Use stable, meaningful names:

- table names should describe evidence, such as `cases`, `responses`, `judged`, or `metric_rows`
- input names should describe the dependency, such as `target_url`, `judge_limit`, or `browser_pool`
- published report tables should have clear columns for their component type

This makes the execution graph and inspector useful to reviewers.
