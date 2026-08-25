# Wrapped SQL editing contract

Every wrapped-SQL change must define behavior for value evaluation, error propagation, latency metadata, and fixture trace metadata when runtime calls are involved. If a valid SQL query fails because wrapping lowered it incorrectly, fix lowering instead of changing the recipe. Update this file whenever a change alters wrap/unwrap, error, latency, or fixture trace semantics.

A wrapped cell is:

```text
STRUCT<cell_id, value, metadata<errors, latency_ms, fixture_trace>, __agentcicd_cell>
```

`value` is the SQL value. `errors` is the semantic error channel and must not be dropped by hidden subqueries or lowering shortcuts. `latency_ms` is runtime-call metadata and is otherwise null. `fixture_trace` is fixture/runtime-call trace summary metadata and is otherwise null; full trace payloads live outside the cell.

| Operation type | Wrap / unwrap behavior | Error behavior |
| --- | --- | --- |
| Source load | Wrap raw columns as cells. Keep internal row ids separate from user cells. | Start clean with empty errors. |
| Literal | Treat as raw scalar, wrap only when projected. | Empty errors. |
| Column reference | Read `cell.value` in scalar expressions. Preserve whole cell only for direct projection when safe. | Preserve column errors. |
| Row expression | Unwrap child values, compute scalar result, wrap projected output. | Merge child errors unless the expression explicitly consumes errors. Runtime trace metadata does not merge through ordinary expressions; projected derived cells use null `fixture_trace`. |
| Cast | Use `TRY_CAST(child.value AS type)`, then wrap. | Preserve child errors; add `AGENTCICD_CAST_ERROR` when non-null input cannot cast. Cast outputs use null `fixture_trace`. |
| JSON variant access | Static bracket paths over variants lower to `TRY_VARIANT_GET`. Dynamic object-key access lowers through `TO_JSON`/`FROM_JSON(..., 'map<string,variant>')` and `ELEMENT_AT`, preserving the variant representation for chained access. | Strict bracket access preserves child errors and adds `AGENTCICD_JSON_ACCESS_ERROR` when the source, key, or accessed value is missing/null. Explicit tolerant forms such as `try_variant_get`/`get` return null without adding an access error. |
| Runtime function | Pass semantic argument values to runtime; wrap runtime result. | Merge argument errors and runtime errors according to function contract. Preserve runtime latency in `metadata.latency_ms`; when debug tracing is enabled, store the fixture call summary in `metadata.fixture_trace`. |
| `err_or` | Unwrap target/fallback values; returns fallback when target is errored. | Consumes target errors for the output value. |
| `is_err` | Reads target error channel and returns boolean. | Output is clean. |
| `latency` | Reads target latency channel and returns scalar latency. | Output is clean. |
| `WHERE` / `HAVING` | Evaluate predicate on semantic value. Do not predicate on full cells. | Predicate errors must not become truthy/falsy metadata. |
| `JOIN ON` | Compare semantic values. Do not compare full cells. | Join-condition errors must not expose metadata as join keys. |
| `ORDER BY` | Sort by semantic values. In aggregate queries, grouped-key ordering reads grouped values only. | Ordering expression errors follow predicate/decision semantics; aggregate grouped-key metadata before using it in aggregate `ORDER BY`. |
| `GROUP BY` | Group by semantic values. | Projected grouped keys merge errors from rows in the group. |
| Aggregate | Aggregate semantic values. Grouped keys used inside aggregate projections read grouped values only. | Collect contributing row errors; aggregate grouped-key metadata before combining it with aggregate expressions. |
| Window function | Evaluate partition, order, and function arguments on semantic values. Ranking functions produce their SQL value; value functions such as `lag`/`lead` operate on semantic argument values. | Preserve row-local errors from partition, order, and function argument expressions. Do not use aggregate error collection inside non-aggregate window projections. |
| `SELECT DISTINCT` | Deduplicate on semantic values using hidden raw value columns; re-wrap output. | Merge errors from rows that collapse to each surviving tuple. |
| `UNION` | Deduplicate on semantic values. Avoid Spark set ops over full cells. | Merge errors from rows/branches that collapse to each surviving tuple. |
| `UNION ALL` | Preserve rows. If wrapping is needed, carry value/error channels through branch normalization. | Preserve each row's errors unless a later operation merges them. |
| `INTERSECT` / `EXCEPT` | Not fully modeled yet. Do not route through `UNION` lowering by accident. | Must define which side's errors survive or merge before implementation. |
| Internal raw subquery | Allowed only as a lowering implementation detail. Must carry hidden value and error columns. | Hidden error columns must be reattached or merged before final wrap. |
| Projection output | User-visible outputs are wrapped cells unless explicitly raw/internal. | Non-empty output errors null the output value through normal cell construction. Literal and derived projection cells must still include a typed null `fixture_trace` field so Spark set operations see a stable cell schema. |
| `PUBLISH ... COMPONENT = METRIC` | Read `metric`, `value`, and tag columns from semantic cell values. Publish only rows with non-null metric names and numeric values. | Rows whose metric/value cells are errored, null, or non-numeric must not fail the SQL run; route them to report issues with the cell error or validation detail. |
| Debug row stream sidecar | Observational artifact only; must not alter materialized table cells. `debug_options.store_intermediate_tables` controls whether row streams are written. Sidecars emit full wrapped cells so debug views can show values, errors, and trace metadata. | Cell errors must remain visible from streamed debug rows. |

Development rule: for any new SQL construct, fill in this table first, add a focused regression using valid source SQL, then implement lowering/runtime behavior. Value-only fixes are incomplete. Recipe-specific workarounds for engine bugs are not acceptable.
