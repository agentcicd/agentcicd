# Local Runs And Inspection

`agentcicd run` turns an evaluation project into reviewable evidence. It validates the project, prepares a run directory, executes the selected backend, renders report artifacts, and optionally serves the local inspection UI.

The inspector is useful because AI evaluation failures are rarely explained by a single final score. You often need to inspect the graph, intermediate tables, logs, annotation status, and published report artifacts together.

## Run Directories

The default run root is:

```text
<project>/.agentcicd/runs/
```

Each Spark run creates a timestamped directory:

```text
<project>/.agentcicd/runs/run-<UTC timestamp>/
```

The run root can be changed with `[run].working_dir` in `agentcicd.toml`.

## Inspector

By default, Spark runs start the local inspection server:

```bash
agentcicd run path/to/project --backend spark
```

The command prints:

```text
Inspect this run: http://127.0.0.1:<port>/...
```

Use `--open` to open that URL in a browser.

Use `--ui off` for non-interactive runs:

```bash
agentcicd run path/to/project --backend spark --ui off
```

The inspector has sections for:

- **Home**: current run status, execution graph, progress, logs, and report summary.
- **Recipe**: the exact workflow that was executed.
- **Inputs**: declared input values used by the run.
- **Fixtures**: local Python fixtures available to recipe calls.
- **Annotations**: human review requests and tasks.
- **Secrets**: configured secret references, with values redacted.

## Opening Existing Runs

Open a previously written run directory:

```bash
agentcicd ui open path/to/project/.agentcicd/runs/run-...
```

Serve all local inspection artifacts for a project:

```bash
agentcicd ui serve path/to/project
```

## Reports

After a Spark run, AgentCICD renders report artifacts under the run's `reports/` directory. Current local report files include:

- `metrics.json`
- `issues.json`
- `charts.json`
- `report.md`
- `report.html`

Reports are the summary. The run directory is the evidence behind the summary.
