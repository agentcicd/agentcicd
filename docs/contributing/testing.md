# Testing

The default suite is run with:

```bash
python -m pytest
```

## Test Layout

- `tests/project/`: local project loader, CLI, fixture discovery, inspection, reports, and package distribution behavior.
- `tests/sql/`: recipe syntax, validation, runtime, backend, progress, and SQL behavior.
- `tests/sql/e2e/`: end-to-end SQL engine artifacts and Spark-oriented scenarios.
- `tests/fixtures/`: fixture authoring API and builtin fixture behavior.
- `tests/sandbox/`: local fixture execution tests.

## Focused Runs

Project runner tests:

```bash
python -m pytest tests/project
```

SQL behavior tests:

```bash
python -m pytest tests/sql
```

UI tests:

```bash
cd ui
npm test
```

Check `ui/package.json` before changing UI commands; package scripts are the source of truth.
