# AgentCICD

AgentCICD is an evaluation workflow engine for AI systems. It turns quality checks for agents, support bots, copilots, model judges, and human review flows into repeatable local runs with inspectable evidence.

AI teams usually start evaluation with a notebook, a spreadsheet, or a small script. That breaks down once the product has multiple prompts, tools, policies, agents, judges, reviewers, and release gates. AgentCICD is for making that quality work operational: rerunnable, inspectable, reviewable, controlled, and suitable for CI.

Use this page to get running first, then understand the concepts you need to build your own evaluation.

## Install

Install AgentCICD with the Spark backend:

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

The inspector opens a local run view with status, recipe, inputs, fixtures, annotations, secrets, execution graph, progress, logs, tables, and published reports.

For the full minimal recipe, read the [quickstart example](examples/quickstart.md).

## What You Just Ran

An AgentCICD evaluation is a project folder:

```text
support-eval/
  recipe.sql
  fixture_target.py
  fixture_judge.py
  inputs.yaml
  secrets.yaml
```

The `recipe.sql` file describes the evaluation workflow. It creates named stages, calls fixtures when custom code is needed, publishes annotation queues when humans must review rows, and publishes report outputs when the run is complete.

Fixtures are normal Python functions exposed to SQL as `local.<function_name>`. They are where target adapters, model judges, browser calls, parsers, tools, simulators, and external API calls belong.

Inputs and secrets keep recipes portable without hard-coding deployment details. Declare runtime values in SQL, supply local values through `inputs.yaml`, and store credentials in `secrets.yaml` or another secret source.

A run writes evidence under `.agentcicd/runs/`: materialized tables, reports, logs, progress events, execution graph data, annotation requests, runtime controls, and redacted configuration snapshots.

## Evaluation Spaces

AgentCICD is useful anywhere an AI behavior needs repeatable evidence:

- **Support bot QA**: test grounding, safe escalation, policy adherence, refusal behavior, and answer quality.
- **Agent workflow QA**: run tool-use cases through an agent and inspect decisions, outputs, and failure rows.
- **Prompt and model release checks**: compare behavior before and after prompt, retrieval, tool, or model changes.
- **Human review pipelines**: publish annotation requests during the run and use reviewer judgments in downstream evaluation stages.
- **Model-judge workflows**: call judges from fixtures while preserving judge inputs, outputs, and scoring rows.
- **External-system evaluations**: coordinate API calls, browsers, shared sessions, and local worker processes through runtime controls.
- **CI quality gates**: fail builds on regression metrics while preserving run artifacts for review.

## Core Concepts

| Concept | What it solves |
| --- | --- |
| `recipe.sql` | Composes the evaluation dataflow as named, inspectable stages. |
| Python fixtures | Keep custom target calls, judges, parsers, and tools reusable without burying workflow logic in scripts. |
| Inputs and secrets | Separate environment-specific values and credentials from committed recipes. |
| Annotation publishing | Brings human review into the run instead of treating it as an external side process. |
| Rate limits | Control throughput for model, API, browser, or target-system calls. |
| Executor pools | Route fixture work to shared executors or local worker groups when the evaluation needs coordinated resources. |
| Local inspector | Shows the run status, graph, recipe, inputs, fixtures, annotations, secrets, reports, logs, and artifacts. |

## Next Steps

Once the quickstart runs, use the page that matches what you are building next:

- [Getting started](getting-started.md) for a guided first run.
- [Project layout](project-layout.md) for the files in an evaluation folder.
- [AgentCICD SQL](recipe-sql.md) for recipe syntax, published outputs, annotations, pools, and rate limits.
- [Python fixtures](fixtures.md) for reusable code called from SQL.
- [Inputs and secrets](inputs-and-secrets.md) for runtime values, credentials, and legacy compatibility.
- [Local runs and inspection](local-runs-and-inspection.md) for run directories, UI behavior, graph data, logs, and artifacts.
- [CLI reference](cli.md) for command details.

## Contributing

- [Development](contributing/development.md) explains local checkout and development commands.
- [Testing](contributing/testing.md) explains test layout and common verification commands.
- [Release](contributing/release.md) covers packaging and release checks.
