from __future__ import annotations

from typing import List

import sqlglot
from sqlglot import expressions as exp
from agentcicd.sql.parsing.python_to_sql_lexer import normalize_python_syntax


def parse_sql(sql_text: str) -> exp.Expression:
    return sqlglot.parse_one(_rewrite_variant_paths(normalize_python_syntax(sql_text)), read="spark")


def rewrite_variant_paths(sql_text: str) -> str:
    return _rewrite_variant_paths(normalize_python_syntax(sql_text))


def _rewrite_variant_paths(sql_text: str) -> str:
    rewritten = sql_text
    for _ in range(8):
        next_sql = _rewrite_variant_paths_once(rewritten)
        if next_sql == rewritten:
            return next_sql
        rewritten = next_sql
    return rewritten


def _rewrite_variant_paths_once(sql_text: str) -> str:
    parts: list[str] = []
    index = 0
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    while index < len(sql_text):
        char = sql_text[index]
        next_char = sql_text[index + 1] if index + 1 < len(sql_text) else ""
        if in_line_comment:
            parts.append(char)
            index += 1
            if char == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            parts.append(char)
            index += 1
            if char == "*" and next_char == "/":
                parts.append(next_char)
                index += 1
                in_block_comment = False
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            parts.append(char)
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            parts.append(char)
            index += 1
            continue
        if in_single or in_double:
            parts.append(char)
            index += 1
            continue
        if char == "-" and next_char == "-":
            parts.append(char)
            parts.append(next_char)
            index += 2
            in_line_comment = True
            continue
        if char == "/" and next_char == "*":
            parts.append(char)
            parts.append(next_char)
            index += 2
            in_block_comment = True
            continue

        match = _match_identifier_variant_expr(sql_text, index)
        if match is not None:
            base, path, end_index = match
            args = ", ".join([base, *[_variant_arg(segment) for segment in path]])
            parts.append(f"__agentcicd_variant_path({args})")
            index = end_index
            continue

        function_match = _match_function_variant_expr(sql_text, index)
        if function_match is None:
            parts.append(char)
            index += 1
            continue

        base, path, end_index = function_match
        args = ", ".join([base, *[_variant_arg(segment) for segment in path]])
        parts.append(f"__agentcicd_variant_path({args})")
        index = end_index
    return "".join(parts)


def _match_identifier_variant_expr(sql_text: str, index: int) -> tuple[str, List[str | int], int] | None:
    base_end = _match_dotted_identifier(sql_text, index)
    if base_end is None:
        return None
    base = sql_text[index:base_end]
    suffix_match = _match_variant_suffix(sql_text, base_end)
    if suffix_match is None:
        return None
    path, end_index = suffix_match
    return base, path, end_index


def _match_function_variant_expr(sql_text: str, index: int) -> tuple[str, List[str | int], int] | None:
    base_end = _match_dotted_identifier(sql_text, index)
    if base_end is None:
        return None

    position = base_end
    while position < len(sql_text) and sql_text[position].isspace():
        position += 1
    if position >= len(sql_text) or sql_text[position] != "(":
        return None

    end_position = _match_closing_paren(sql_text, position)
    if end_position is None:
        return None

    base = sql_text[index:end_position]
    if ":" in base:
        return None

    suffix_match = _match_variant_suffix(sql_text, end_position)
    if suffix_match is None:
        return None

    path, end_index = suffix_match
    return base, path, end_index


def _match_variant_suffix(sql_text: str, index: int) -> tuple[List[str | int], int] | None:
    position = index
    path: list[str | int] = []
    parsed_any = False

    while True:
        while position < len(sql_text) and sql_text[position].isspace():
            position += 1
        if position >= len(sql_text) or sql_text[position] != ":":
            break
        position += 1
        while position < len(sql_text) and sql_text[position].isspace():
            position += 1

        if position < len(sql_text) and sql_text[position] == "[":
            index_value, next_position = _match_numeric_index(sql_text, position)
            if index_value is None or next_position is None:
                return None
            path.append(index_value)
            position = next_position
        else:
            identifier_end = _match_identifier(sql_text, position)
            if identifier_end is None:
                return None
            path.append(sql_text[position:identifier_end])
            position = identifier_end
        parsed_any = True

        while True:
            while position < len(sql_text) and sql_text[position].isspace():
                position += 1

            if position < len(sql_text) and sql_text[position] == "[":
                index_value, next_position = _match_numeric_index(sql_text, position)
                if index_value is None or next_position is None:
                    return None
                path.append(index_value)
                position = next_position
                continue

            if position < len(sql_text) and sql_text[position] == ".":
                position += 1
                while position < len(sql_text) and sql_text[position].isspace():
                    position += 1
                dotted_end = _match_identifier(sql_text, position)
                if dotted_end is None:
                    return None
                path.append(sql_text[position:dotted_end])
                position = dotted_end
                continue
            break

    if not parsed_any:
        return None
    return path, position


def _match_dotted_identifier(sql_text: str, index: int) -> int | None:
    position = index
    identifier_end = _match_identifier(sql_text, position)
    if identifier_end is None:
        return None
    position = identifier_end

    while position < len(sql_text) and sql_text[position] == ".":
        next_identifier_end = _match_identifier(sql_text, position + 1)
        if next_identifier_end is None:
            break
        position = next_identifier_end
    return position


def _match_identifier(sql_text: str, index: int) -> int | None:
    if index >= len(sql_text) or not _is_ident_start(sql_text[index]):
        return None
    position = index + 1
    while position < len(sql_text) and _is_ident_char(sql_text[position]):
        position += 1
    return position


def _match_numeric_index(sql_text: str, index: int) -> tuple[int | None, int | None]:
    if index >= len(sql_text) or sql_text[index] != "[":
        return None, None
    position = index + 1
    while position < len(sql_text) and sql_text[position].isspace():
        position += 1
    start = position
    while position < len(sql_text) and sql_text[position].isdigit():
        position += 1
    if start == position:
        return None, None
    while position < len(sql_text) and sql_text[position].isspace():
        position += 1
    if position >= len(sql_text) or sql_text[position] != "]":
        return None, None
    return int(sql_text[start:position].strip()), position + 1


def _match_closing_paren(sql_text: str, open_paren_index: int) -> int | None:
    depth = 0
    index = open_paren_index
    in_single = False
    in_double = False

    while index < len(sql_text):
        char = sql_text[index]
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index + 1
        index += 1
    return None


def _variant_arg(segment: str | int) -> str:
    if isinstance(segment, int):
        return str(segment)
    escaped = segment.replace("'", "''")
    return f"'{escaped}'"


def _is_ident_start(char: str) -> bool:
    return char == "_" or char.isalpha()


def _is_ident_char(char: str) -> bool:
    return char == "_" or char.isalnum()
