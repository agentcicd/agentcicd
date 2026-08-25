from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any, Optional

from agentcicd.sql.ir.options import StatementOptions
from agentcicd.sql.ir.statements import (
    DeclareInputStmt,
    LoadStmt,
    PublishAnnotationStmt,
    PublishDatasetStmt,
    PublishReportsStmt,
    RetrieveAnnotationStmt,
    SaveStmt,
    StatementIR,
)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str


class _Cursor:
    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._index = 0

    def peek(self, offset: int = 0) -> _Token | None:
        position = self._index + offset
        if 0 <= position < len(self._tokens):
            return self._tokens[position]
        return None

    def match_keyword(self, keyword: str) -> bool:
        token = self.peek()
        if token is None or token.kind != "word" or token.text.upper() != keyword.upper():
            return False
        self._index += 1
        return True

    def match_symbol(self, symbol: str) -> bool:
        token = self.peek()
        if token is None or token.kind != "symbol" or token.text != symbol:
            return False
        self._index += 1
        return True

    def expect_keyword(self, keyword: str) -> None:
        if not self.match_keyword(keyword):
            raise ValueError(f"Expected keyword '{keyword}'")

    def expect_symbol(self, symbol: str) -> None:
        if not self.match_symbol(symbol):
            raise ValueError(f"Expected symbol '{symbol}'")

    def expect_identifier(self) -> str:
        token = self.peek()
        if token is None or token.kind != "word":
            raise ValueError("Expected identifier")
        self._index += 1
        return token.text

    def expect_string_or_identifier(self) -> str:
        token = self.peek()
        if token is None:
            raise ValueError("Expected string or identifier")
        if token.kind == "string":
            self._index += 1
            return token.text
        if token.kind == "word":
            self._index += 1
            return token.text
        raise ValueError("Expected string or identifier")

    def expect_positive_int(self) -> int:
        token = self.peek()
        if token is None or token.kind != "word":
            raise ValueError("Expected positive integer")
        try:
            value = int(token.text)
        except ValueError as exc:
            raise ValueError("Expected positive integer") from exc
        if value <= 0:
            raise ValueError("Expected positive integer")
        self._index += 1
        return value

    def remaining_text(self) -> str:
        return " ".join(token.text if token.kind != "string" else _quote_string(token.text) for token in self._tokens[self._index :])

    def consume_remaining(self) -> None:
        self._index = len(self._tokens)

    def at_end(self) -> bool:
        return self.peek() is None


def parse_custom_statement(source_text: str) -> Optional[StatementIR]:
    tokens = _tokenize(source_text.strip().rstrip(";"))
    if not tokens:
        return None
    cursor = _Cursor(tokens)
    first = cursor.peek()
    if first is None or first.kind != "word":
        return None
    keyword = first.text.upper()
    if keyword == "LOAD":
        return _parse_load(cursor, source_text)
    if keyword == "DECLARE" and len(tokens) > 1 and tokens[1].kind == "word" and tokens[1].text.upper() == "INPUT":
        return _parse_declare_input(cursor, source_text)
    if keyword == "SAVE":
        return _parse_save(cursor, source_text)
    if keyword == "PUBLISH":
        return _parse_publish(cursor, source_text)
    if keyword == "RETRIEVE":
        return _parse_retrieve_annotation(cursor, source_text)
    return None


def _parse_declare_input(cursor: _Cursor, source_text: str) -> DeclareInputStmt:
    cursor.expect_keyword("DECLARE")
    cursor.expect_keyword("INPUT")
    name = cursor.expect_identifier()
    input_type = cursor.expect_identifier().upper()
    options = StatementOptions()
    if cursor.match_keyword("WITH"):
        options = StatementOptions.from_mapping(_parse_declare_input_options(cursor))
    default_sql = None
    if cursor.match_keyword("DEFAULT"):
        default_sql = _consume_default_sql(cursor)
        if not default_sql:
            raise ValueError("DECLARE INPUT DEFAULT requires an expression")
    environment = None
    if cursor.match_keyword("ON"):
        cursor.expect_keyword("ENVIRONMENT")
        environment = cursor.expect_string_or_identifier().strip()
        if not environment:
            raise ValueError("DECLARE INPUT ON ENVIRONMENT requires a name")
    if not cursor.at_end() and default_sql is None:
        raise ValueError(f"Unexpected DECLARE INPUT tokens: {cursor.remaining_text()}")
    if not cursor.at_end():
        raise ValueError(f"Unexpected DECLARE INPUT tokens: {cursor.remaining_text()}")
    return DeclareInputStmt(
        name=name,
        input_type=input_type,
        options=options,
        default_sql=default_sql,
        environment=environment,
        source_text=source_text,
    )


def _consume_default_sql(cursor: _Cursor) -> str:
    default_tokens: list[_Token] = []
    while not cursor.at_end():
        token = cursor.peek()
        next_token = cursor.peek(1)
        if (
            token is not None
            and next_token is not None
            and token.kind == "word"
            and next_token.kind == "word"
            and token.text.upper() == "ON"
            and next_token.text.upper() == "ENVIRONMENT"
        ):
            break
        if token is not None:
            default_tokens.append(token)
        cursor._index += 1
    return " ".join(token.text if token.kind != "string" else _quote_string(token.text) for token in default_tokens).strip()


def _parse_declare_input_options(cursor: _Cursor) -> dict[str, Any]:
    options: dict[str, Any] = {}
    while not cursor.at_end():
        if cursor.peek() is not None and cursor.peek().kind == "word" and cursor.peek().text.upper() == "DEFAULT":
            break
        key = cursor.expect_identifier().lower()
        cursor.expect_symbol("=")
        value_token = cursor.peek()
        raw_value = cursor.expect_string_or_identifier()
        if value_token is not None and value_token.kind == "word":
            options[key] = _parse_option_value(raw_value)
        else:
            options[key] = raw_value
        if not cursor.match_symbol(","):
            continue
    return options


def _parse_load(cursor: _Cursor, source_text: str) -> LoadStmt:
    cursor.expect_keyword("LOAD")
    table = cursor.expect_identifier()
    cursor.expect_keyword("FROM")
    path = cursor.expect_string_or_identifier()
    options = StatementOptions()
    limit = None
    if cursor.match_keyword("WITH"):
        options_text = cursor.remaining_text()
        options_text, limit = _split_load_limit(options_text)
        options = StatementOptions.from_mapping(_parse_options(options_text))
        cursor.consume_remaining()
    elif cursor.match_keyword("LIMIT"):
        limit = cursor.expect_positive_int()
    elif not cursor.at_end():
        options_text = cursor.remaining_text()
        options_text, limit = _split_load_limit(options_text)
        options = StatementOptions.from_mapping(_parse_options(options_text))
        cursor.consume_remaining()
    if not cursor.at_end():
        raise ValueError(f"Unexpected LOAD tokens: {cursor.remaining_text()}")
    return LoadStmt(table=table, path=path, options=options, limit=limit, source_text=source_text)


def _parse_save(cursor: _Cursor, source_text: str) -> SaveStmt:
    cursor.expect_keyword("SAVE")
    table = cursor.expect_identifier()
    cursor.expect_keyword("TO")
    path = cursor.expect_string_or_identifier()
    options = _parse_with_options(cursor)
    return SaveStmt(table=table, path=path, options=options, source_text=source_text)


def _parse_publish(cursor: _Cursor, source_text: str) -> StatementIR:
    cursor.expect_keyword("PUBLISH")
    table = cursor.expect_identifier()
    cursor.expect_keyword("TO")
    if cursor.match_keyword("REPORTS"):
        options = _parse_with_options(cursor).to_dict()
        normalized_options = {
            str(key).strip().lower(): str(value).strip()
            for key, value in options.items()
            if not isinstance(value, list)
        }
        component = str(normalized_options.get("component") or "").strip().lower()
        if component not in {"metric", "chart", "issue"}:
            raise ValueError("PUBLISH TO REPORTS requires WITH (COMPONENT = METRIC|CHART|ISSUE)")
        chart_type = normalized_options.get("chart_type")
        if isinstance(chart_type, list):
            raise ValueError("CHART_TYPE must be a scalar option")
        if component != "chart" and chart_type is not None:
            raise ValueError("CHART_TYPE is only valid for report chart components")
        if component == "chart":
            missing = [key for key in ("chart_type", "x_axis", "y_axis") if not normalized_options.get(key)]
            if missing:
                raise ValueError(f"PUBLISH TO REPORTS chart components require {', '.join(missing).upper()}")
        return PublishReportsStmt(
            table=table,
            component=component,
            chart_type=str(chart_type).strip().lower() if chart_type else None,
            report_options=normalized_options,
            source_text=source_text,
        )
    if cursor.match_keyword("DATASET"):
        dataset_name = cursor.expect_string_or_identifier() if cursor.peek() is not None else None
        return PublishDatasetStmt(table=table, dataset_name=dataset_name, source_text=source_text)
    if cursor.match_keyword("ANNOTATION"):
        cursor.expect_keyword("QUEUE")
        queue_name = cursor.expect_string_or_identifier()
        alias = None
        if cursor.match_keyword("AS"):
            alias = cursor.expect_identifier()
        options = _parse_with_options(cursor)
        return PublishAnnotationStmt(
            table=table,
            queue_name=queue_name,
            alias=alias,
            options=options,
            source_text=source_text,
        )
    raise ValueError("PUBLISH must target REPORTS, DATASET, or ANNOTATION QUEUE")


def _parse_retrieve_annotation(cursor: _Cursor, source_text: str) -> RetrieveAnnotationStmt:
    cursor.expect_keyword("RETRIEVE")
    cursor.expect_keyword("ANNOTATION")
    if cursor.match_keyword("RESULTS"):
        table = cursor.expect_identifier()
        cursor.expect_keyword("FROM")
        if cursor.match_keyword("ANNOTATION"):
            cursor.expect_keyword("REQUEST")
            request_id = cursor.expect_string_or_identifier()
            return RetrieveAnnotationStmt(
                table=table,
                source_ref=request_id,
                annotation_request_id=request_id,
                source_text=source_text,
            )
        source_ref = cursor.expect_string_or_identifier()
        return RetrieveAnnotationStmt(table=table, source_ref=source_ref, source_text=source_text)
    raise ValueError("RETRIEVE ANNOTATION must use RESULTS <table> FROM <source>")


def _parse_with_options(cursor: _Cursor) -> StatementOptions:
    if not cursor.match_keyword("WITH"):
        return StatementOptions()
    return StatementOptions.from_mapping(_parse_options(cursor.remaining_text()))


def _split_load_limit(options_text: str) -> tuple[str, int | None]:
    match = re.search(r"(?is)\s+LIMIT\s+([0-9]+)\s*$", options_text.strip())
    if not match:
        return options_text, None
    limit = int(match.group(1))
    if limit <= 0:
        raise ValueError("LOAD LIMIT must be a positive integer")
    return options_text[: match.start()].strip(), limit


def _parse_options(raw_options: Optional[str]) -> dict[str, Any]:
    if not raw_options:
        return {}
    raw_options = raw_options.strip().rstrip(";").strip()
    if raw_options.startswith("(") and raw_options.endswith(")"):
        raw_options = raw_options[1:-1].strip()
    options: dict[str, Any] = {}
    for part in _split_top_level_commas(raw_options):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"Invalid option fragment: {part}")
        key, raw_value = part.split("=", 1)
        key = key.strip().lower()
        value = raw_value.strip()
        if value.startswith("(") and value.endswith(")"):
            items = [item.strip().strip("'").strip('"') for item in _split_top_level_commas(value[1:-1]) if item.strip()]
            options[key] = items
            continue
        options[key] = _parse_option_value(value)
    return options


def _parse_option_value(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value[0] in "{[":
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"Invalid structured option value: {value}") from exc
    if value[0] in {"'", '"'} and value[-1:] == value[0]:
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def _split_top_level_commas(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_single = False
    in_double = False

    for char in value:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char in "({[":
                depth += 1
            elif char in ")}]" and depth > 0:
                depth -= 1
            elif char == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
                continue
        current.append(char)

    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _tokenize(statement: str) -> list[_Token]:
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
            start = i
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
        if char in "(),=;{}[]:":
            tokens.append(_Token("symbol", char))
            i += 1
            continue
        start = i
        while i < len(statement) and not statement[i].isspace() and statement[i] not in "(),=;{}[]:'\"":
            if statement[i] == "-" and i + 1 < len(statement) and statement[i + 1] == "-":
                break
            i += 1
        tokens.append(_Token("word", statement[start:i]))
    return tokens


def _quote_string(value: str) -> str:
    return "'" + value.replace("'", "\\'") + "'"
