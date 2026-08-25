from __future__ import annotations

from agentcicd.sql.parsing import parser as _parser

for _name, _value in vars(_parser).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

class AgentCICDScriptParserFunctionMixin:
    @classmethod
    def _parse_sql_function_definition_from_source(
        cls,
        source_text: str,
        *,
        enable_function_semantics: bool = True,
    ) -> Optional[_SqlFunctionDefinition]:
        if not source_text.strip():
            return None
        normalized_script = cls._normalize_script_for_parsing(source_text)
        previous_transpile = AgentCICDSqlDialect.Parser._agentcicd_enable_sql_transpile
        previous_function_semantics = AgentCICDSqlDialect.Parser._agentcicd_enable_function_semantics
        AgentCICDSqlDialect.Parser._agentcicd_enable_sql_transpile = False
        AgentCICDSqlDialect.Parser._agentcicd_enable_function_semantics = enable_function_semantics
        try:
            expressions = sqlglot.parse(normalized_script, read=AgentCICDSqlDialect)
        finally:
            AgentCICDSqlDialect.Parser._agentcicd_enable_sql_transpile = previous_transpile
            AgentCICDSqlDialect.Parser._agentcicd_enable_function_semantics = previous_function_semantics
        temp_parser = cls("", registered_functions=[])
        for expression in expressions:
            if isinstance(expression, exp.Semicolon):
                continue
            if not cls._is_sql_function_create(expression):
                continue
            temp_parser._register_function(expression)
            if temp_parser._functions:
                return next(iter(temp_parser._functions.values()))
        return None

    def _register_function(self, expression: exp.Create) -> None:
        """Register a SQL function definition for later inlining."""
        function_expr = expression.this
        if not isinstance(function_expr, exp.UserDefinedFunction):
            raise ValueError("CREATE FUNCTION is missing a SQL function signature")

        function_name_expr = function_expr.this
        if function_name_expr is None:
            raise ValueError("CREATE FUNCTION is missing a function name")
        name = self._identifier_name(function_name_expr).upper()

        parameters = [
            _FunctionParameter(
                name=self._identifier_name(parameter.this if isinstance(parameter, exp.ColumnDef) else parameter)
            )
            for parameter in function_expr.expressions or []
            if (
                isinstance(parameter, exp.Identifier)
                or (isinstance(parameter, exp.ColumnDef) and parameter.this is not None)
            )
        ]

        body_expr = expression.expression
        normalized_body: Optional[exp.Expression]
        if isinstance(body_expr, exp.Return):
            normalized_body = body_expr.this
        else:
            normalized_body = body_expr
        if normalized_body is None:
            raise ValueError(f"CREATE FUNCTION '{name}' must include a body expression.")

        self._functions[name] = _SqlFunctionDefinition(
            name=name,
            parameters=parameters,
            expression=normalized_body,
            create_expression=expression.copy(),
        )

    def _build_required_function_segments(self, blocks: List[SqlSegment]) -> List[SqlSegment]:
        required_names = self._collect_required_function_names(blocks)
        segments: List[SqlSegment] = []
        for function_name in required_names:
            local_definition = self._functions.get(function_name)
            if local_definition is not None:
                segments.append(
                    self._build_function_segment(
                        function_name,
                        local_definition.create_expression,
                        local_definition.expression,
                        len(segments),
                    )
                )
                continue

            registered_definition = self._registered_functions.get(function_name)
            if (
                registered_definition is not None
                and registered_definition.function_type == "sql"
                and registered_definition.sql_definition is not None
            ):
                segments.append(
                    self._build_function_segment(
                        function_name,
                        registered_definition.sql_definition.create_expression,
                        registered_definition.sql_definition.expression,
                        len(segments),
                        original_sql=registered_definition.source_text,
                    )
                )
        return segments

    def _build_function_segment(
        self,
        function_name: str,
        create_expression: exp.Create,
        body_expression: exp.Expression,
        index: int,
        *,
        original_sql: Optional[str] = None,
    ) -> SqlSegment:
        create_statement = create_expression.copy()
        runtime_alias = self._sql_function_runtime_alias(function_name)
        if isinstance(create_statement.this, exp.UserDefinedFunction):
            create_statement.this.set("this", exp.to_table(runtime_alias))
        normalized_body = self._rewrite_registered_runtime_functions(body_expression.copy())
        if self._enable_function_semantics:
            normalized_body = normalize_sql_function_expression(normalized_body)
        serialized_body = (
            exp.Subquery(this=normalized_body.copy())
            if _is_query_expression(normalized_body)
            else normalized_body
        )
        original_body = create_statement.expression
        if isinstance(original_body, exp.Return):
            create_statement.set("expression", exp.Return(this=serialized_body))
        else:
            create_statement.set("expression", serialized_body)
        return SqlSegment(
            segment_id=self._segment_id(index, SqlSegmentType.CREATE_FUNCTION, function_name),
            block_type=SqlSegmentType.CREATE_FUNCTION,
            table=runtime_alias,
            statement_exprs=[create_statement],
            original_sql=original_sql,
            source_functions=[
                self._sql_function_runtime_alias(name)
                for name in self._collect_sql_function_references(body_expression)
            ],
        )

    def _collect_required_function_names(self, blocks: List[SqlSegment]) -> List[str]:
        seen: set[str] = set()
        ordered: List[str] = []
        if not blocks and self._functions:
            for function_name in self._functions.keys():
                self._visit_required_function(function_name, seen=seen, ordered=ordered, stack=())
            return ordered
        for block in blocks:
            for function_name in self._collect_sql_function_references_from_segment(block):
                self._visit_required_function(function_name, seen=seen, ordered=ordered, stack=())

        return ordered

    def _visit_required_function(
        self,
        function_name: str,
        *,
        seen: set[str],
        ordered: List[str],
        stack: tuple[str, ...],
    ) -> None:
        if function_name in seen:
            return
        if function_name in stack:
            raise ValueError(f"Recursive function call detected for '{function_name}'")

        next_stack = stack + (function_name,)
        local_definition = self._functions.get(function_name)
        if local_definition is not None:
            nested = self._collect_sql_function_references(local_definition.expression)
        else:
            registered_definition = self._registered_functions.get(function_name)
            if (
                registered_definition is None
                or registered_definition.function_type != "sql"
                or registered_definition.sql_definition is None
            ):
                return
            nested = self._collect_sql_function_references(registered_definition.sql_definition.expression)

        for dependency_name in nested:
            self._visit_required_function(dependency_name, seen=seen, ordered=ordered, stack=next_stack)

        seen.add(function_name)
        ordered.append(function_name)

    def _collect_sql_function_references_from_segment(self, segment: SqlSegment) -> List[str]:
        references: List[str] = []
        for expression in segment.statement_exprs or []:
            references.extend(self._collect_sql_function_references(expression))
        return references

    def _collect_sql_function_references(self, expression: exp.Expression) -> List[str]:
        references: list[str] = []
        seen: set[str] = set()
        for node in expression.find_all(exp.Func):
            name = self._function_call_name(node)
            if not name:
                continue
            local_definition = self._resolve_function_definition(name)
            if local_definition is not None:
                resolved_name = local_definition[0]
                if resolved_name not in seen:
                    seen.add(resolved_name)
                    references.append(resolved_name)
                continue
            registered_definition = self._resolve_registered_function_definition(name)
            if (
                registered_definition is None
                or registered_definition.function_type != "sql"
                or registered_definition.sql_definition is None
            ):
                continue
            resolved_name = registered_definition.name.upper()
            if resolved_name not in seen:
                seen.add(resolved_name)
                references.append(resolved_name)
        return references

    def _inline_functions(self, expression: exp.Expression, stack: Optional[tuple[str, ...]] = None) -> exp.Expression:
        if (
            not self._functions
            and not self._registered_functions
            and not self._expression_contains_runtime_function_alias(expression)
        ):
            return expression
        stack = stack or ()

        def _apply(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Func):
                name = self._function_call_name(node)
                resolved = self._resolve_function_definition(name) if name else None
                if resolved:
                    resolved_name, definition = resolved
                    if resolved_name in stack:
                        raise ValueError(f"Recursive function call detected for '{definition.name}'")
                    inlined = self._inline_function_call(node, definition)
                    return self._inline_functions(inlined, stack + (resolved_name,))
                registered_definition = self._resolve_registered_function_definition(name) if name else None
                if registered_definition:
                    if registered_definition.function_type == "sql" and registered_definition.sql_definition is not None:
                        resolved_name = registered_definition.name.upper()
                        if resolved_name in stack:
                            raise ValueError(
                                f"Recursive function call detected for '{registered_definition.name}'"
                            )
                        inlined = self._inline_function_call(node, registered_definition.sql_definition)
                        return self._inline_functions(inlined, stack + (resolved_name,))
                    return self._rewrite_registered_function_call(node, registered_definition)
                if name and self._looks_like_runtime_function_alias(name):
                    return node
                if self._call_has_keyword_arguments(node):
                    raise ValueError(f"Keyword arguments require a known function signature for '{name or node.sql(dialect='spark')}'")
            return node

        return expression.copy().transform(_apply)

    def _rewrite_registered_runtime_functions(self, expression: exp.Expression) -> exp.Expression:
        if (
            not self._functions
            and not self._registered_functions
            and not self._expression_contains_runtime_function_alias(expression)
        ):
            return expression

        def _apply(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Func):
                return node
            name = self._function_call_name(node)
            if not name:
                return node
            local_definition = self._resolve_function_definition(name)
            if local_definition is not None:
                resolved_name, definition = local_definition
                return self._rewrite_sql_function_call(
                    node,
                    parameters=definition.parameters,
                    call_name=name,
                    runtime_name=self._sql_function_runtime_alias(resolved_name),
                )
            registered_definition = self._resolve_registered_function_definition(name)
            if registered_definition is None:
                if self._looks_like_runtime_function_alias(name):
                    return node
                if self._call_has_keyword_arguments(node):
                    raise ValueError(
                        f"Keyword arguments require a known function signature for '{name or node.sql(dialect='spark')}'"
                    )
                return node
            if registered_definition.function_type == "sql" and registered_definition.sql_definition is not None:
                return self._rewrite_sql_function_call(
                    node,
                    parameters=registered_definition.parameters,
                    call_name=registered_definition.call_name or registered_definition.name,
                    runtime_name=self._sql_function_runtime_alias(registered_definition.name),
                )
            return self._rewrite_registered_function_call(node, registered_definition)

        return expression.copy().transform(_apply)

    def _rewrite_sql_function_call(
        self,
        call_expression: exp.Func,
        *,
        parameters: List[_FunctionParameter],
        call_name: str,
        runtime_name: str,
    ) -> exp.Expression:
        display_sql = (
            f"{call_name}({', '.join(argument.sql(dialect='spark') for argument in list(call_expression.expressions or []))})"
            if call_name
            else call_expression.sql(dialect="spark")
        )
        ordered_bindings = self._bind_function_arguments(
            list(call_expression.expressions or []),
            parameters,
            call_name,
            call_display_sql=display_sql,
        )
        arguments_sql = ", ".join(argument.sql(dialect="spark") for _, argument in ordered_bindings)
        return _parse_scalar_expression(f"{runtime_name}({arguments_sql})")

    def _sql_function_runtime_alias(self, canonical_name: str) -> str:
        registered_definition = self._registered_functions.get(canonical_name.upper())
        if (
            registered_definition is not None
            and registered_definition.function_type == "sql"
            and registered_definition.runtime_alias
        ):
            return registered_definition.runtime_alias
        return canonical_name.strip().lower().replace(".", "_")
