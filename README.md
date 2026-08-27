# AgentCICD

AgentCICD is an open-source evaluation workflow engine for AI systems. It helps teams turn support-bot checks, agent regression tests, model-judge workflows, and human review loops into repeatable runs with evidence.

An AgentCICD evaluation is a folder: a `recipe.sql` workflow, optional Python fixtures, runtime inputs, and secret references. The workflow prepares cases, calls systems or evaluators, scores outputs, publishes results, and leaves behind a run you can inspect.

AI teams usually start evaluation with a notebook, a spreadsheet, or a small script. That breaks down once the product has multiple prompts, tools, policies, agents, judges, reviewers, and release gates. AgentCICD is for making that quality work operational: rerunnable, inspectable, reviewable, controlled, and suitable for CI.

## Install

Install the engine with the Spark execution backend:

```bash
python -m pip install "agentcicd[spark]"
```

From source:

```bash
git clone https://github.com/agentcicd/agentcicd.git
cd agentcicd
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test,spark]'
```

## Quick Start

Validate the included evaluation:

```bash
agentcicd validate examples/quickstart
```

Run it and open the local inspector:

```bash
agentcicd run examples/quickstart --backend spark --open
```

The inspector shows the current run, executed recipe, inputs, fixtures, annotations, secrets, execution graph, progress, logs, and published report artifacts.

For the complete quickstart recipe, see [Quickstart example](docs/examples/quickstart.md).

## Usecases

- Support bot QA for grounding, escalation, policy adherence, and answer quality.
- Agent and tool-use regression tests across realistic cases.
- Prompt, model, retrieval, and policy release checks.
- Human-in-the-loop review and annotation workflows.
- External API, browser, and model-judge evaluations with runtime controls.
- CI quality gates that preserve inspectable run evidence.

## How It Works

1. Create a project folder with `recipe.sql`.
2. Declare runtime inputs and secret references outside the recipe.
3. Add Python fixtures for target calls, judges, parsers, tools, or simulators.
4. Run the project with `agentcicd run`.
5. Inspect the execution graph, reports, logs, tables, annotations, and runtime controls.

## Minimal Project

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

- `recipe.sql`: evaluation stages, fixture calls, scoring, annotation publishing, and report publishing.
- `fixture_*.py`: optional Python functions available to the recipe as `local.<function_name>`.
- `inputs.yaml`: values for SQL `DECLARE INPUT` declarations. YAML supports scalars, lists, and objects when the declared type accepts them.
- `secrets.yaml`: local secret records. Reference a secret from `inputs.yaml` as `secret.<KEY>`; do not embed credentials in SQL or commit this file.
- `agentcicd.toml`: optional run configuration.

The legacy `input.properties` and `secret.properties` formats remain supported for scalar values.

## Core Capabilities

- **SQL evaluation workflows**: named stages make preparation, execution, scoring, and reporting inspectable.
- **Python extension points**: fixtures handle target adapters, judges, tools, simulators, browser calls, and custom parsing.
- **Inputs and secrets**: declare runtime values in SQL, supply them outside the recipe, and keep credentials out of committed workflows.
- **Annotation support**: review queues and annotation tasks are part of the local inspection flow.
- **Runtime control**: `RATELIMIT` and `POOL` inputs control evaluation speed and executor routing.
- **Run inspection**: materialized tables, progress, reports, logs, graph edges, and artifacts stay with each run.

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

## Docs

- [Start here](docs/index.md): install, quick start, product model, and common evaluation spaces.
- [Getting started](docs/getting-started.md): step-by-step local run.
- [Quickstart example](docs/examples/quickstart.md): complete minimal recipe.
- [Project layout](docs/project-layout.md): files in an evaluation project.
- [AgentCICD SQL](docs/recipe-sql.md): recipe syntax and workflow statements.
- [Python fixtures](docs/fixtures.md): reusable Python functions called from SQL.
- [Inputs and secrets](docs/inputs-and-secrets.md): runtime values, secret references, and legacy compatibility.
- [Local runs and inspection](docs/local-runs-and-inspection.md): run artifacts and inspector behavior.
- [CLI reference](docs/cli.md): local commands.

## License

AgentCICD is licensed under the [Apache License 2.0](LICENSE).
