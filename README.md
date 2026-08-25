# AgentCICD

AgentCICD is an open-source engine for evaluating agent behavior before release. Define an evaluation as a folder containing declarative SQL, optional Python fixtures, inputs, and secret references. Run it locally, inspect the materialized stages and report artifacts, and keep the evaluation alongside the agent it protects.

The engine is designed for repeatable agent evaluation workflows: prepare cases, invoke agents or fixtures, score results, publish metrics or issues, and inspect the evidence behind a release decision.

## What It Provides

- Declarative evaluation stages written in AgentCICD SQL on top of Spark SQL.
- Named batch and streaming tables that make preparation, generation, scoring, and reporting inspectable.
- Python fixtures for reusable evaluators, tools, simulators, parsers, and target adapters.
- A local sandbox manager that routes fixture calls while fixtures run independently.
- Typed YAML inputs and local secret references, with scalar `.properties` compatibility.
- Local run artifacts, reports, traces, tables, and an inspection UI.

For the broader AgentCICD workflow, SQL language concepts, fixtures, run review, annotations, and release checks, see the [AgentCICD documentation](https://app.agentcicd.com/docs).

## Install

Install the engine with the Spark execution backend:

```bash
python -m pip install "agentcicd[spark]"
```

For local development:

```bash
git clone https://github.com/agentcicd/agentcicd.git
cd agentcicd
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test,spark]'
```

## Quick Start

Validate the included example without starting Spark:

```bash
agentcicd validate examples/quickstart
```

Run it with Spark and open the local inspector:

```bash
agentcicd run examples/quickstart --backend spark --open
```

`agentcicd run` prints a loopback URL. The inspector remains available while the command is running; stop it with `Ctrl-C` when you are finished reviewing the run.

## Project Layout

An AgentCICD project is a directory.

```text
support-eval/
  recipe.sql
  fixture_target.py
  fixture_judge.py
  inputs.yaml
  secrets.yaml
  agentcicd.toml
```

- `recipe.sql`: Evaluation stages, fixture calls, scoring, and published outputs.
- `fixture_*.py`: Optional Python functions available to the recipe as `local.<function_name>`.
- `inputs.yaml`: Values for SQL `DECLARE INPUT` declarations. YAML supports scalars, lists, and objects when the declared type accepts them.
- `secrets.yaml`: Local secret records. Reference a secret from `inputs.yaml` as `secret.<KEY>`; do not embed credentials in SQL or commit the file.
- `agentcicd.toml`: Optional run configuration, including backend and parallel-stage settings.

The legacy `input.properties` and `secret.properties` formats remain supported for scalar values.

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

Named tables are evaluation evidence. Use separate tables for preparation, target calls, parsing, scoring, and aggregates so a report change can be traced back to the relevant examples.

## Python Fixtures

Fixtures hold reusable Python logic while recipes retain the evaluation dataflow:

```python
from agentcicd import Str, function


@function
def normalize_answer(value: Str) -> Str:
    return value.strip().lower()
```

Save this as `fixture_normalize.py`, then call it from a recipe:

```sql
CREATE BATCH TABLE normalized
SELECT local.normalize_answer(value = message) AS message
FROM cases;
```

Fixture calls are routed through the sandbox manager. This preserves isolation and gives the run a single control point for fixture lifecycle, rate limits, and teardown.

## Inputs And Secrets

Declare dependencies in SQL:

```sql
DECLARE INPUT target_url STRING;
DECLARE INPUT provider_key SECRET;
DECLARE INPUT threshold FLOAT DEFAULT 0.8;
```

Supply them locally:

```yaml
# inputs.yaml
target_url: https://agent.example.test
provider_key: secret.OPENAI_API_KEY
threshold: 0.85
```

```yaml
# secrets.yaml
OPENAI_API_KEY:
  type: api_key
  value: replace-with-local-value
```

Keep `secrets.yaml` out of version control. AgentCICD redacts secret values from local inspection artifacts, but you should still treat local secret files as credentials.

## Commands

```bash
agentcicd validate path/to/project
agentcicd run path/to/project --backend spark
agentcicd ui serve path/to/project
```

Use `--ui off` for CI or non-interactive runs.

## Development

Run the standalone engine test suite:

```bash
python -m pytest
```

The default suite validates the folder runner without requiring Spark. Spark and end-to-end tests are marked separately.

To change the local inspector UI:

```bash
cd ui
npm ci
npm run build
```

This rebuilds the static assets packaged with the Python distribution.

## Documentation

- [Product documentation](https://app.agentcicd.com/docs)
- [SQL engine architecture](docs/architecture.md)
- [Wrapped SQL semantics](docs/contributing/wrapped-sql-semantics.md)

## License

No license has been selected yet. Add an OSI-approved license before inviting external contributors or distributing the repository as open source.
