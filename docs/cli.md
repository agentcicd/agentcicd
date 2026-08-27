# CLI Reference

Use the `agentcicd` command to validate projects, run evaluations, and open the local inspector.

## Validate

```bash
agentcicd validate path/to/project
```

Loads the project, discovers fixtures, coerces declared inputs, and validates the recipe.

## Run

```bash
agentcicd run path/to/project --backend spark
```

Options:

- `--backend`: execution backend. Current configured names are `spark`, `validate`, and `duckdb`; the v1 local runner supports Spark execution and validate-only mode.
- `--ui`: `auto` or `off`. Defaults to `auto`.
- `--open`: open the local inspection URL in a browser.

## Transpile

```bash
agentcicd transpile path/to/project
```

Prints the execution SQL generated from `recipe.sql`, discovered fixtures, declared inputs, and default runtime controls without starting a run.

To write numbered SQL files and a plan manifest:

```bash
agentcicd transpile path/to/project --output-dir /tmp/agentcicd-transpiled
```

The output directory contains `engine_plan.json` plus one `.sql` file for each SQL-bearing execution step. Non-SQL steps such as fixture registration and table loads appear in the manifest.

## UI Serve

```bash
agentcicd ui serve path/to/project
```

Serves the local inspection UI for a project. Use `--port` to select a loopback port; `0` lets the server choose one.

## UI Open

```bash
agentcicd ui open path/to/project/.agentcicd/runs/run-...
```

Serves the local inspection UI and opens a specific existing run.
