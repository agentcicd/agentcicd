# Project Layout

An AgentCICD project is a directory. The project loader uses `recipe.sql` when present. If there is no `recipe.sql` and exactly one root-level `.sql` file exists, that file is selected automatically. If multiple root-level `.sql` files exist, pass `--recipe` or `-r` to choose one.

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

## Recipe File

- `recipe.sql`: the conventional evaluation recipe name.
- `<name>.sql`: an alternate root-level recipe name. It is selected automatically only when it is the only root-level `.sql` file, or explicitly with `--recipe <name>.sql`.

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

Most projects can start without `agentcicd.toml`. Add it when you need to pin the backend, change where run artifacts are written, or control local execution settings.
