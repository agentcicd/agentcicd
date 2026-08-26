# Inspection Artifacts

Local inspection is built around durable run artifacts. The UI should read artifacts from the run directory rather than treating terminal progress as the source of truth.

## Current Run Roots

The local runner creates:

```text
<project>/.agentcicd/runs/run-<timestamp>/
  progress/
  reports/
```

Backend execution may also write tables, debug streams, logs, stage manifests, schema sidecars, publications, and annotation artifacts depending on the recipe and debug settings.

## Report Rendering

`src/agentcicd/reports.py` renders local report artifacts after Spark execution:

- `reports/metrics.json`
- `reports/issues.json`
- `reports/charts.json`
- `reports/report.md`
- `reports/report.html`

Known local secret values are redacted from report, progress, log, and debug text artifacts after rendering.

## Progress Versus Evidence

Progress events are for user feedback while a run executes. Debug streams, stage manifests, reports, and materialized tables are the durable evidence used for inspection and review.
