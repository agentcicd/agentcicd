from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional, Union

import sqlglot
from sqlglot import expressions as exp
from sqlglot.dialects import spark as sqlglotspark
from sqlglot.tokens import Token
from sqlglot.tokens import TokenType

from agentcicd.sql.parsing.sql_segments import (
    SqlSegment,
    SqlSegmentType,
    add_segment_dependencies,
    topologically_sort_segments,
)
from agentcicd.sql.contracts import RegisteredRuntimeFunction
from agentcicd.sql.parsing.function_args import (
    is_keyword_argument_target,
    keyword_argument_name,
)
from agentcicd.sql.parsing.runtime_signature_registry import register_runtime_signature_specs
from agentcicd.sql.ir.functions import coerce_registered_runtime_specs
from agentcicd.sql.parsing.python_to_sql_lexer import normalize_python_syntax
from agentcicd.sql.parsing.sql_transpiler import (
    normalize_sql_function_expression,
    normalize_sql_function_step_expression,
    transpile_query_expression_with_options,
)
from agentcicd.sql.json_semantics import is_variant_expression


_RAW_MACRO_PATTERN = re.compile(r"\$([A-Z][A-Z0-9_]*)\b")
_ROW_LIMIT_STANDALONE_PATTERN = re.compile(r"(?m)^([ \t]*)\$LIMIT_ROWS\s*;?\s*$")
_VARIANT_PATH_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _normalize_macro_placeholders(sql: str) -> str:
    sql = _ROW_LIMIT_STANDALONE_PATTERN.sub(r"\1LIMIT __agentcicd_macro_limit_rows__;", sql)
    result: list[str] = []
    i = 0
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False

    while i < len(sql):
        if in_line_comment:
            result.append(sql[i])
            if sql[i] == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if sql[i:i + 2] == "*/":
                result.append("*/")
                i += 2
                in_block_comment = False
            else:
                result.append(sql[i])
                i += 1
            continue

        if not in_single and not in_double:
            if sql[i:i + 2] == "--":
                result.append("--")
                i += 2
                in_line_comment = True
                continue
            if sql[i:i + 2] == "/*":
                result.append("/*")
                i += 2
                in_block_comment = True
                continue

        char = sql[i]

        if char == "'" and not in_double:
            in_single = not in_single
            result.append(char)
            i += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            result.append(char)
            i += 1
            continue

        if not in_single and not in_double and char == "$":
            match = _RAW_MACRO_PATTERN.match(sql, i)
            if match:
                result.append(f"__agentcicd_macro_{match.group(1).lower()}__")
                i = match.end()
                continue

        result.append(char)
        i += 1

    return "".join(result)


def _rewrite_variant_colon_access(sql: str) -> str:
    result: list[str] = []
    i = 0
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False

    while i < len(sql):
        if in_line_comment:
            result.append(sql[i])
            if sql[i] == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if sql[i:i + 2] == "*/":
                result.append("*/")
                i += 2
                in_block_comment = False
            else:
                result.append(sql[i])
                i += 1
            continue

        if not in_single and not in_double:
            if sql[i:i + 2] == "--":
                result.append("--")
                i += 2
                in_line_comment = True
                continue
            if sql[i:i + 2] == "/*":
                result.append("/*")
                i += 2
                in_block_comment = True
                continue

        char = sql[i]

        if char == "'" and not in_double:
            in_single = not in_single
            result.append(char)
            i += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            result.append(char)
            i += 1
            continue

        if char == ":" and not in_single and not in_double:
            if i + 1 < len(sql) and sql[i + 1] == "=":
                result.append(char)
                i += 1
                continue

            base_start = _find_variant_base_start(sql, i)
            if base_start is not None:
                path, path_end = _parse_variant_path(sql, i)
                if path is not None:
                    base_sql = sql[base_start:i].rstrip()
                    if base_sql:
                        del result[-(i - base_start):]
                        result.append(
                            f"__agentcicd_colon_path({base_sql}, {exp.Literal.string(path).sql(dialect='spark')})"
                        )
                        i = path_end
                        continue

        result.append(char)
        i += 1

    return "".join(result)


def _find_variant_base_start(sql: str, colon_index: int) -> Optional[int]:
    i = colon_index - 1
    while i >= 0 and sql[i].isspace():
        i -= 1
    if i < 0:
        return None

    paren_depth = 0
    bracket_depth = 0
    while i >= 0:
        char = sql[i]
        if char == ")":
            paren_depth += 1
        elif char == "(":
            if paren_depth == 0 and bracket_depth == 0:
                break
            if paren_depth > 0:
                paren_depth -= 1
        elif char == "]":
            bracket_depth += 1
        elif char == "[":
            if bracket_depth > 0:
                bracket_depth -= 1
        elif paren_depth == 0 and bracket_depth == 0 and (char.isspace() or char in ",;+*/%=&|^!<>?-"):
            break
        i -= 1

    base_start = i + 1
    while base_start < colon_index and sql[base_start].isspace():
        base_start += 1
    if base_start >= colon_index:
        return None
    return base_start


def _parse_variant_path(sql: str, colon_index: int) -> tuple[Optional[str], int]:
    i = colon_index
    path_parts: list[str] = ["$"]
    parsed_any = False

    while i < len(sql):
        while i < len(sql) and sql[i].isspace():
            i += 1
        if i >= len(sql) or sql[i] != ":":
            break
        i += 1
        while i < len(sql) and sql[i].isspace():
            i += 1

        if i < len(sql) and sql[i] == "[":
            bracket_sql, bracket_end = _parse_variant_bracket(sql, i)
            if bracket_sql is None:
                return (None, colon_index)
            path_parts.append(bracket_sql)
            i = bracket_end
        else:
            identifier_match = _VARIANT_PATH_IDENTIFIER_PATTERN.match(sql, i)
            if not identifier_match:
                return (None, colon_index)
            path_parts.append(f".{identifier_match.group(0)}")
            i = identifier_match.end()
        parsed_any = True

        while True:
            while i < len(sql) and sql[i].isspace():
                i += 1

            if i < len(sql) and sql[i] == "[":
                bracket_sql, bracket_end = _parse_variant_bracket(sql, i)
                if bracket_sql is None:
                    return (None, colon_index)
                path_parts.append(bracket_sql)
                i = bracket_end
                continue

            if i < len(sql) and sql[i] == ".":
                i += 1
                while i < len(sql) and sql[i].isspace():
                    i += 1
                dotted_identifier = _VARIANT_PATH_IDENTIFIER_PATTERN.match(sql, i)
                if not dotted_identifier:
                    return (None, colon_index)
                path_parts.append(f".{dotted_identifier.group(0)}")
                i = dotted_identifier.end()
                continue
            break

    if not parsed_any:
        return (None, colon_index)
    return ("".join(path_parts), i)


def _parse_variant_bracket(sql: str, start_index: int) -> tuple[Optional[str], int]:
    end_index = start_index + 1
    while end_index < len(sql) and sql[end_index] != "]":
        end_index += 1
    if end_index >= len(sql):
        return (None, start_index)

    inner = sql[start_index + 1:end_index].strip()
    if not inner:
        return (None, start_index)
    if re.fullmatch(r"-?\d+", inner):
        return (f"[{inner}]", end_index + 1)
    return (None, start_index)


def _is_query_expression(expression: exp.Expression) -> bool:
    """Check if an expression is a query (SELECT, WITH, etc.)."""
    return isinstance(
        expression,
        (
            exp.Select,
            exp.With,
            exp.Union,
            exp.Except,
            exp.Intersect,
            exp.SetOperation,
            exp.Table,
            exp.Subquery,
            exp.Values,
        ),
    )


def _parse_scalar_expression(sql_text: str) -> exp.Expression:
    normalized_sql_text = normalize_python_syntax(sql_text)
    normalized_sql_text = _rewrite_variant_colon_access(normalized_sql_text)
    parsed = sqlglot.parse_one(f"SELECT {normalized_sql_text}", read="spark")
    if not isinstance(parsed, exp.Select) or not parsed.expressions:
        raise ValueError(f"Invalid SQL expression in function body: {sql_text}")
    return parsed.expressions[0]


class CreateTableExpression(exp.Expression):
    arg_types = {
        "table": True,
        "phase_type": True,
        "batch_size": False,
        "query": True,
    }


class LoadExpression(exp.Expression):
    arg_types = {
        "table": True,
        "path": True,
        "options": False,
        "limit": False,
    }


class SaveExpression(exp.Expression):
    arg_types = {
        "table": True,
        "path": True,
        "options": False,
    }


class PublishExpression(exp.Expression):
    arg_types = {
        "table": True,
        "destination": True,
        "component": True,
        "chart_type": False,
        "report_options": False,
    }


class PublishDatasetExpression(exp.Expression):
    arg_types = {
        "table": True,
        "dataset_name": False,
    }


class PublishAnnotationExpression(exp.Expression):
    arg_types = {
        "table": True,
        "queue_name": True,
        "alias": False,
        "options": False,
    }


class RetrieveAnnotationExpression(exp.Expression):
    arg_types = {
        "table": True,
        "source_ref": False,
        "annotation_request_id": False,
    }


class AgentCICDSqlDialect(sqlglotspark.Spark):
    class Parser(sqlglotspark.Spark.Parser):
        _agentcicd_enable_sql_transpile = False
        _agentcicd_enable_function_semantics = True

        def _parse_statement(self):
            if self._match_texts("LOAD"):
                return self._parse_load_statement()
            if self._match_texts("SAVE"):
                return self._parse_save_statement()
            if self._match_texts("PUBLISH"):
                return self._parse_publish_statement()
            if self._match_texts("RETRIEVE"):
                return self._parse_retrieve_annotation_statement()
            # Handle CREATE BATCH TABLE and CREATE STREAM TABLE
            if self._match(TokenType.CREATE):
                # Check if next token is BATCH or STREAM
                if self._curr and self._curr.text.upper() in {"BATCH", "STREAM"}:
                    return self._parse_create_table_block()
                # Check if it's CREATE FUNCTION
                if self._curr and self._curr.text.upper() == "FUNCTION":
                    if self._has_rich_function_body_ahead():
                        return self._parse_create_function_block()
                    self._retreat(self._index - 1)
                    return super()._parse_statement()
                # Reset if not a special create statement
                self._retreat(self._index - 1)
            return super()._parse_statement()

        def _has_rich_function_body_ahead(self) -> bool:
            for token in self._tokens[self._index:]:
                if token.token_type == TokenType.SEMICOLON:
                    break
                if token.token_type == TokenType.COLON_EQ:
                    return True
            return False

        def _parse_create_table_block(self):
            """Parse CREATE BATCH/STREAM TABLE ... SELECT ... syntax."""
            # We already consumed CREATE, now consume BATCH/STREAM
            block_mode_token = self._curr
            if block_mode_token is None:
                self.raise_error("Expected BATCH or STREAM after CREATE")
            block_mode_text = block_mode_token.text.upper()  # type: ignore
            if block_mode_text not in {"BATCH", "STREAM"}:
                self.raise_error(f"Unsupported block mode '{block_mode_text}'")
            self._advance()  # Consume BATCH/STREAM token

            # Expect TABLE keyword
            if not self._match_texts("TABLE"):
                self.raise_error(f"Expected TABLE after CREATE {block_mode_text}")

            # Parse table name (target)
            target = self._parse_id_var()
            if target is None:
                self.raise_error("Expected table name after CREATE {block_mode_text} TABLE")
            # Extract table name from identifier
            if isinstance(target, exp.Identifier):
                target_table = target.this
            else:
                target_table = target.sql(dialect="spark")

            # Parse OPTIONS clause if present
            options_expr = None
            if self._match_texts("OPTIONS"):
                if not self._match(TokenType.L_PAREN):
                    self.raise_error("Expected '(' after OPTIONS")
                options_expr = self._parse_option_pairs()
                if not self._match(TokenType.R_PAREN):
                    self.raise_error("Expected ')' after OPTIONS")

            # Extract batch_size from options if present
            batch_size_value = None
            if options_expr:
                for pair in options_expr.expressions:
                    if isinstance(pair, exp.Tuple):
                        if isinstance(pair.this, exp.Identifier):
                            key = pair.this.this.upper()
                        else:
                            key = pair.this.sql(dialect="spark").upper()
                        if key == "BATCH_SIZE":
                            if isinstance(pair.expression, exp.Literal):
                                batch_size_value = int(pair.expression.this)

            # Parse the SELECT statement that follows
            query_statement = super()._parse_statement()
            if query_statement is None or not _is_query_expression(query_statement):
                self.raise_error(f"Expected SELECT/WITH query after CREATE {block_mode_text} TABLE definition")

            return CreateTableExpression(
                table=exp.Identifier(this=target_table),
                phase_type=exp.Identifier(this=block_mode_text),
                batch_size=(
                    exp.Literal.number(batch_size_value) if batch_size_value is not None else None
                ),
                query=query_statement,
            )

        def _parse_create_function_block(self):
            """Parse CREATE FUNCTION with AgentCICD rich-body sugar and normalize to Spark SQL."""
            # We already consumed CREATE, now consume FUNCTION
            self._advance()  # Consume FUNCTION token

            # Parse function name (supports dotted names like module.function)
            func_name_parts: List[str] = []
            first_part = self._parse_id_var()
            if first_part is None:
                self.raise_error("Expected function name after CREATE FUNCTION")
            if isinstance(first_part, exp.Identifier):
                func_name_parts.append(first_part.this)
            else:
                func_name_parts.append(first_part.sql(dialect="spark"))
            while self._match(TokenType.DOT):
                next_part = self._parse_id_var()
                if next_part is None:
                    self.raise_error("Expected identifier after '.' in function name")
                if isinstance(next_part, exp.Identifier):
                    func_name_parts.append(next_part.this)
                else:
                    func_name_parts.append(next_part.sql(dialect="spark"))
            func_name = exp.Identifier(this=".".join(func_name_parts))

            # Parse parameters - expect (param1 TYPE, param2 TYPE, ...)
            if not self._match(TokenType.L_PAREN):
                self.raise_error("Expected '(' after function name")

            parameters: List[str] = []
            if not self._match(TokenType.R_PAREN):
                while True:
                    param = self._parse_id_var()
                    if param is None:
                        self.raise_error("Expected parameter name")
                    else:
                        parameters.append(self._identifier_sql(param))

                    # Skip optional type annotation (e.g., STRING, ARRAY<STRING>, MAP<...>).
                    # Capture type annotations so the normalized SQL function matches Spark 4.1 syntax.
                    param_type_tokens: List[str] = []
                    angle_depth = 0
                    paren_depth = 0
                    bracket_depth = 0
                    brace_depth = 0
                    while self._curr:
                        token_type = self._curr.token_type
                        if (
                            token_type in {TokenType.COMMA, TokenType.R_PAREN}
                            and angle_depth == 0
                            and paren_depth == 0
                            and bracket_depth == 0
                            and brace_depth == 0
                        ):
                            break
                        token_text = getattr(self._curr, "text", None)
                        if isinstance(token_text, str):
                            param_type_tokens.append(token_text)
                        if token_type == TokenType.LT:
                            angle_depth += 1
                        elif token_type == TokenType.GT and angle_depth > 0:
                            angle_depth -= 1
                        elif token_type == TokenType.L_PAREN:
                            paren_depth += 1
                        elif token_type == TokenType.R_PAREN:
                            if paren_depth > 0:
                                paren_depth -= 1
                        elif token_type == TokenType.L_BRACKET:
                            bracket_depth += 1
                        elif token_type == TokenType.R_BRACKET and bracket_depth > 0:
                            bracket_depth -= 1
                        elif token_type == TokenType.L_BRACE:
                            brace_depth += 1
                        elif token_type == TokenType.R_BRACE and brace_depth > 0:
                            brace_depth -= 1
                        self._advance()
                    if not param_type_tokens:
                        self.raise_error("Expected parameter type")
                    parameters[-1] = f"{parameters[-1]} {' '.join(part for part in param_type_tokens if part).strip()}"

                    if self._match(TokenType.R_PAREN):
                        break
                    if not self._match(TokenType.COMMA):
                        self.raise_error("Expected ',' or ')' in parameter list")

            # Capture optional RETURNS type clause.
            return_type_sql: Optional[str] = None
            if self._match_texts("RETURNS"):
                returns_line = getattr(self._curr, "line", None)
                return_type_tokens: List[str] = []
                angle_depth = 0
                paren_depth = 0
                bracket_depth = 0
                brace_depth = 0
                while self._curr:
                    token_text = getattr(self._curr, "text", None)
                    if isinstance(token_text, str) and token_text.upper() in ("AS", "RETURN"):
                        break
                    if (
                        returns_line is not None
                        and getattr(self._curr, "line", None) != returns_line
                        and angle_depth == 0
                        and paren_depth == 0
                        and bracket_depth == 0
                        and brace_depth == 0
                    ):
                        break
                    if isinstance(token_text, str):
                        return_type_tokens.append(token_text)
                    token_type = self._curr.token_type
                    if token_type == TokenType.LT:
                        angle_depth += 1
                    elif token_type == TokenType.GT and angle_depth > 0:
                        angle_depth -= 1
                    elif token_type == TokenType.L_PAREN:
                        paren_depth += 1
                    elif token_type == TokenType.R_PAREN and paren_depth > 0:
                        paren_depth -= 1
                    elif token_type == TokenType.L_BRACKET:
                        bracket_depth += 1
                    elif token_type == TokenType.R_BRACKET and bracket_depth > 0:
                        bracket_depth -= 1
                    elif token_type == TokenType.L_BRACE:
                        brace_depth += 1
                    elif token_type == TokenType.R_BRACE and brace_depth > 0:
                        brace_depth -= 1
                    self._advance()
                return_type_sql = " ".join(part for part in return_type_tokens if part).strip()

            characteristics = self._parse_function_characteristics()
            body_starts_with_return = False
            if self._match_texts("AS"):
                pass
            elif self._match_texts("RETURN"):
                body_starts_with_return = True
            elif self._is_rich_function_body_start():
                pass
            else:
                self.raise_error("Expected AS or RETURN after function parameters")

            body_expression = self._parse_rich_function_body_expression(
                parameter_names=[parameter.split(" ", 1)[0] for parameter in parameters],
                initial_statement_kind="return" if body_starts_with_return else None,
            )

            create_expression = self._build_rich_function_create_expression(
                function_name=func_name.this,
                parameters=parameters,
                return_type_sql=return_type_sql,
                characteristics=characteristics,
                body_expression=body_expression,
                normalize_body=False,
            )
            return create_expression

        def _is_rich_function_body_start(self) -> bool:
            if self._curr is None:
                return False
            token_text = getattr(self._curr, "text", "")
            if isinstance(token_text, str) and token_text.upper() == "RETURN":
                return True
            current_line = getattr(self._curr, "line", None)
            next_token = self._next
            if (
                self._curr.token_type in {TokenType.VAR, TokenType.IDENTIFIER}
                and next_token is not None
                and getattr(next_token, "line", None) == current_line
                and next_token.token_type == TokenType.COLON_EQ
            ):
                return False
            return False

        def _parse_rich_function_body_expression(
            self,
            *,
            parameter_names: List[str],
            initial_statement_kind: Optional[str],
        ) -> exp.Expression:
            body_tokens = self._collect_function_body_tokens()
            if not body_tokens:
                self.raise_error("Expected function body expression")

            line_groups = self._group_tokens_by_line(body_tokens)
            current_kind = initial_statement_kind
            current_name: Optional[str] = None
            current_tokens: List[Token] = []
            return_expression: Optional[exp.Expression] = None

            def _flush_current() -> None:
                nonlocal current_kind, current_name, current_tokens, return_expression
                if current_kind is None:
                    return
                if not current_tokens:
                    if current_kind == "assign":
                        raise ValueError("Function assignment right-hand side expression is empty.")
                    raise ValueError("RETURN expression cannot be empty in CREATE FUNCTION body.")

                expression_sql = self.sql[current_tokens[0].start: current_tokens[-1].end + 1].strip()
                if not expression_sql:
                    if current_kind == "assign":
                        raise ValueError("Function assignment right-hand side expression is empty.")
                    raise ValueError("RETURN expression cannot be empty in CREATE FUNCTION body.")

                parsed_expression = self._parse_function_body_expression_sql(expression_sql)
                if current_kind == "assign":
                    raise ValueError("CREATE FUNCTION assignment statements are no longer supported; use RETURN with a SQL expression")
                else:
                    return_expression = parsed_expression

                current_kind = None
                current_name = None
                current_tokens = []

            for line_tokens in line_groups:
                if current_kind is not None and self._tokens_have_open_delimiters(current_tokens):
                    current_tokens.extend(line_tokens)
                    continue
                statement_header = self._classify_function_body_line(line_tokens)
                if statement_header is not None:
                    _flush_current()
                    current_kind, current_name, body_start_index = statement_header
                    current_tokens = line_tokens[body_start_index:]
                    continue

                if current_kind is None:
                    raise ValueError(
                        f"Unsupported CREATE FUNCTION body statement '{self._line_sql(line_tokens)}'. "
                        "Only 'RETURN <sql_expression>' is allowed."
                    )
                current_tokens.extend(line_tokens)

            _flush_current()

            if return_expression is None:
                self.raise_error("Expected function body expression")
            return self._build_rich_function_body_with_ctes(
                parameter_names=parameter_names,
                assignments=[],
                return_expression=return_expression,
                normalize_steps=False,
            )

        def _collect_function_body_tokens(self) -> List[Token]:
            tokens: List[Token] = []
            while self._curr is not None and self._curr.token_type != TokenType.SEMICOLON:
                tokens.append(self._curr)
                self._advance()
            return tokens

        @staticmethod
        def _group_tokens_by_line(tokens: List[Token]) -> List[List[Token]]:
            groups: List[List[Token]] = []
            current_line: Optional[int] = None
            current_tokens: List[Token] = []

            for token in tokens:
                token_line = getattr(token, "line", None)
                if current_line is None or token_line == current_line:
                    current_tokens.append(token)
                    current_line = token_line
                    continue
                groups.append(current_tokens)
                current_tokens = [token]
                current_line = token_line

            if current_tokens:
                groups.append(current_tokens)
            return groups

        @staticmethod
        def _classify_function_body_line(
            line_tokens: List[Token],
        ) -> Optional[tuple[str, Optional[str], int]]:
            if not line_tokens:
                return None
            first_token = line_tokens[0]
            first_text = getattr(first_token, "text", "")
            if isinstance(first_text, str) and first_text.upper() == "RETURN":
                return "return", None, 1
            if (
                len(line_tokens) >= 3
                and line_tokens[0].token_type in {TokenType.VAR, TokenType.IDENTIFIER}
                and line_tokens[1].token_type == TokenType.COLON_EQ
            ):
                raise ValueError(
                    f"Unsupported CREATE FUNCTION body statement '{AgentCICDSqlDialect.Parser._line_sql(line_tokens)}'. "
                    "Only 'RETURN <sql_expression>' is allowed."
                )
            if (
                len(line_tokens) >= 3
                and line_tokens[0].token_type in {TokenType.VAR, TokenType.IDENTIFIER}
                and line_tokens[1].token_type == TokenType.EQ
            ):
                raise ValueError(
                    f"Unsupported CREATE FUNCTION body statement '{AgentCICDSqlDialect.Parser._line_sql(line_tokens)}'. "
                    "Only 'RETURN <sql_expression>' is allowed."
                )
            return None

        @staticmethod
        def _line_sql(line_tokens: List[Token]) -> str:
            if not line_tokens:
                return ""
            first = line_tokens[0]
            last = line_tokens[-1]
            return first.text if first is last else " ".join(token.text for token in line_tokens)

        @staticmethod
        def _tokens_have_open_delimiters(tokens: List[Token]) -> bool:
            paren_depth = 0
            bracket_depth = 0
            brace_depth = 0
            angle_depth = 0
            for token in tokens:
                token_type = token.token_type
                if token_type == TokenType.L_PAREN:
                    paren_depth += 1
                elif token_type == TokenType.R_PAREN and paren_depth > 0:
                    paren_depth -= 1
                elif token_type == TokenType.L_BRACKET:
                    bracket_depth += 1
                elif token_type == TokenType.R_BRACKET and bracket_depth > 0:
                    bracket_depth -= 1
                elif token_type == TokenType.L_BRACE:
                    brace_depth += 1
                elif token_type == TokenType.R_BRACE and brace_depth > 0:
                    brace_depth -= 1
                elif token_type == TokenType.LT:
                    angle_depth += 1
                elif token_type == TokenType.GT and angle_depth > 0:
                    angle_depth -= 1
            return any(depth > 0 for depth in (paren_depth, bracket_depth, brace_depth, angle_depth))

        @staticmethod
        def _identifier_sql(expr: exp.Expression) -> str:
            if isinstance(expr, exp.Identifier):
                return expr.this
            return expr.sql(dialect="spark")

        def _parse_function_body_expression_sql(self, sql_text: str) -> exp.Expression:
            normalized_sql_text = normalize_python_syntax(sql_text)
            normalized_sql_text = _rewrite_variant_colon_access(normalized_sql_text)
            parsed = sqlglot.parse_one(normalized_sql_text, read="spark")
            if _is_query_expression(parsed):
                return parsed
            return _parse_scalar_expression(sql_text)

        def _parse_function_characteristics(self) -> List[str]:
            characteristics: List[str] = []
            while self._curr is not None and not self._is_rich_function_body_start():
                if self._match_texts("LANGUAGE"):
                    if not self._match_texts("SQL"):
                        self.raise_error("Expected SQL after LANGUAGE in CREATE FUNCTION")
                    characteristics.append("LANGUAGE SQL")
                    continue
                if self._match_texts("NOT"):
                    if not self._match_texts("DETERMINISTIC"):
                        self.raise_error("Expected DETERMINISTIC after NOT in CREATE FUNCTION")
                    characteristics.append("NOT DETERMINISTIC")
                    continue
                if self._match_texts("DETERMINISTIC"):
                    characteristics.append("DETERMINISTIC")
                    continue
                if self._match_texts("COMMENT"):
                    comment = self._parse_string()
                    if comment is None:
                        self.raise_error("COMMENT in CREATE FUNCTION requires a string literal")
                    characteristics.append(f"COMMENT {comment.sql(dialect='spark')}")
                    continue
                if self._match_texts("CONTAINS"):
                    if not self._match_texts("SQL"):
                        self.raise_error("Expected SQL after CONTAINS in CREATE FUNCTION")
                    characteristics.append("CONTAINS SQL")
                    continue
                if self._match_texts("READS"):
                    if not self._match_texts("SQL"):
                        self.raise_error("Expected SQL after READS in CREATE FUNCTION")
                    if not self._match_texts("DATA"):
                        self.raise_error("Expected DATA after READS SQL in CREATE FUNCTION")
                    characteristics.append("READS SQL DATA")
                    continue
                self.raise_error(f"Unsupported CREATE FUNCTION clause '{getattr(self._curr, 'text', '')}'")
            return characteristics

        @staticmethod
        def _build_rich_function_create_expression(
            *,
            function_name: str,
            parameters: List[str],
            return_type_sql: Optional[str],
            characteristics: List[str],
            body_expression: exp.Expression,
            normalize_body: bool,
        ) -> exp.Create:
            normalized_body_expression = (
                normalize_sql_function_expression(body_expression)
                if normalize_body
                else body_expression
            )
            parts = [
                f"CREATE FUNCTION {function_name}({', '.join(parameters)})",
            ]
            if return_type_sql:
                parts.append(f"RETURNS {return_type_sql}")
            parts.extend(characteristics)
            parts.append("RETURN __agentcicd_body_placeholder")
            create_expression = sqlglot.parse_one(" ".join(parts), read="spark")
            if not isinstance(create_expression, exp.Create):
                raise ValueError("Expected CREATE FUNCTION expression")
            create_expression.set(
                "expression",
                exp.Return(this=normalized_body_expression.copy()),
            )
            return create_expression

        @staticmethod
        def _build_rich_function_body_with_ctes(
            *,
            parameter_names: List[str],
            assignments: List[tuple[str, exp.Expression]],
            return_expression: exp.Expression,
            normalize_steps: bool,
        ) -> exp.Expression:
            if not assignments:
                return return_expression

            ctes: List[exp.Expression] = []
            available_columns = list(parameter_names)
            variant_columns: set[str] = set()
            previous_cte_name: Optional[str] = None

            if parameter_names:
                previous_cte_name = "__agentcicd_fn_args"
                ctes.append(
                    exp.CTE(
                        this=exp.Select(
                            expressions=[exp.column(name) for name in parameter_names],
                        ),
                        alias=exp.TableAlias(this=exp.to_identifier(previous_cte_name)),
                    )
                )

            for index, (assignment_name, assignment_expression) in enumerate(assignments):
                normalized_assignment_expression = (
                    normalize_sql_function_step_expression(
                        assignment_expression,
                        variant_columns=variant_columns,
                    )
                    if normalize_steps
                    else assignment_expression
                )
                cte_name = f"__agentcicd_fn_step_{index}"
                select_expressions: List[exp.Expression] = [
                    exp.column(column_name) for column_name in available_columns
                ]
                select_expressions.append(
                    exp.alias_(
                        normalized_assignment_expression.copy(),
                        assignment_name,
                        copy=False,
                    )
                )
                select_body = exp.Select(expressions=select_expressions)
                if previous_cte_name:
                    select_body.set("from_", exp.From(this=exp.to_table(previous_cte_name)))
                ctes.append(
                    exp.CTE(
                        this=select_body,
                        alias=exp.TableAlias(this=exp.to_identifier(cte_name)),
                    )
                )
                previous_cte_name = cte_name
                available_columns.append(assignment_name)
                if normalize_steps and is_variant_expression(
                    normalized_assignment_expression,
                    variant_columns=variant_columns,
                ):
                    variant_columns.add(assignment_name.lower())

            normalized_return_expression = (
                normalize_sql_function_step_expression(
                    return_expression,
                    variant_columns=variant_columns,
                )
                if normalize_steps
                else return_expression
            )
            final_select = exp.Select(expressions=[normalized_return_expression.copy()])
            if previous_cte_name:
                final_select.set("from_", exp.From(this=exp.to_table(previous_cte_name)))
            final_select.set("with_", exp.With(expressions=ctes, recursive=False))
            return final_select

        def _parse_load_statement(self):
            table = self._parse_id_var()
            if table is None:
                self.raise_error("LOAD must specify table name")
            if not self._match_texts("FROM"):
                self.raise_error("LOAD statement must include FROM <path>")
            path_expr = self._parse_string()
            if path_expr is None:
                self.raise_error("LOAD path must be a string literal")
            options_expr = None
            if self._match_texts("WITH"):
                options_expr = self._parse_option_pairs()
            limit_expr = None
            if self._match_texts("LIMIT"):
                limit_expr = self._parse_number()
                if limit_expr is None:
                    self.raise_error("LOAD LIMIT must be a positive integer")
                try:
                    limit_value = int(limit_expr.this)
                except (TypeError, ValueError):
                    self.raise_error("LOAD LIMIT must be a positive integer")
                if limit_value <= 0:
                    self.raise_error("LOAD LIMIT must be a positive integer")
            return LoadExpression(
                table=table,
                path=path_expr,
                options=options_expr,
                limit=limit_expr,
            )

        def _parse_save_statement(self):
            table = self._parse_id_var()
            if table is None:
                self.raise_error("SAVE must specify table name")
            if not self._match_texts("TO"):
                self.raise_error("SAVE statement must include TO <path>")
            path_expr = self._parse_string()
            if path_expr is None:
                self.raise_error("SAVE path must be a string literal")
            options_expr = None
            if self._match_texts("WITH"):
                options_expr = self._parse_option_pairs()
            return SaveExpression(
                table=table,
                path=path_expr,
                options=options_expr,
            )

        def _parse_publish_statement(self):
            """Parse PUBLISH <table> TO REPORTS|DATASET|ANNOTATION ..."""
            table = self._parse_id_var()
            if table is None:
                self.raise_error("PUBLISH must specify table name")
            # Support both TO and AS for backward compatibility
            if not (self._match_texts("TO") or self._match_texts("AS")):
                self.raise_error("PUBLISH statement must include TO <destination>")
            destination = self._parse_id_var()
            if destination is None:
                self.raise_error("PUBLISH destination must be specified")
            dest_name = destination.this.upper() if isinstance(destination, exp.Identifier) else destination.sql(dialect="spark").upper()

            if dest_name == "REPORTS":
                if not self._match_texts("WITH"):
                    self.raise_error("PUBLISH TO REPORTS requires WITH (COMPONENT = METRIC|CHART|ISSUE)")
                wrapped_options = self._match(TokenType.L_PAREN)
                options_expr = self._parse_option_pairs()
                if wrapped_options and not self._match(TokenType.R_PAREN):
                    self.raise_error("Expected ')' after PUBLISH REPORTS options")
                options = {}
                if isinstance(options_expr, exp.Array):
                    for item in options_expr.expressions:
                        if isinstance(item, exp.Tuple):
                            key_expr = item.this
                            value_expr = item.expression
                            key = key_expr.sql(dialect="spark").strip("`").lower()
                            if isinstance(value_expr, exp.Literal):
                                options[key] = str(value_expr.this)
                            else:
                                options[key] = value_expr.sql(dialect="spark").strip("`")
                component = str(options.get("component", "")).upper()
                if component not in {"METRIC", "CHART", "ISSUE"}:
                    self.raise_error("PUBLISH TO REPORTS requires COMPONENT = METRIC|CHART|ISSUE")
                chart_type = options.get("chart_type")
                if component == "CHART":
                    missing = [key for key in ("chart_type", "x_axis", "y_axis") if not options.get(key)]
                    if missing:
                        self.raise_error(f"PUBLISH TO REPORTS chart components require {', '.join(missing).upper()}")
                elif chart_type is not None:
                    self.raise_error("CHART_TYPE is only valid for report chart components")
                return PublishExpression(
                    table=table,
                    destination=exp.Identifier(this=dest_name),
                    component=exp.Identifier(this=component),
                    chart_type=exp.Literal.string(chart_type) if chart_type else None,
                    report_options=exp.Array(
                        expressions=[
                            exp.Tuple(this=exp.Identifier(this=str(key)), expression=exp.Literal.string(str(value)))
                            for key, value in options.items()
                        ]
                    ),
                )
            elif dest_name == "DATASET":
                dataset_name_expr = self._parse_string() or self._parse_id_var()
                dataset_name = None
                if dataset_name_expr is not None:
                    if isinstance(dataset_name_expr, exp.Literal):
                        dataset_name = dataset_name_expr
                    else:
                        dataset_name = exp.Literal.string(dataset_name_expr.this)
                return PublishDatasetExpression(
                    table=table,
                    dataset_name=dataset_name,
                )
            elif dest_name == "ANNOTATION":
                if not self._match_texts("QUEUE"):
                    self.raise_error("PUBLISH TO ANNOTATION must use QUEUE <name>")
                queue_name_expr = self._parse_string() or self._parse_id_var()
                if queue_name_expr is None:
                    self.raise_error("PUBLISH TO ANNOTATION QUEUE must specify a queue name")
                if isinstance(queue_name_expr, exp.Literal):
                    queue_name = queue_name_expr
                else:
                    queue_name = exp.Literal.string(queue_name_expr.this)
                alias = None
                if self._match_texts("AS"):
                    alias_expr = self._parse_id_var()
                    if alias_expr is None:
                        self.raise_error("PUBLISH TO ANNOTATION QUEUE AS must specify an alias")
                    alias = alias_expr
                options_expr = None
                if self._match_texts("WITH"):
                    has_parens = self._match(TokenType.L_PAREN)
                    options_expr = self._parse_option_pairs()
                    if has_parens and not self._match(TokenType.R_PAREN):
                        self.raise_error("Expected ')' after PUBLISH annotation options")
                return PublishAnnotationExpression(
                    table=table,
                    queue_name=queue_name,
                    alias=alias,
                    options=options_expr,
                )
            else:
                self.raise_error(f"Unsupported PUBLISH destination '{dest_name}'. Use REPORTS, DATASET, or ANNOTATION.")

        def _parse_retrieve_annotation_statement(self):
            """Parse RETRIEVE ANNOTATION RESULTS <table> FROM ANNOTATION <id>"""
            # Expect ANNOTATION keyword
            if not self._match_texts("ANNOTATION"):
                self.raise_error("RETRIEVE must be followed by ANNOTATION")
            # Expect RESULTS keyword
            if not self._match_texts("RESULTS"):
                self.raise_error("RETRIEVE ANNOTATION must be followed by RESULTS")
            # Parse target table name
            table = self._parse_id_var()
            if table is None:
                self.raise_error("RETRIEVE ANNOTATION RESULTS must specify a table name")
            # Expect FROM keyword
            if not self._match_texts("FROM"):
                self.raise_error("RETRIEVE ANNOTATION RESULTS <table> must include FROM")
            annotation_request_id = None
            source_ref = None
            if self._match_texts("ANNOTATION"):
                if not self._match_texts("REQUEST"):
                    self.raise_error("Expected REQUEST after FROM ANNOTATION")
                request_expr = self._parse_string() or self._parse_id_var()
                if request_expr is None:
                    self.raise_error("RETRIEVE ANNOTATION RESULTS FROM ANNOTATION REQUEST must specify a request ID")
                annotation_request_id = (
                    request_expr if isinstance(request_expr, exp.Literal) else exp.Literal.string(request_expr.this)
                )
            else:
                source_expr = self._parse_id_var()
                if source_expr is None:
                    self.raise_error("RETRIEVE ANNOTATION RESULTS must specify a source reference")
                source_ref = source_expr
            return RetrieveAnnotationExpression(
                table=table,
                source_ref=source_ref,
                annotation_request_id=annotation_request_id,
            )

        def _parse_option_pairs(self) -> exp.Array:
            pairs: List[exp.Expression] = []
            while True:
                key = self._parse_id_var()
                if key is None:
                    self.raise_error("Expected option name")
                if not self._match(TokenType.EQ):
                    self.raise_error("Expected '=' after option name")
                value = self._parse_option_value()
                pairs.append(exp.Tuple(this=key, expression=value))
                if not self._match(TokenType.COMMA):
                    break
            return exp.Array(expressions=pairs)

        def _parse_option_value(self) -> exp.Expression:
            if self._match(TokenType.L_PAREN):
                values: List[exp.Expression] = []
                if self._match(TokenType.R_PAREN):
                    return exp.Array(expressions=values)
                while True:
                    value_expr = self._parse_string() or self._parse_number() or self._parse_id_var()
                    if value_expr is None:
                        self.raise_error("Expected value inside list")
                    values.append(value_expr)
                    if self._match(TokenType.COMMA):
                        continue
                    if not self._match(TokenType.R_PAREN):
                        self.raise_error("Expected ')' to close list")
                    break
                return exp.Array(expressions=values)
            literal = self._parse_string()
            if literal:
                return literal
            number = self._parse_number()
            if number:
                return number
            identifier = self._parse_id_var()
            if identifier:
                return identifier
            self.raise_error("Unsupported option value")


@dataclass
class _SqlFunctionDefinition:
    name: str
    parameters: List["_FunctionParameter"]
    expression: exp.Expression
    create_expression: exp.Create


@dataclass(frozen=True)
class _FunctionParameter:
    name: str
    type_sql: str = "ANY"
    has_default: bool = False


@dataclass
class _RegisteredFunctionDefinition:
    function_id: str
    name: str
    function_type: str
    call_name: str
    runtime_alias: str
    parameters: List[_FunctionParameter]
    operations: List[Dict[str, Any]]
    source_text: str = ""
    sql_definition: Optional[_SqlFunctionDefinition] = None
    allow_short_name_match: bool = True



from agentcicd.sql.parsing.script_parser import AgentCICDScriptParser
