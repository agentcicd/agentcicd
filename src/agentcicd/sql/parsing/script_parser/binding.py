from __future__ import annotations

from agentcicd.sql.parsing import parser as _parser
from agentcicd.sql.udf_registry import load_builtin_udfs

for _name, _value in vars(_parser).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

class AgentCICDScriptParserBindingMixin:
    def _build_registered_function_index(
        self,
        items: List[RegisteredRuntimeFunction],
    ) -> Dict[str, _RegisteredFunctionDefinition]:
        indexed: Dict[str, _RegisteredFunctionDefinition] = {}
        for item in items:
            name = item.name
            function_type = item.kind
            call_name = item.call_name
            runtime_alias = item.runtime_alias
            source_text = item.source_text
            if not name:
                continue
            parameters: List[_FunctionParameter] = []
            for entry in item.signature:
                param_name = entry.name.strip()
                if not param_name:
                    continue
                parameters.append(
                    _FunctionParameter(
                        name=param_name,
                        type_sql=entry.type_sql,
                        has_default=entry.has_default,
                    )
                )
            definition = _RegisteredFunctionDefinition(
                function_id=item.id,
                name=name,
                function_type=function_type,
                call_name=call_name,
                runtime_alias=runtime_alias,
                parameters=parameters,
                operations=[operation.to_dict() for operation in item.operations],
                source_text=source_text,
                sql_definition=(
                    self._parse_sql_function_definition_from_source(
                        source_text,
                        enable_function_semantics=self._enable_function_semantics,
                    )
                    if function_type == "sql"
                    else None
                ),
            )
            for key in {name, call_name, runtime_alias}:
                normalized_key = key.strip().upper()
                if normalized_key:
                    indexed[normalized_key] = definition
        return indexed

    def _register_builtin_function_signatures(self) -> None:
        for udf_name, udf_cls in load_builtin_udfs().items():
            normalized_name = udf_name.upper()
            if normalized_name in self._registered_functions:
                continue
            runtime_alias = udf_name.replace(".", "_")
            parameters = [
                _FunctionParameter(
                    name=param.name,
                    type_sql=param.type_sql,
                    has_default=not param.required,
                )
                for param in udf_cls().signature()
            ]
            definition = _RegisteredFunctionDefinition(
                function_id=f"builtin.{udf_name}",
                name=udf_name,
                function_type="py",
                call_name=udf_name,
                runtime_alias=runtime_alias,
                parameters=parameters,
                operations=[],
                allow_short_name_match=False,
            )
            keys = {udf_name, runtime_alias}
            if udf_name == "string.extract_from_fence":
                keys.add(udf_name.split(".")[-1])
            for key in keys:
                normalized_key = key.strip().upper()
                if normalized_key:
                    self._registered_functions[normalized_key] = definition

    def _resolve_function_definition(
        self,
        name: str,
    ) -> Optional[tuple[str, _SqlFunctionDefinition]]:
        """Resolve function definitions by exact or unique qualified suffix match."""
        normalized = name.upper()
        direct = self._functions.get(normalized)
        if direct:
            return normalized, direct

        short_name = normalized.split(".")[-1]
        candidates = [
            key for key in self._functions.keys() if key == short_name or key.endswith(f".{short_name}")
        ]
        if len(candidates) == 1:
            matched = candidates[0]
            return matched, self._functions[matched]
        if len(candidates) > 1:
            raise ValueError(
                f"Ambiguous SQL function reference '{name}'. Candidates: {', '.join(sorted(candidates))}"
            )
        return None

    def _resolve_registered_function_definition(
        self,
        name: str,
    ) -> Optional[_RegisteredFunctionDefinition]:
        normalized = name.upper()
        direct = self._registered_functions.get(normalized)
        if direct:
            return direct

        short_name = normalized.split(".")[-1]
        candidates: dict[str, _RegisteredFunctionDefinition] = {}
        for key, definition in self._registered_functions.items():
            if not definition.allow_short_name_match:
                continue
            if key == short_name or key.endswith(f".{short_name}"):
                candidates[definition.name.upper()] = definition

        if len(candidates) == 1:
            return next(iter(candidates.values()))
        if len(candidates) > 1:
            raise ValueError(
                f"Ambiguous registered function reference '{name}'. Candidates: {', '.join(sorted(candidates.keys()))}"
            )
        return None

    @staticmethod
    def _function_call_name(func: exp.Func) -> Optional[str]:
        sql_name = func.sql_name()
        if sql_name and sql_name.upper() != "ANONYMOUS":
            normalized = sql_name.upper()
            if normalized.startswith("SQLUDF."):
                return normalized[len("SQLUDF."):]
            return normalized
        identifier = getattr(func, "this", None)
        if isinstance(identifier, str):
            normalized = identifier.upper()
            if normalized.startswith("SQLUDF."):
                return normalized[len("SQLUDF."):]
            return normalized
        if isinstance(identifier, exp.Identifier):
            normalized = identifier.this.upper()
            if normalized.startswith("SQLUDF."):
                return normalized[len("SQLUDF."):]
            return normalized
        return None

    def _inline_function_call(
        self,
        call_expression: exp.Func,
        definition: _SqlFunctionDefinition,
    ) -> exp.Expression:
        call_display_sql = call_expression.sql(dialect="spark")
        ordered_bindings = self._bind_function_arguments(
            list(call_expression.expressions or []),
            definition.parameters,
            definition.name,
            call_display_sql=call_display_sql,
        )
        arg_map = {
            parameter.name.upper(): argument
            for parameter, argument in ordered_bindings
        }
        body = definition.expression.copy()

        def _replace(node: exp.Expression) -> exp.Expression:
            if is_keyword_argument_target(node):
                return node
            match_name = self._match_parameter_name(node)
            if match_name and match_name in arg_map:
                return arg_map[match_name].copy()
            return node

        return body.transform(_replace)

    def _rewrite_registered_function_call(
        self,
        call_expression: exp.Func,
        definition: _RegisteredFunctionDefinition,
    ) -> exp.Expression:
        original_arguments = list(call_expression.expressions or [])
        display_sql = (
            f"{definition.call_name}({', '.join(argument.sql(dialect='spark') for argument in original_arguments)})"
            if definition.call_name
            else call_expression.sql(dialect="spark")
        )
        ordered_bindings = self._bind_function_arguments(
            original_arguments,
            definition.parameters,
            definition.call_name or definition.name,
            call_display_sql=display_sql,
        )
        if definition.function_type == "sql":
            rewritten = call_expression.copy()
            rewritten.set(
                "expressions",
                [argument.copy() for _, argument in ordered_bindings],
            )
            return rewritten
        runtime_alias = definition.runtime_alias or definition.name.strip().lower().replace(".", "_")
        if definition.function_type in {"py", "python", "pyudf"}:
            bound_by_name = {
                parameter.name.upper(): argument
                for parameter, argument in ordered_bindings
            }
            arguments_sql = ", ".join(
                (
                    bound_by_name[parameter.name.upper()].sql(dialect="spark")
                    if parameter.name.upper() in bound_by_name
                    else exp.Null().sql(dialect="spark")
                )
                for parameter in definition.parameters
            )
            rewritten = _parse_scalar_expression(f"{runtime_alias}({arguments_sql})")
            rewritten.meta["agentcicd_display_sql"] = display_sql
            return rewritten
        named_struct = self._build_named_struct_expression(ordered_bindings)
        rewritten = _parse_scalar_expression(f"{runtime_alias}({named_struct.sql(dialect='spark')})")
        rewritten.meta["agentcicd_display_sql"] = display_sql
        return rewritten

    @staticmethod
    def _call_has_keyword_arguments(call_expression: exp.Func) -> bool:
        return any(isinstance(argument, exp.EQ) for argument in list(call_expression.expressions or []))

    def _bind_function_arguments(
        self,
        arguments: List[exp.Expression],
        parameters: List[_FunctionParameter],
        function_name: str,
        *,
        call_display_sql: Optional[str] = None,
    ) -> List[tuple[_FunctionParameter, exp.Expression]]:
        call_suffix = f" in call `{call_display_sql}`" if call_display_sql else ""
        if not parameters and arguments:
            raise ValueError(f"Function '{function_name}' does not accept arguments{call_suffix}")
        parameter_by_name = {
            parameter.name.upper(): parameter
            for parameter in parameters
        }
        bindings: Dict[str, exp.Expression] = {}
        seen_keyword = False
        positional_index = 0

        for argument in arguments:
            if isinstance(argument, exp.EQ):
                seen_keyword = True
                keyword_name = keyword_argument_name(argument).upper()
                parameter = parameter_by_name.get(keyword_name)
                if parameter is None:
                    accepted = ", ".join(parameter.name for parameter in parameters) or "none"
                    raise ValueError(
                        f"Function '{function_name}' does not accept keyword argument "
                        f"'{keyword_name.lower()}'{call_suffix}. Accepted parameters: {accepted}"
                    )
                if keyword_name in bindings:
                    raise ValueError(
                        f"Function '{function_name}' got multiple values for argument "
                        f"'{keyword_name.lower()}'{call_suffix}"
                    )
                bindings[keyword_name] = argument.expression.copy()
                continue

            if seen_keyword:
                raise ValueError(
                    f"Function '{function_name}' cannot use positional arguments after "
                    f"keyword arguments{call_suffix}"
                )
            if positional_index >= len(parameters):
                accepted = ", ".join(parameter.name for parameter in parameters) or "none"
                raise ValueError(
                    f"Function '{function_name}' expects at most {len(parameters)} arguments "
                    f"but received {len(arguments)}{call_suffix}. Accepted parameters: {accepted}"
                )
            parameter = parameters[positional_index]
            keyword_name = parameter.name.upper()
            if keyword_name in bindings:
                raise ValueError(
                    f"Function '{function_name}' got multiple values for argument "
                    f"'{parameter.name}'{call_suffix}"
                )
            bindings[keyword_name] = argument.copy()
            positional_index += 1

        missing_required = [
            parameter.name
            for parameter in parameters
            if not parameter.has_default and parameter.name.upper() not in bindings
        ]
        if missing_required:
            missing = ", ".join(missing_required)
            raise ValueError(
                f"Function '{function_name}' is missing required arguments: "
                f"{missing}{call_suffix}"
            )

        ordered_bindings: List[tuple[_FunctionParameter, exp.Expression]] = []
        for parameter in parameters:
            bound = bindings.get(parameter.name.upper())
            if bound is not None:
                ordered_bindings.append((parameter, bound))
        return ordered_bindings

    @staticmethod
    def _build_named_struct_expression(
        ordered_bindings: List[tuple[_FunctionParameter, exp.Expression]],
    ) -> exp.Expression:
        args_sql: List[str] = []
        for parameter, argument in ordered_bindings:
            args_sql.append(f"'{parameter.name}'")
            args_sql.append(argument.sql(dialect="spark"))
        return _parse_scalar_expression(f"named_struct({', '.join(args_sql)})")

    @staticmethod
    def _match_parameter_name(node: exp.Expression) -> Optional[str]:
        if isinstance(node, exp.Identifier):
            return node.this.upper()
        if isinstance(node, exp.Column):
            table_ref = node.args.get("table")
            if table_ref:
                return None
            column_identifier = node.args.get("this")
            if isinstance(column_identifier, exp.Identifier):
                return column_identifier.this.upper()
        var_cls = getattr(exp, "Var", None)
        if var_cls and isinstance(node, var_cls):
            var_identifier = getattr(node, "this", None)
            if isinstance(var_identifier, str):
                return var_identifier.upper()
            if isinstance(var_identifier, exp.Identifier):
                return var_identifier.this.upper()
        return None
