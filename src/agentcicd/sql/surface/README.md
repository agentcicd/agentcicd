# Surface Parser Ownership

Use `agentcicd_eval_sql.surface.TopLevelParser` as the single entrypoint for parsing recipe text into `StatementIR`.

- `top_level_parser.py` splits scripts into top-level statements and dispatches by statement kind.
- `custom_statement_parser.py` owns AgentCICD workflow statements such as load, save, publish, and annotation operations.
- `spark_sql_parser.py` owns Spark SQL expression/query parsing helpers.
- `sqlglot_bridge.py` converts SQLGlot expressions into AgentCICD IR.
- `agentcicd_eval_sql.parsing` should remain low-level token, segment, and compatibility support.

New top-level statements should add dispatch logic in `top_level_parser.py`, feature-specific parsing in the owning surface module, and focused syntax tests under `agentcicd_eval_sql/tests`.
