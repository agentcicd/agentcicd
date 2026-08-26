# SQL Engine Internals

The SQL engine lives under `src/agentcicd/sql/`. Keep the public recipe surface and internal execution pipeline separate when changing this package.

## Package Map

- `surface/`: top-level recipe parsing and SQLGlot conversion.
- `ir/`: immutable statement and expression models.
- `semantics/`: validation, type handling, function resolution, and recipe contracts.
- `lowering/`: conversion from IR into executable SQL and runtime expressions.
- `engine/`: execution plans, backend interfaces, progress reporting, publications, reusable stages, and Spark compatibility facade.
- `engine/backends/spark/`: focused Spark backend services for layout, table I/O, stage artifacts, debug streams, reuse, and sessions.
- `runtime/`: runtime invocation helpers and control argument handling.
- `observability/`: progress, diagnostics, and redaction helpers.
- `analysis/`: graph and dependency analysis.

## Parser Boundary

Use `agentcicd.sql.surface.TopLevelParser` as the parser entrypoint for recipe text. New top-level statements should add dispatch in `top_level_parser.py`, feature-specific parsing in the owning surface module, and focused syntax tests.

## Validation Boundary

Semantics should reject invalid recipes before backend execution. Examples include duplicate input names, unsupported declared input types, report publish contracts, missing source tables, invalid runtime-control arguments, and unsupported table dependencies.

## Backend Boundary

The engine should depend on backend interfaces and focused backend services. Spark-specific file layout, cell wrapping, table registry behavior, and debug stream emission should stay under `engine/backends/spark/` unless a cross-backend contract is needed.
