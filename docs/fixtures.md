# Python Fixtures

Fixtures keep reusable Python logic outside the recipe while preserving the recipe as the evaluation dataflow.

## Basic Fixture

Create a root-level file such as `fixture_normalize.py`:

```python
from agentcicd import Str, function


@function
def normalize_answer(value: Str) -> Str:
    return value.strip().lower()
```

Call it from `recipe.sql`:

```sql
CREATE BATCH TABLE normalized
SELECT local.normalize_answer(value = message) AS message
FROM cases;
```

## Discovery

The local runner discovers fixture files from:

- `fixture*.py` files in the project root
- `fixtures/**/*.py`
- explicit fixture group paths in `agentcicd.toml`

## Runtime Boundary

The local runner builds a fixture runtime plan before validating a project. During Spark execution, fixture calls are routed through the local fixture runtime and sandbox manager rather than being embedded directly into recipe SQL.

This keeps lifecycle, limits, teardown, and runtime-control behavior in one place.

## Public Authoring Imports

The package exports fixture authoring helpers from `agentcicd`, including `function`, scalar types such as `Str`, `Int`, `Float`, `Bool`, and richer types such as `Variant`, `Array`, `Map`, and `NamedStruct`.

Prefer importing authoring helpers from `agentcicd` in user examples unless an internal module is required for a maintainer test.
