# CLI Reference

The `agentcicd` command is implemented with Typer in `src/agentcicd/cli.py`.

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
