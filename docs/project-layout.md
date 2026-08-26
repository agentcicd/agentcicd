# Project Layout

An AgentCICD project is a directory. The project loader requires `recipe.sql`; other files are optional.

```text
support-eval/
  recipe.sql
  fixture_target.py
  fixture_judge.py
  fixtures/
    shared_helpers.py
  inputs.yaml
  secrets.yaml
  agentcicd.toml
```

## Required File

- `recipe.sql`: the evaluation recipe. It declares inputs, defines named tables, calls fixtures, scores outputs, and publishes reports or other artifacts.

## Optional Files

- `fixture*.py`: Python fixture files discovered from the project root.
- `fixtures/**/*.py`: Python fixture files discovered recursively under `fixtures/`.
- `inputs.yaml`: values for SQL `DECLARE INPUT` declarations.
- `secrets.yaml`: local secret records referenced from inputs as `secret.<KEY>`.
- `agentcicd.toml`: local run configuration.

The legacy scalar files `input.properties` and `secret.properties` are still supported. A project must not define both `inputs.yaml` and `input.properties`, or both `secrets.yaml` and `secret.properties`.

## Fixture Discovery

AgentCICD discovers fixtures from:

- root-level Python files matching `fixture*.py`
- Python files below a root-level `fixtures/` directory
- additional paths listed in `agentcicd.toml` fixture groups

Discovered fixture functions are registered before recipe validation so recipe calls like `local.normalize_answer(...)` can be checked.

## Run Directory

Runs are written under the configured working directory. The default is:

```text
.agentcicd/runs/run-<UTC timestamp>/
```

The default working directory is controlled by `[run].working_dir` in `agentcicd.toml`.

## Configuration

`agentcicd.toml` is optional. Current run settings include:

```toml
[run]
backend = "spark"
working_dir = ".agentcicd/runs"
table_format = "parquet"
include_cells = true
max_parallel_stages = 1
```

Current backend names are `spark`, `validate`, and `duckdb`; `duckdb` is recognized by configuration but not supported by the v1 runner.
