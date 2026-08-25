from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from agentcicd.sql.ir.options import StatementOptions
from agentcicd.sql.ir.statements import BatchTableStmt, QueryStmt, SqlFunctionDefStmt, StatementIR, StreamTableStmt
from agentcicd.sql.surface.custom_statement_parser import _parse_options, parse_custom_statement
from agentcicd.sql.surface.rich_function_parser import _strip_sql_comments, parse_rich_sql_function
from agentcicd.sql.surface.spark_sql_parser import parse_sql
from agentcicd.sql.surface.sqlglot_bridge import expression_to_ir


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str


_SYMBOL_TOKENS = frozenset({"(", ")", "{", "}", "[", "]", ":", ",", "=", ";", ">", "<", "!", "+", "-", "*", "/", "%"})
_NO_SPACE_BEFORE = frozenset({")", "}", "]", ",", ":", ";"})
_NO_SPACE_AFTER = frozenset({"(", "{", "[", ",", ":"})
_MULTI_CHAR_OPERATORS = frozenset({">=", "<=", "!=", "<>", "==", "=>", ":="})


class TopLevelParser:
    def __init__(self, script: str):
        self._script = script

    def parse(self) -> List[StatementIR]:
        statements: list[StatementIR] = []
        for chunk in _split_top_level_statements(self._script):
            if not _strip_sql_comments(chunk).strip():
                continue
            custom = parse_custom_statement(chunk)
            if custom is not None:
                statements.append(custom)
                continue
            tokens = _tokenize_statement(chunk)
            if _matches_keywords(tokens, "CREATE", "FUNCTION"):
                parsed = parse_rich_sql_function(chunk)
                if parsed.statement is None:
                    raise ValueError("Unable to parse CREATE FUNCTION statement")
                statements.append(parsed.statement)
                continue
            parsed_table = _parse_create_table_statement(tokens, chunk)
            if parsed_table is not None:
                mode, table_name, batch_size, options, query_text = parsed_table
                query_expression = parse_sql(query_text)
                if mode == "BATCH":
                    statements.append(
                        BatchTableStmt(
                            name=table_name,
                            query=expression_to_ir(query_expression),
                            query_source_text=query_text,
                            batch_size=batch_size,
                            options=options,
                            source_text=chunk,
                        )
                    )
                else:
                    statements.append(
                        StreamTableStmt(
                            name=table_name,
                            query=expression_to_ir(query_expression),
                            query_source_text=query_text,
                            batch_size=batch_size,
                            options=options,
                            source_text=chunk,
                        )
                    )
                continue
            statements.append(
                QueryStmt(
                    query=expression_to_ir(parse_sql(chunk)),
                    source_text=chunk,
                )
            )
        return statements


def _parse_create_table_statement(
    tokens: list[_Token],
    chunk: str,
) -> tuple[str, str, int | None, StatementOptions, str] | None:
    if not _matches_keywords(tokens, "CREATE", "BATCH", "TABLE") and not _matches_keywords(tokens, "CREATE", "STREAM", "TABLE"):
        return None
    mode = tokens[1].text.upper()
    if len(tokens) < 4 or tokens[3].kind != "word":
        raise ValueError("CREATE TABLE is missing a table name")
    table_name = tokens[3].text
    index = 4
    batch_size: int | None = None
    options = StatementOptions()

    if _matches_keywords(tokens[index:], "OPTIONS"):
        options_token = tokens[index]
        if len(tokens) <= index + 1 or tokens[index + 1].text != "(":
            raise ValueError("CREATE TABLE OPTIONS must be wrapped in parentheses")
        options_text, next_index = _extract_parenthesized_tokens(tokens, index + 1)
        options = StatementOptions.from_mapping(_parse_options(options_text))
        batch_value = options.get("batch_size")
        if batch_value is not None:
            try:
                batch_size = int(str(batch_value))
            except ValueError as exc:
                raise ValueError(f"Invalid BATCH_SIZE value '{batch_value}'") from exc
        index = next_index

    query_text = _reconstruct_tokens(tokens[index:]).strip()
    if not query_text:
        raise ValueError("CREATE TABLE statement is missing a query body")
    return mode, table_name, batch_size, options, query_text


def _extract_parenthesized_tokens(tokens: list[_Token], open_index: int) -> tuple[str, int]:
    depth = 0
    current: list[_Token] = []
    for index in range(open_index, len(tokens)):
        token = tokens[index]
        if token.text == "(":
            depth += 1
            if depth > 1:
                current.append(token)
            continue
        if token.text == ")":
            depth -= 1
            if depth == 0:
                return _reconstruct_tokens(current), index + 1
            current.append(token)
            continue
        current.append(token)
    raise ValueError("Unterminated CREATE TABLE OPTIONS clause")


def _matches_keywords(tokens: Iterable[_Token], *keywords: str) -> bool:
    token_list = list(tokens)
    if len(token_list) < len(keywords):
        return False
    for token, keyword in zip(token_list, keywords):
        if token.kind != "word" or token.text.upper() != keyword.upper():
            return False
    return True


def _split_top_level_statements(script: str) -> List[str]:
    statements: list[str] = []
    current: list[str] = []
    paren_depth = 0
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    i = 0

    while i < len(script):
        char = script[i]
        next_char = script[i + 1] if i + 1 < len(script) else ""

        if in_line_comment:
            current.append(char)
            if char == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            current.append(char)
            if char == "*" and next_char == "/":
                current.append(next_char)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if not in_single and not in_double:
            if char == "-" and next_char == "-":
                current.append(char)
                current.append(next_char)
                in_line_comment = True
                i += 2
                continue
            if char == "/" and next_char == "*":
                current.append(char)
                current.append(next_char)
                in_block_comment = True
                i += 2
                continue

        current.append(char)
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char == "(":
                paren_depth += 1
            elif char == ")" and paren_depth > 0:
                paren_depth -= 1
            elif char == ";" and paren_depth == 0:
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
        i += 1

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _tokenize_statement(statement: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    while i < len(statement):
        char = statement[i]
        next_char = statement[i + 1] if i + 1 < len(statement) else ""
        if char.isspace():
            i += 1
            continue
        if char == "-" and next_char == "-":
            while i < len(statement) and statement[i] != "\n":
                i += 1
            continue
        if char == "/" and next_char == "*":
            i += 2
            while i + 1 < len(statement) and not (statement[i] == "*" and statement[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if char in {"'", '"'}:
            quote = char
            i += 1
            value_chars: list[str] = []
            while i < len(statement):
                current = statement[i]
                if current == quote:
                    break
                value_chars.append(current)
                i += 1
            if i >= len(statement) or statement[i] != quote:
                raise ValueError("Unterminated quoted string")
            tokens.append(_Token("string", "".join(value_chars)))
            i += 1
            continue
        if char in _SYMBOL_TOKENS:
            tokens.append(_Token("symbol", char))
            i += 1
            continue
        start = i
        while i < len(statement) and not statement[i].isspace() and statement[i] not in "".join(_SYMBOL_TOKENS) + "'\"":
            if statement[i] == "-" and i + 1 < len(statement) and statement[i + 1] == "-":
                break
            i += 1
        tokens.append(_Token("word", statement[start:i]))
    return tokens


def _reconstruct_tokens(tokens: Iterable[_Token]) -> str:
    parts: list[str] = []
    previous: _Token | None = None
    for token in tokens:
        text = "'" + token.text.replace("'", "\\'") + "'" if token.kind == "string" else token.text
        needs_space = True
        if previous is None:
            needs_space = False
        elif text in _NO_SPACE_BEFORE or previous.text in _NO_SPACE_AFTER:
            needs_space = False
        elif previous.kind == "symbol" and token.kind == "symbol" and previous.text + token.text in _MULTI_CHAR_OPERATORS:
            needs_space = False
        elif previous.kind == "symbol" and previous.text in _SYMBOL_TOKENS - {"(", ")", ",", ";"}:
            needs_space = True
        elif token.kind == "symbol" and token.text in _SYMBOL_TOKENS - {"(", ")", ",", ";"}:
            needs_space = True
        if needs_space:
            parts.append(" ")
        parts.append(text)
        previous = token
    return "".join(parts)
