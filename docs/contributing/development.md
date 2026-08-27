# Development

Set up a local checkout:

```bash
git clone https://github.com/agentcicd/agentcicd.git
cd agentcicd
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test,spark]'
```

Run the Python test suite:

```bash
python -m pytest
```

Build the local inspector UI:

```bash
cd ui
npm ci
npm run build
```

The UI build writes static assets that are packaged under `src/agentcicd/ui_static/`.

## Repository Areas

- `src/agentcicd/`: Python package, CLI, local runner, project loading, reports, and UI server.
- `src/agentcicd/sql/`: recipe syntax, validation, execution, runtime helpers, and backend integrations.
- `src/agentcicd/fixtures/`: fixture authoring API and builtin fixture support.
- `src/agentcicd/sandbox/`: local fixture execution support.
- `ui/`: React/Vite inspector source.
- `tests/`: Python and SQL engine tests.
