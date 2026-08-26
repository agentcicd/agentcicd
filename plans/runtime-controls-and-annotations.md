# Runtime Controls And Annotation Plan

This plan covers three related concepts in the open-source local runner:

1. Pools: bounded execution capacity for table stages and fixture/runtime calls.
2. Annotation: human review tasks generated during evaluation, with run continuation after results are produced.
3. Rate limits: named concurrency limits that control evaluation speed for external calls and expensive runtime work.

The goal is to make these concepts coherent for local open-source use before exposing them as first-class public docs.

## Current Local Implementation

The current checkout already has implementation pieces for all three concepts.

### Pools

Implemented pieces:

- `POOL` is a declared input kind.
- Pool kinds are `executor`, `service`, `session`, and `sandbox`.
- Table stages without an explicit pool get a default `executor_pool`.
- Fixture/runtime calls can receive a `POOL` control argument.
- Recipe normalization can inject default fixture pools from registered function metadata.
- Local fixture runtime starts a runtime-control server and sandbox manager, then registers a local pool node.

Key files:

- `src/agentcicd/sql/pool_inputs.py`
- `src/agentcicd/sql/injections/recipe_defaults.py`
- `src/agentcicd/runtime/local_fixtures.py`
- `src/agentcicd/sql/runtime/controls.py`
- `src/agentcicd/sql/runtime/udf_compat/runtime_control.py`

Important current behavior:

- `executor_pool` models table-stage execution capacity. In local OSS docs, executor pools should be the primary user-facing model for executors and local processes. Backend scheduling details should remain implementation detail.
- `service`, `session`, and `sandbox` pools model fixture/runtime worker lifecycle.
- Local fixture runtime defaults to a `service` pool when no explicit recipe pool is supplied.
- Pool configuration should live in recipe-declared `POOL` inputs rather than `agentcicd.toml` fixture group configuration.

### Annotation

Implemented pieces:

- SQL parses `PUBLISH <table> TO ANNOTATION QUEUE ...`.
- SQL parses `RETRIEVE ANNOTATION RESULTS <table> FROM ...`.
- Engine execution has explicit retrieve annotation steps.
- Local annotation store can load `results.parquet`, `results.json`, or `results.jsonl`.
- HTTP annotation store can fetch results and treats HTTP 404 as pending results.
- Pending annotation produces a waiting progress event with `annotation_request_id`, `source_ref`, and `target_table`.
- Inspection graph already includes publish and retrieve annotation segments.

Key files:

- `src/agentcicd/sql/surface/custom_statement_parser.py`
- `src/agentcicd/sql/engine/annotation_store.py`
- `src/agentcicd/sql/engine/runtime.py`
- `src/agentcicd/sql/analysis/recipe_graph.py`
- `src/agentcicd/inspection/local.py`
- `tests/sql/test_engine_runtime.py`
- `tests/sql/test_engine_interface_edge_cases.py`

Important current behavior:

- The engine can stop at a waiting annotation step instead of failing the run.
- There is no complete local annotation UI workflow yet.
- There is no complete local wait-and-continue loop yet that keeps the running process alive while annotation results are produced.
- The current UI has status styling for `waiting_for_annotation`, but not task review screens.

### Rate Limits

Implemented pieces:

- `RATELIMIT` is a declared input kind.
- Runtime calls can receive a `RATELIMIT` control argument.
- Recipe normalization can inject a default `fixture_ratelimit`.
- Runtime-control code supports local and driver HTTP limiter acquisition.
- Local fixture/runtime invokers acquire limiter permits around synchronous calls.
- Async row functions use their internal async limiter path.

Key files:

- `src/agentcicd/sql/runtime/controls.py`
- `src/agentcicd/sql/runtime/rate_limits.py`
- `src/agentcicd/sql/runtime/udf_compat/runtime_control.py`
- `src/agentcicd/sql/runtime/invokers/local_fixture.py`
- `src/agentcicd/sql/runtime/invokers/http.py`
- `tests/sql/test_runtime_controls.py`
- `tests/sql/test_declared_inputs.py`

Important current behavior:

- A `RATELIMIT` value is lowered into a runtime-control payload with `key` and `max_in_flight`.
- `max_in_flight` limits concurrent calls for that named key.
- Rate limits control IO/runtime call concurrency, not Spark table scheduling.

## Concept Model

### Pool

A pool is a named capacity boundary.

For table execution, an `executor` pool describes how much table-stage work can run at once. In local open-source mode, this should map cleanly to local executors or local worker processes:

- SQL-declared `executor_pool` capacity
- executor/task capacity where applicable
- deterministic scheduling behavior for independent DAG stages

For fixtures, a pool describes worker lifecycle and capacity:

- `service`: reusable shared workers. Good for stateless HTTP-like services.
- `session`: reusable exclusive workers with session state. Good for browser sessions, shell sessions, or agent environments.
- `sandbox`: disposable or isolated workers. Good for untrusted or failure-prone work.

For local OSS, a pool can be implemented with local processes and a local runtime-control service. It does not need Kubernetes or managed CP/DP infrastructure.

### Annotation

Annotation is a run wait-and-continue workflow.

The engine should be able to:

1. Publish candidate rows from a recipe table into an annotation request.
2. Surface those tasks in the local inspection UI.
3. Let a reviewer submit labels/reviews.
4. Write annotation results as a durable local artifact.
5. Keep the run process waiting at the `RETRIEVE ANNOTATION RESULTS` step.
6. Continue the rest of the evaluation using the produced annotation table.

Annotation should be treated as evaluation data, not a side note. It should appear in:

- recipe graph
- run progress
- local artifacts
- inspection UI
- report provenance

### Rate Limit

A rate limit is a named max-in-flight limit for runtime work.

It answers: "How many calls with this limiter key may be active at once?"

It should be used for:

- model API calls
- browser/agent harness calls
- remote service calls
- expensive or externally constrained fixture work

It should not be used to model table-stage compute. Table-stage compute belongs to executor pools.

## Desired Local OSS Behavior

### Pools

Pool configuration should live in the recipe. A project should not need `agentcicd.toml` fixture groups for the normal pool path.

```sql
DECLARE INPUT executor_pool POOL
WITH kind = 'executor'
DEFAULT {
  'kind': 'executor',
  'max_workers': 2,
  'cores_per_worker': 1,
  'memory_per_worker': '1g',
  'task_cpus': 1
};

DECLARE INPUT browser_session_pool POOL
WITH kind = 'session'
DEFAULT {
  'kind': 'session',
  'max_instances': 2,
  'cpu_per_instance': '1',
  'memory_per_instance': '1g',
  'timeout_seconds': 300
};

DECLARE INPUT model_api_limit RATELIMIT DEFAULT 4;

CREATE BATCH TABLE cases
OPTIONS (POOL = executor_pool)
SELECT * FROM input_cases;

CREATE BATCH TABLE judged
OPTIONS (POOL = executor_pool)
SELECT local.review_page(
  task = task,
  pool = browser_session_pool,
  limiter = model_api_limit
) AS review
FROM cases;
```

Default behavior should stay ergonomic:

- If a table has no `OPTIONS (POOL = ...)`, inject or assume `executor_pool`.
- If a fixture function declares a `POOL` parameter and compatible metadata, inject a default fixture pool.
- If a fixture function declares a `RATELIMIT` parameter and no explicit limiter is provided, inject `fixture_ratelimit`.
- Local fixture runtime should map the SQL `POOL` input to the matching local worker pool, same as the service path maps SQL pool inputs to managed runtime pool nodes.

### Annotation

Local annotation should have a simple folder contract:

```text
.agentcicd/runs/<run-id>/
  annotations/
    requests/
      annreq.<generated-id>/
        manifest.json
        tasks.jsonl
        reviews.jsonl
        results.jsonl
```

The UI should expose:

- waiting annotation requests
- task list
- task detail
- review form generated from the same Label Studio XML template contract used by the service
- progress counts
- finalize action that writes `results.jsonl`
- continue signal for the waiting run process

The CLI should keep the process alive while the run is waiting for annotation results:

```bash
agentcicd ui open .agentcicd/runs/<run-id>
```

Wait-and-continue behavior should be deterministic:

- Reuse completed upstream tables.
- Load annotation results into the requested retrieve table.
- Continue descendants of the retrieve step in the same running process.
- Keep all artifacts in the original run directory.

If the original process exits before annotation completes, recovery can be a later feature. It should not be required for the first local annotation UX.

### Rate Limits

Rate limit docs and UI should show:

- limiter key
- max in flight
- active leases
- waiting calls
- timeout/failure count

The local runtime-control server already has enough machinery for this. The missing work is mostly surfacing it cleanly and making examples obvious.

## Implementation Plan

### Phase 1: Document Current Runtime Controls

Create public docs that explain only current local behavior:

- `docs/runtime-controls.md`
- `docs/annotation.md`
- a small example under `docs/examples/`

Avoid promising managed CP/DP behavior in OSS docs. Keep Kubernetes, CP, and DP as architecture references only.

### Phase 2: Local Entity APIs For UI

Create local API endpoints for the entities the UI exposes, using the same shape as the existing service split between CP and DP APIs.

Service references in `/Users/ankursarda/Repo/niro-benchmarks`:

- CP-style metadata APIs: `kb/agentcicd-apis.md` documents annotation queues, annotation requests, run status, run progress, and internal DP callbacks.
- DP-style publish API: `agentcicd_dp_api/src/agentcicd_dp_api/routers/publish.py` publishes annotation tasks and creates annotation request metadata.
- DP annotation task APIs: `agentcicd_dp_api/src/agentcicd_dp_api/routers/annotations.py` serves tasks, reservations, reviews, progress, results, and finalize.
- Worker wait integration: `agentcicd_dp_worker/src/agentcicd_dp_worker/activities/job.py` marks a run waiting when retrieve annotation emits a waiting event.

Local OSS does not need separate network services, but the UI should talk to a clean local API boundary rather than reading arbitrary files directly. Suggested local API groups:

- `/api/runs/{run_id}`: run status, progress, graph, artifacts, report summary.
- `/api/runs/{run_id}/annotations/requests`: list local annotation requests.
- `/api/runs/{run_id}/annotations/requests/{request_id}`: read request metadata and progress.
- `/api/runs/{run_id}/annotations/requests/{request_id}/tasks`: list tasks.
- `/api/runs/{run_id}/annotations/requests/{request_id}/tasks/{task_id}`: read one task.
- `/api/runs/{run_id}/annotations/requests/{request_id}/tasks/{task_id}/reviews`: submit review.
- `/api/runs/{run_id}/annotations/requests/{request_id}/finalize`: write results and signal the waiting process.
- `/api/runs/{run_id}/runtime/pools`: inspect pool nodes and leases.
- `/api/runs/{run_id}/runtime/rate-limits`: inspect limiter keys and active permits.

The local API should preserve the same entity names as the service:

- annotation queue
- annotation request
- annotation task
- annotation review
- annotation result
- run
- run progress event
- pool node
- pool lease
- rate-limit lease

Use explicit DTOs for these local APIs. Do not let the UI infer structure from
raw files.

Implementation rule: define these as shared typed models in the local OSS codebase
first, then adapt filesystem artifacts into those models. The UI client should
consume these API DTOs only. Filesystem paths are storage details, not UI data
contracts.

Annotation metadata should mirror the CP contacts/API models:

```text
AnnotationQueue:
  id
  name
  description
  admins
  reviewers
  status
  created_at
  updated_at

AnnotationRequest:
  id
  organization_id or local_project_id
  queue_id
  run_id
  recipe_id
  cluster_id
  source_table
  publish_alias
  instructions
  reviewers_per_task
  reservation_minutes
  consensus
  template_snapshot
  data_path
  reviews_path
  results_path
  manifest_path
  status
  total_tasks
  completed_tasks
  created_at
  updated_at
```

Annotation task/review APIs should mirror the DP router response models:

```text
AnnotationTask:
  task_id
  data
  status
  review_count

AnnotationTaskList:
  request_id
  tasks
  total
  completed

AnnotationReviewCreate:
  reviewer_id
  result

AnnotationReview:
  task_id
  reviewer_id
  submitted_at
  result

AnnotationProgress:
  request_id
  total_tasks
  completed_tasks
  status

AnnotationFinalize:
  request_id
  total_tasks
  completed_tasks
  results_path
```

Use `task_id` as the local task identifier in task/review/result DTOs. The
older CP task-group model uses `id`, but the active DP annotation APIs use
`task_id`; local OSS annotation should follow the DP request/task contract.

Local storage can be filesystem-backed, but it should be projected through the
same structures:

```text
manifest.json:
  queue_name
  source_table
  publish_alias
  instructions
  template
  review_policy:
    reviewers_per_task
    reservation_minutes
    consensus

tasks.jsonl:
  task_id
  data

reviews.jsonl or reviews/task=<task_id>/*.json:
  task_id
  reviewer_id
  submitted_at
  result

results.jsonl:
  task_id
  data
  result
  reviews
```

Pool inputs should use the same normalized contract as service `pool_inputs`:

```text
PoolInput:
  kind: executor | service | session | sandbox

Executor pool fields:
  min_workers
  max_workers
  cores_per_worker
  memory_per_worker
  task_cpus
  max_parallel_stages, compatibility/derived only

Fixture/runtime pool fields:
  min_instances
  min_warm
  max_instances
  cpu_per_instance
  memory_per_instance
  timeout_seconds
  lease_ttl_seconds
  reset_timeout_seconds
  idle_ttl_seconds
```

The local runtime-control API should expose pool and limiter state as typed
records:

```text
PoolNode:
  pool_name
  pool_kind
  node_id
  address
  status
  capacity
  available
  generation

PoolLease:
  lease_id
  pool_name
  pool_kind
  node_id
  manager_id
  worker_slot_id
  address
  request_id
  executor_id
  fixture_id
  status
  acquired_at
  expires_at
  generation
  lease_decision

RateLimitLease:
  lease_id
  key
  max_in_flight
  active_count
  request_id
  acquired_at
  expires_at
```

`max_parallel_stages` may exist in service-compatible DTOs because the managed
runtime derives Spark scheduling settings from executor pool capacity. Local OSS
docs and examples should not present it as a separate user-facing pool
configuration knob.

Acceptance criteria for this phase:

- Local DTO names and fields line up with CP/DP contracts unless there is an explicit local-only reason.
- UI state is built from DTOs returned by local API endpoints, not by walking `.agentcicd/runs` directly.
- Annotation publication writes storage artifacts and a DTO projection with the same request/task/review/progress vocabulary.
- Runtime-control inspection exposes pool nodes, pool leases, and rate-limit leases with stable typed fields.
- Tests cover DTO serialization from filesystem artifacts and endpoint responses consumed by the UI.

### Phase 3: Local Annotation Artifact Writer

Add a local annotation publication store that writes tasks locally when the recipe runs:

- Convert published rows into `tasks.jsonl`.
- Write `manifest.json`.
- Write alias/request metadata so `RETRIEVE ... FROM <alias>` resolves deterministically.
- Emit progress metadata with the generated request id.
- Use generated `annreq.<id>` request ids, same as current service behavior.
- Require and validate the same Label Studio XML `TEMPLATE` option as the service path.

This fills the current gap between parser/engine support and usable local review.

### Phase 4: Annotation UI

Add local inspection views for annotation requests:

- request list
- task list
- task review form
- task status
- finalize results

The first version should support the same Label Studio XML template contract used by the service. A reduced renderer is acceptable, but the recipe/API contract should not diverge.

### Phase 5: Waiting Run Continuation

Add a wait-and-continue path:

- `agentcicd run` keeps the inspector and run process alive when annotation results are pending.
- `RETRIEVE ANNOTATION RESULTS` polls or waits on a local annotation completion signal.
- Finalizing annotation results in the UI unblocks the running process.
- The engine loads the generated result table and continues downstream stages.

This is the critical piece that makes annotation part of evaluation rather than a separate manual export/import flow.

### Phase 6: Pool And Rate Limit Observability

Expose runtime-control state in local inspection:

- pool nodes
- active leases
- limiter state
- worker lifecycle events
- fixture traces with `pool_name`, `pool_kind`, `limiter_key`, and `max_in_flight`

This turns pools/rate limits from invisible scheduling knobs into debuggable evaluation infrastructure.

## Decisions

- Annotation request ids use generated `annreq.<id>` values, same as current service behavior.
- Local annotation should not require a separate resume command for the first workflow. The run process should wait, and UI finalization should unblock it.
- Annotation UI should support the same Label Studio XML template contract as the service path.
- `executor_pool` should remain SQL-visible because it is the user-facing model for executors and table-stage capacity.
- SQL `POOL` inputs should map to local runtime pools the same way service `pool_inputs` map to managed pool nodes. Do not introduce a separate TOML-only pool identity.

## Near-Term Recommendation

For the open-source project, treat `RATELIMIT` and fixture pools as near-term public features, because the local implementation is already substantive and tested.

Treat annotation as an explicit planned workflow: the SQL engine and artifact contracts exist, but the local product loop is incomplete until local entity APIs, Label Studio-template review UI, and wait-and-continue execution are implemented.

Keep managed CP/DP deployment details in architecture docs, but make the local API/entity model intentionally parallel to the service APIs so the UI does not need separate concepts.

## Verification Notes

Verified against the local checkout on this branch.

Passing focused command:

```bash
python3 -m pytest \
  tests/sql/test_recipe_injections.py \
  tests/sql/test_runtime_controls.py \
  tests/sql/test_engine_runtime.py \
  tests/sql/test_engine_interface_edge_cases.py \
  -q
```

Result:

```text
32 passed
```

Passing local fixture runtime command in the repository virtualenv:

```bash
.venv/bin/python -m pytest \
  tests/project/test_local_fixtures.py::test_local_fixture_runtime_invokes_through_sandbox_manager \
  -q
```

Result:

```text
1 passed
```

The same local fixture test failed under the bare system `python3 -m pytest` command with HTTP 400. A direct reproduction using `PYTHONPATH=src python3` passed. Treat the repository virtualenv as the reliable verification environment for local fixture runtime behavior.
