# Runtime And Fixtures

Local runs connect project fixtures to recipe execution.

## Local Run Flow

The current local runner flow is:

```text
load project -> discover fixtures -> validate recipe -> prepare run directory -> execute backend -> render report
```

The implementation is centered in:

- `src/agentcicd/project.py`
- `src/agentcicd/runtime/local_runner.py`
- `src/agentcicd/runtime/local_fixtures.py`
- `src/agentcicd/sandbox/manager.py`
- `src/agentcicd/sandbox/function_runner.py`

## Fixture Discovery

`load_project` discovers root-level `fixture*.py` files, recursive `fixtures/**/*.py` files, and additional fixture group paths from `agentcicd.toml`.

## Runtime Plan

Before recipe validation, the local runner builds a fixture runtime plan so fixture signatures can be registered. During Spark execution, `local_fixture_runtime` exposes registered functions to the SQL engine.

## Runtime Controls

Runtime-control argument types such as `RATELIMIT` and `POOL` are part of function invocation behavior. They should stay typed and testable outside Spark-specific wrappers.
