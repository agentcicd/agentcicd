# Eval SQL E2E Artifacts

Use e2e artifacts for contracts that need the recipe runner, Spark-local behavior, debug artifacts, runtime fixture calls, publication artifacts, or rerun/reuse behavior. Prefer unit or smoke tests for parser, lowering, pure runtime helpers, and small backend services.

Naming convention:

- Directory names should use `e2e-NN-short-behavior-name`.
- Put recipe metadata in `recipe.yaml` and executable SQL in `recipe.sql`.
- Put small deterministic input files under `inputs/`.
- Put runtime fixture definitions under `fixtures/`.
- Put expected review artifacts under `expected/`.

Expected files should stay minimal and reviewable. When updating expected artifacts, run the narrow e2e scenario first, inspect only the changed YAML, and keep unrelated fixture churn out of the diff.

Curated canary categories:

- Debug row stream sidecars.
- Runtime-control limiter behavior.
- Wrapped-cell relational workflows.
- Rerun/reuse behavior.
