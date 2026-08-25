from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TypeSpec:
    name: str
    parameters: tuple["TypeSpec", ...] = ()
    fields: tuple[tuple[str, "TypeSpec"], ...] = ()
    function_parameters: tuple[tuple[str, "TypeSpec"], ...] = ()
    function_return: "TypeSpec | None" = None

    @property
    def is_function(self) -> bool:
        return self.name.upper() == "FUNCTION"

    def normalized(self) -> str:
        name = _normalize_type_name(self.name)
        if self.is_function:
            args = ", ".join(
                f"{param_name.lower()} {param_type.normalized()}"
                for param_name, param_type in self.function_parameters
            )
            return_type = self.function_return.normalized() if self.function_return is not None else "ANY"
            return f"FUNCTION<({args}) RETURNS {return_type}>"
        if self.fields:
            fields = ", ".join(
                f"{field_name.lower()}: {field_type.normalized()}"
                for field_name, field_type in self.fields
            )
            return f"{name}<{fields}>"
        if self.parameters:
            return f"{name}<" + ", ".join(item.normalized() for item in self.parameters) + ">"
        return name


class _TokenStream:
    def __init__(self, source: str) -> None:
        self.tokens = _tokenize_type(source)
        self.index = 0

    def peek(self) -> str | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def pop(self) -> str:
        token = self.peek()
        if token is None:
            raise ValueError("Unexpected end of type expression")
        self.index += 1
        return token

    def match(self, text: str) -> bool:
        token = self.peek()
        if token is not None and token.upper() == text.upper():
            self.index += 1
            return True
        return False

    def expect(self, text: str) -> None:
        if not self.match(text):
            actual = self.peek()
            raise ValueError(f"Expected '{text}' in type expression, found '{actual or 'end of input'}'")

    def consumed(self) -> bool:
        return self.index >= len(self.tokens)


def parse_type_spec(type_sql: str) -> TypeSpec:
    stream = _TokenStream(type_sql)
    parsed = _parse_type(stream)
    if not stream.consumed():
        raise ValueError(f"Unexpected token '{stream.peek()}' in type expression")
    return parsed


def is_function_type(type_sql: str) -> bool:
    try:
        return parse_type_spec(type_sql).is_function
    except ValueError:
        return False


def function_type_matches(actual_type_sql: str | None, expected_type_sql: str) -> bool:
    if actual_type_sql is None:
        return False
    actual = parse_type_spec(actual_type_sql)
    expected = parse_type_spec(expected_type_sql)
    if not expected.is_function:
        return True
    if not actual.is_function:
        return False
    return actual.normalized() == expected.normalized()


def _parse_type(stream: _TokenStream) -> TypeSpec:
    name = stream.pop()
    if not _is_identifier(name):
        raise ValueError(f"Expected type name, found '{name}'")
    if name.upper() == "FUNCTION":
        return _parse_function_type(stream)
    if not stream.match("<"):
        return TypeSpec(name=name)

    if name.upper() == "STRUCT":
        fields: list[tuple[str, TypeSpec]] = []
        if not stream.match(">"):
            while True:
                field_name = stream.pop()
                if not _is_identifier(field_name):
                    raise ValueError(f"Expected struct field name, found '{field_name}'")
                stream.expect(":")
                fields.append((field_name, _parse_type(stream)))
                if stream.match(">"):
                    break
                stream.expect(",")
        return TypeSpec(name=name, fields=tuple(fields))

    parameters: list[TypeSpec] = []
    if not stream.match(">"):
        while True:
            parameters.append(_parse_type(stream))
            if stream.match(">"):
                break
            stream.expect(",")
    return TypeSpec(name=name, parameters=tuple(parameters))


def _parse_function_type(stream: _TokenStream) -> TypeSpec:
    stream.expect("<")
    stream.expect("(")
    parameters: list[tuple[str, TypeSpec]] = []
    if not stream.match(")"):
        while True:
            param_name = stream.pop()
            if not _is_identifier(param_name):
                raise ValueError(f"Expected function parameter name, found '{param_name}'")
            parameters.append((param_name, _parse_type(stream)))
            if stream.match(")"):
                break
            stream.expect(",")
    if not stream.match("RETURNS") and not stream.match("RETURN"):
        actual = stream.peek()
        raise ValueError(f"Expected RETURNS in function type, found '{actual or 'end of input'}'")
    return_type = _parse_type(stream)
    stream.expect(">")
    return TypeSpec(name="FUNCTION", function_parameters=tuple(parameters), function_return=return_type)


def _tokenize_type(source: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if char in "<>(),:":
            tokens.append(char)
            index += 1
            continue
        start = index
        while index < len(source) and not source[index].isspace() and source[index] not in "<>(),:":
            index += 1
        tokens.append(source[start:index])
    return tokens


def _is_identifier(value: str) -> bool:
    if not value:
        return False
    first = value[0]
    if not (first.isalpha() or first == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in value[1:])


def _normalize_type_name(name: str) -> str:
    lowered = name.strip().lower()
    aliases = {
        "int": "INTEGER",
        "integer": "INTEGER",
        "str": "STRING",
        "string": "STRING",
        "bool": "BOOLEAN",
        "boolean": "BOOLEAN",
        "float": "FLOAT",
        "double": "DOUBLE",
        "variant": "VARIANT",
        "json": "VARIANT",
        "any": "ANY",
        "array": "ARRAY",
        "map": "MAP",
        "struct": "STRUCT",
    }
    return aliases.get(lowered, name.upper())
