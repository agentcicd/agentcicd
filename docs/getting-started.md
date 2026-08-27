# Getting Started

This guide runs a small AgentCICD evaluation from a local checkout or installed package. It is the shortest path from installation to an inspectable run.

AgentCICD is an evaluation workflow engine. A workflow is a project folder with a `recipe.sql` file, optional Python fixtures, runtime inputs, and secret references. Use it when you want an AI quality check that can be rerun, inspected, and used as release evidence.

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

## Understand The Project

The quickstart project is a normal folder:

```text
examples/quickstart/
  recipe.sql
```

Open the recipe before running it:

```bash
sed -n '1,200p' examples/quickstart/recipe.sql
```

The recipe creates named evaluation tables and publishes report metrics. Named tables are important because they connect the final result back to the examples and intermediate rows that produced it.

## Validate The Quickstart

Validation loads the project directory, discovers fixtures, resolves declared inputs, and checks the recipe without starting a Spark run.

```bash
agentcicd validate examples/quickstart
```

Expected output:

```text
Validated <absolute-path-to>/examples/quickstart
```

## Run With Spark

```bash
agentcicd run examples/quickstart --backend spark --open
```

`agentcicd run` creates a timestamped run directory under `.agentcicd/runs`, starts the local inspector unless `--ui off` is set, and prints a loopback inspection URL.

In the inspector, start with:

- **Home**: current run status, execution graph, progress, logs, and report results.
- **Recipe**: the exact `recipe.sql` that was executed.
- **Inputs**: values supplied to `DECLARE INPUT`.
- **Fixtures**: Python functions available to the recipe.
- **Annotations**: human review requests and tasks, when the recipe publishes annotation queues.
- **Secrets**: configured secret references, with secret values redacted.

Stop the inspector with `Ctrl-C` when you are finished reviewing the run.

## CI Or Non-Interactive Runs

Disable the inspection server for CI:

```bash
agentcicd run examples/quickstart --backend spark --ui off
```

## Next Steps

- Read the [quickstart example](examples/quickstart.md) for the complete minimal recipe.
- Read [Project layout](project-layout.md) when creating your own evaluation folder.
- Read [AgentCICD SQL](recipe-sql.md) for recipe syntax.
- Read [Local runs and inspection](local-runs-and-inspection.md) for artifacts and UI behavior.
