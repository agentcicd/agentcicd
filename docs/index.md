# AgentCICD Documentation

AgentCICD is organized around local evaluation projects: a project directory contains a `recipe.sql` file, optional Python fixtures, typed inputs, local secret references, and run configuration. The engine validates the project, executes recipe stages, writes local artifacts, and serves an inspection UI for completed or in-progress runs.

Start with the user docs if you want to run or author evaluations. Use the architecture docs when changing internals.

## User Docs

- [Getting started](getting-started.md): install AgentCICD, validate the quickstart, run it with Spark, and open the inspector.
- [Project layout](project-layout.md): files that make up an AgentCICD project directory.
- [AgentCICD SQL](recipe-sql.md): the current recipe surface: declared inputs, batch and stream tables, fixture calls, and publish statements.
- [Python fixtures](fixtures.md): local Python functions exposed to recipes as `local.<function_name>`.
- [Inputs and secrets](inputs-and-secrets.md): `inputs.yaml`, `secrets.yaml`, type coercion, and legacy `.properties` compatibility.
- [Local runs and inspection](local-runs-and-inspection.md): where run artifacts are written and how to inspect them.
- [CLI reference](cli.md): local `agentcicd` commands.
- [Examples](examples/quickstart.md): notes for the included quickstart project.

## Maintainer Docs

- [Architecture overview](architecture/overview.md): boundaries for parser, IR, semantics, lowering, execution, backends, runtime functions, and debug streams.
- [SQL engine internals](architecture/sql-engine.md): package-level map of the SQL implementation.
- [Runtime and fixtures](architecture/runtime-and-fixtures.md): how local fixture discovery and invocation fit into runs.
- [Inspection artifacts](architecture/inspection-artifacts.md): local run artifact expectations.
- [Sandboxing](architecture/sandboxing.md): current sandbox manager and function-runner boundaries.
- [Development](contributing/development.md): local checkout and development commands.
- [Testing](contributing/testing.md): test layout and common test commands.
- [Release](contributing/release.md): current packaging and release entry points.

## Documentation Policy

Keep public docs grounded in shipped behavior. If a feature only exists in tests, examples, or an internal module, describe it as current implementation detail or leave it in maintainer docs until the user-facing surface is stable.
