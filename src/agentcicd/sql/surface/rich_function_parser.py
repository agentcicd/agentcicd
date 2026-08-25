from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from agentcicd.sql.ir.expressions import ReturnExpr
from agentcicd.sql.ir.functions import FunctionDefinitionIR, FunctionParameterIR, SqlFunctionBodyIR
from agentcicd.sql.ir.statements import SqlFunctionDefStmt
from agentcicd.sql.surface.spark_sql_parser import parse_sql
from agentcicd.sql.surface.sqlglot_bridge import expression_to_ir

_HEADER_RE = re.compile(
    r"^\s*CREATE\s+FUNCTION\s+([A-Za-z_][\w\.]*)\s*\((.*?)\)(?:\s+RETURNS\s+(.+?))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_RETURNS_RE = re.compile(r"^\s*RETURNS\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*:=\s*(.+?)\s*$", re.IGNORECASE | re.DOTALL)
_RETURN_RE = re.compile(r"^\s*RETURN\s+(.+?)\s*;?\s*$", re.IGNORECASE | re.DOTALL)


@dataclass
class ParsedRichSqlFunction:
    definition: Optional[FunctionDefinitionIR]
    statement: Optional[SqlFunctionDefStmt]


def parse_rich_sql_function(source_text: str) -> ParsedRichSqlFunction:
    parse_text = _strip_sql_comments(source_text)
    lines = [line.rstrip() for line in parse_text.strip().splitlines() if line.strip()]
    if not lines:
        return ParsedRichSqlFunction(definition=None, statement=None)

    header_text, body_start = _extract_header(lines)
    header_match = _HEADER_RE.match(header_text)
    if header_match is None:
        raise ValueError("Expected CREATE FUNCTION header")

    name = header_match.group(1)
    params_text = header_match.group(2).strip()
    inline_return_type = header_match.group(3)
    return_type_sql: Optional[str] = inline_return_type.strip() if inline_return_type else None
    parameters = _parse_parameters(params_text)

    return_expr: Optional[ReturnExpr] = None
    if return_type_sql is None and len(lines) > 1:
        returns_match = _RETURNS_RE.match(lines[body_start]) if body_start < len(lines) else None
        if returns_match is not None:
            return_type_sql = returns_match.group(1).strip() or None
            body_start += 1

    for line in _split_body_statements(lines[body_start:]):
        assign_match = _ASSIGN_RE.match(line)
        if assign_match is not None:
            raise ValueError("CREATE FUNCTION assignment statements are no longer supported; use RETURN with a SQL expression")
        return_match = _RETURN_RE.match(line)
        if return_match is not None:
            parsed_expr = parse_sql(return_match.group(1))
            return_expr = ReturnExpr(value=expression_to_ir(parsed_expr))
            continue
        raise ValueError(f"Unsupported function body line: {line}")

    definition = FunctionDefinitionIR(
        canonical_name=name,
        kind="sql",
        surface_names=[name],
        runtime_alias=name.replace(".", "_"),
        parameters=parameters,
        return_type_sql=return_type_sql,
        sql_body=SqlFunctionBodyIR(assignments=[], return_expr=return_expr),
        source_text=source_text,
    )
    return ParsedRichSqlFunction(definition=definition, statement=SqlFunctionDefStmt(definition=definition, source_text=source_text))


def _strip_sql_comments(source_text: str) -> str:
    result: list[str] = []
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    i = 0

    while i < len(source_text):
        char = source_text[i]
        next_char = source_text[i + 1] if i + 1 < len(source_text) else ""

        if in_line_comment:
            if char == "\n":
                result.append(char)
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if char == "\n":
                result.append(char)
            if char == "*" and next_char == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_single:
            result.append(char)
            if char == "'" and next_char == "'":
                result.append(next_char)
                i += 2
                continue
            if char == "'":
                in_single = False
            i += 1
            continue

        if in_double:
            result.append(char)
            if char == '"' and next_char == '"':
                result.append(next_char)
                i += 2
                continue
            if char == '"':
                in_double = False
            i += 1
            continue

        if char == "-" and next_char == "-":
            in_line_comment = True
            i += 2
            continue

        if char == "/" and next_char == "*":
            in_block_comment = True
            i += 2
            continue

        result.append(char)
        if char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        i += 1

    return "".join(result)


def _parse_parameters(params_text: str) -> List[FunctionParameterIR]:
    if not params_text.strip():
        return []
    parameters: list[FunctionParameterIR] = []
    for raw_part in [part.strip() for part in params_text.split(",") if part.strip()]:
        pieces = raw_part.split(None, 1)
        if len(pieces) != 2:
            raise ValueError(f"Invalid function parameter: {raw_part}")
        parameters.append(FunctionParameterIR(name=pieces[0], type_sql=pieces[1]))
    return parameters


def _split_body_statements(lines: List[str]) -> List[str]:
    statements: list[str] = []
    current: list[str] = []
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        current.append(line)
        paren_depth += line.count("(") - line.count(")")
        bracket_depth += line.count("[") - line.count("]")
        brace_depth += line.count("{") - line.count("}")
        statement_text = " ".join(current).strip()
        if paren_depth <= 0 and bracket_depth <= 0 and brace_depth <= 0 and (
            line.endswith(";")
            or _RETURN_RE.match(statement_text) is not None
            or _ASSIGN_RE.match(statement_text) is not None
        ):
            statements.append(statement_text)
            current = []
            paren_depth = 0
            bracket_depth = 0
            brace_depth = 0

    if current:
        statements.append(" ".join(current).strip())

    return statements


def _extract_header(lines: List[str]) -> tuple[str, int]:
    header_parts: list[str] = []
    paren_depth = 0
    saw_open = False

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        header_parts.append(line)
        paren_depth += line.count("(") - line.count(")")
        if "(" in line:
            saw_open = True
        header_text = " ".join(header_parts).strip()
        if saw_open and paren_depth <= 0 and _HEADER_RE.match(header_text):
            return header_text, index + 1

    return lines[0].strip(), 1
