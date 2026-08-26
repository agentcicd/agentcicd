# Getting Started

This guide runs the included quickstart project from a local checkout or installed package.

## Install

Install AgentCICD with the Spark backend:

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

## Validate The Quickstart

Validation loads the project directory, discovers local fixtures, coerces declared inputs, and validates the recipe without starting a Spark run.

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

Stop the inspector with `Ctrl-C` when you are finished reviewing the run.

## CI Or Non-Interactive Runs

Disable the inspection server for CI:

```bash
agentcicd run examples/quickstart --backend spark --ui off
```
