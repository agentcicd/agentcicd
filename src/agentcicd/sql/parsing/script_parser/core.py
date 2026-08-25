from __future__ import annotations

from agentcicd.sql.parsing import parser as _parser

for _name, _value in vars(_parser).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

class AgentCICDScriptParserCoreMixin:
    @staticmethod
    def _normalize_script_for_parsing(script: str) -> str:
        normalized_script = normalize_python_syntax(script)
        normalized_script = _normalize_macro_placeholders(normalized_script)
        normalized_script = _rewrite_variant_colon_access(normalized_script)
        # `sql.` is the logical namespace alias for SQL helper function calls.
        # Strip it before parsing so Spark executes unqualified function names.
        normalized_script = re.sub(r"(?i)\bsql\.", "", normalized_script)
        for prefix in ("sql", "agent", "aisystems", "data", "envs", "http", "ranking", "simulators", "string", "zip"):
            normalized_script = re.sub(
                rf"(?i)\b{prefix}\.([a-z_]\w*(?:\.[a-z_]\w*)*)\s*\(",
                lambda m, p=prefix: f"{p}_{m.group(1).replace('.', '_')}(",
                normalized_script,
            )
        # Strip single-level SQL function namespaces (e.g. llm.func(...), text.func(...)).
        # Keep target runtime families like aisystems.*, envs.*, and http.* untouched.
        normalized_script = re.sub(
            r"(?i)(?<!sql\.)(?<!agent\.)(?<!aisystems\.)(?<!data\.)(?<!envs\.)(?<!http\.)(?<!ranking\.)(?<!simulators\.)(?<!string\.)(?<!zip\.)\b[a-z_]\w*\.([a-z_]\w*)\s*\(",
            r"\1(",
            normalized_script,
        )
        return normalized_script

    @staticmethod
    def _looks_like_runtime_function_alias(name: str) -> bool:
        normalized = name.upper()
        return normalized.startswith((
            "SQL_",
            "AGENT_",
            "AISYSTEMS_",
            "DATA_",
            "ENVS_",
            "HTTP_",
            "RANKING_",
            "SIMULATORS_",
            "STRING_",
            "ZIP_",
            "CTR_",
            "CONTAINER_",
        ))

    def _expression_contains_runtime_function_alias(self, expression: exp.Expression) -> bool:
        for node in expression.find_all(exp.Func):
            name = self._function_call_name(node)
            if name and self._looks_like_runtime_function_alias(name):
                return True
        return False

    def parse(self) -> List[SqlSegment]:
        expressions = self._parse_expressions()
        blocks: List[SqlSegment] = []

        for expression in expressions:
            if isinstance(expression, exp.Semicolon):
                continue
            if isinstance(expression, LoadExpression):
                blocks.append(self._build_load_segment(expression, len(blocks)))
            elif isinstance(expression, SaveExpression):
                blocks.append(self._build_save_segment(expression, len(blocks)))
            elif isinstance(expression, PublishExpression):
                blocks.append(self._build_publish_segment(expression, len(blocks)))
            elif isinstance(expression, PublishDatasetExpression):
                blocks.append(self._build_publish_dataset_segment(expression, len(blocks)))
            elif isinstance(expression, PublishAnnotationExpression):
                blocks.append(self._build_publish_annotation_segment(expression, len(blocks)))
            elif isinstance(expression, RetrieveAnnotationExpression):
                blocks.append(self._build_retrieve_annotation_segment(expression, len(blocks)))
            elif isinstance(expression, CreateTableExpression):
                blocks.append(self._build_create_table_segment(expression, len(blocks)))
            elif self._is_sql_function_create(expression):
                self._register_function(expression)
            else:
                raise ValueError(f"Unexpected expression type: {type(expression).__name__}")

        function_blocks = self._build_required_function_segments(blocks)
        all_blocks = [*function_blocks, *blocks]

        if not all_blocks:
            raise ValueError("No statements were parsed from the script")
        add_segment_dependencies(all_blocks)
        ordered_blocks = topologically_sort_segments(all_blocks)
        self._validate_publish_reports_contract(ordered_blocks)
        return ordered_blocks

    @classmethod
    def discover_external_function_references(
        cls,
        script: str,
        *,
        registered_functions: Optional[List[RegisteredRuntimeFunction | Dict[str, Any]]] = None,
    ) -> List[str]:
        parser = cls(script, registered_functions=registered_functions)
        return parser._discover_external_function_references()

    def _parse_expressions(self) -> List[exp.Expression]:
        normalized_script = self._normalize_script_for_parsing(self._script)
        previous_transpile = AgentCICDSqlDialect.Parser._agentcicd_enable_sql_transpile
        previous_function_semantics = AgentCICDSqlDialect.Parser._agentcicd_enable_function_semantics
        AgentCICDSqlDialect.Parser._agentcicd_enable_sql_transpile = self._enable_sql_transpile
        AgentCICDSqlDialect.Parser._agentcicd_enable_function_semantics = self._enable_function_semantics
        try:
            return sqlglot.parse(normalized_script, read=AgentCICDSqlDialect)
        finally:
            AgentCICDSqlDialect.Parser._agentcicd_enable_sql_transpile = previous_transpile
            AgentCICDSqlDialect.Parser._agentcicd_enable_function_semantics = previous_function_semantics

    def _discover_external_function_references(self) -> List[str]:
        expressions = self._parse_expressions()

        query_expressions: List[exp.Expression] = []
        for expression in expressions:
            if isinstance(expression, exp.Semicolon):
                continue
            if self._is_sql_function_create(expression):
                self._register_function(expression)
                continue
            query_expressions.append(expression)

        references: dict[str, str] = {}

        def _record_expression(expression: exp.Expression) -> None:
            for node in expression.find_all(exp.Func):
                name = self._function_call_name(node)
                if not name:
                    continue
                if self._resolve_function_definition(name):
                    continue
                registered = self._resolve_registered_function_definition(name)
                if registered is None:
                    continue
                canonical = str(registered.call_name or registered.name).strip().lower()
                if canonical:
                    references[canonical] = canonical

        for expression in query_expressions:
            _record_expression(expression)
        for definition in self._functions.values():
            _record_expression(definition.expression)

        return sorted(references.values())

    @staticmethod
    def _segment_id(index: int, block_type: SqlSegmentType, name: str) -> str:
        normalized_name = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "segment"
        return f"{index:04d}_{block_type.value.lower()}_{normalized_name}"

    @staticmethod
    def _identifier_name(expr: exp.Expression) -> str:
        if isinstance(expr, exp.Identifier):
            return expr.this
        return expr.sql(dialect="spark")

    @staticmethod
    def _literal_value(expr: exp.Expression) -> str:
        if isinstance(expr, exp.Literal):
            return expr.this
        return expr.sql(dialect="spark")

    @staticmethod
    def _options_expr_to_dict(expression: Optional[exp.Expression]) -> Dict[str, Union[str, List[str]]]:
        options: Dict[str, Union[str, List[str]]] = {}
        if not isinstance(expression, exp.Array):
            return options
        for pair in expression.expressions:
            if not isinstance(pair, exp.Tuple):
                continue
            key = AgentCICDScriptParserCoreMixin._identifier_name(pair.this).upper()
            value = AgentCICDScriptParserCoreMixin._option_value_to_python(pair.expression)
            options[key] = value
        return options

    @staticmethod
    def _option_value_to_python(expression: exp.Expression) -> Union[str, List[str]]:
        if isinstance(expression, exp.Array):
            values: List[str] = []
            for element in expression.expressions:
                normalized = AgentCICDScriptParserCoreMixin._option_value_to_python(element)
                if isinstance(normalized, list):
                    values.extend(normalized)
                else:
                    values.append(normalized)
            return values
        if isinstance(expression, exp.Literal):
            return expression.this
        if isinstance(expression, exp.Identifier):
            return expression.this
        if isinstance(expression, exp.Number):
            return expression.this
        return expression.sql(dialect="spark")

    @staticmethod
    def _is_sql_function_create(expression: exp.Expression) -> bool:
        return isinstance(expression, exp.Create) and str(expression.args.get("kind") or "").upper() == "FUNCTION"
