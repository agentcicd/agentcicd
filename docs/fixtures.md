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

During a run, fixture calls are routed through AgentCICD instead of being embedded directly into recipe SQL.

This lets the evaluation use normal Python for custom logic while the recipe keeps the workflow readable. It also gives AgentCICD a place to apply runtime controls such as rate limits and executor pools.

## Public Authoring Imports

The package exports fixture authoring helpers from `agentcicd`, including `function`, scalar types such as `Str`, `Int`, `Float`, `Bool`, and richer types such as `Variant`, `Array`, `Map`, and `NamedStruct`.

Prefer importing authoring helpers from `agentcicd` in project fixtures.
