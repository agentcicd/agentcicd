"""
Lexer-based Python-to-SQL converter.

More robust than regex-based parsing. Properly handles:
- String literals with special characters
- Comments (-- and /* */)
- Nested structures
- Escape sequences
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional, Tuple


class TokenType(Enum):
    """Token types for lexical analysis."""
    LBRACKET = auto()      # [
    RBRACKET = auto()      # ]
    LBRACE = auto()        # {
    RBRACE = auto()        # }
    LPAREN = auto()        # (
    RPAREN = auto()        # )
    COMMA = auto()         # ,
    COLON = auto()         # :
    STRING = auto()        # 'string' or "string"
    IDENTIFIER = auto()    # column_name, function_name, etc.
    NUMBER = auto()        # 123, 45.67
    WHITESPACE = auto()    # spaces, tabs, newlines
    COMMENT = auto()       # -- comment or /* comment */
    OTHER = auto()         # any other character


@dataclass
class Token:
    """A lexical token."""
    type: TokenType
    value: str
    position: int


class SQLLexer:
    """
    Lexical scanner for SQL with Python-like syntax.

    Properly handles string literals, comments, and nested structures.
    """

    def __init__(self, sql: str):
        self.sql = sql
        self.pos = 0
        self.tokens: List[Token] = []

    def tokenize(self) -> List[Token]:
        """Tokenize the SQL string."""
        while self.pos < len(self.sql):
            # Try to match each token type
            if self._try_whitespace():
                continue
            if self._try_comment():
                continue
            if self._try_string():
                continue
            if self._try_number():
                continue
            if self._try_single_char():
                continue
            if self._try_identifier():
                continue

            # If nothing matched, consume one character as OTHER
            self.tokens.append(Token(TokenType.OTHER, self.sql[self.pos], self.pos))
            self.pos += 1

        return self.tokens

    def _try_whitespace(self) -> bool:
        """Try to match whitespace."""
        if self.pos >= len(self.sql):
            return False

        if self.sql[self.pos] in ' \t\n\r':
            start = self.pos
            while self.pos < len(self.sql) and self.sql[self.pos] in ' \t\n\r':
                self.pos += 1
            self.tokens.append(Token(TokenType.WHITESPACE, self.sql[start:self.pos], start))
            return True
        return False

    def _try_comment(self) -> bool:
        """Try to match SQL comments (-- or /* */)."""
        if self.pos >= len(self.sql):
            return False

        # Line comment: --
        if self.sql[self.pos:self.pos+2] == '--':
            start = self.pos
            self.pos += 2
            while self.pos < len(self.sql) and self.sql[self.pos] != '\n':
                self.pos += 1
            if self.pos < len(self.sql):
                self.pos += 1  # Include newline
            self.tokens.append(Token(TokenType.COMMENT, self.sql[start:self.pos], start))
            return True

        # Block comment: /* */
        if self.sql[self.pos:self.pos+2] == '/*':
            start = self.pos
            self.pos += 2
            while self.pos < len(self.sql) - 1:
                if self.sql[self.pos:self.pos+2] == '*/':
                    self.pos += 2
                    break
                self.pos += 1
            self.tokens.append(Token(TokenType.COMMENT, self.sql[start:self.pos], start))
            return True

        return False

    def _try_string(self) -> bool:
        """Try to match a string literal."""
        if self.pos >= len(self.sql):
            return False

        quote = self.sql[self.pos]
        if quote not in ('"', "'"):
            return False

        start = self.pos
        self.pos += 1

        # Scan until closing quote, handling escapes
        while self.pos < len(self.sql):
            if self.sql[self.pos] == '\\':
                # Skip escaped character
                self.pos += 2
                continue
            if self.sql[self.pos] == quote:
                self.pos += 1
                break
            self.pos += 1

        self.tokens.append(Token(TokenType.STRING, self.sql[start:self.pos], start))
        return True

    def _try_number(self) -> bool:
        """Try to match a number."""
        if self.pos >= len(self.sql):
            return False

        if not self.sql[self.pos].isdigit():
            return False

        start = self.pos
        while self.pos < len(self.sql) and (self.sql[self.pos].isdigit() or self.sql[self.pos] == '.'):
            self.pos += 1

        self.tokens.append(Token(TokenType.NUMBER, self.sql[start:self.pos], start))
        return True

    def _try_single_char(self) -> bool:
        """Try to match single-character tokens."""
        if self.pos >= len(self.sql):
            return False

        char = self.sql[self.pos]
        token_map = {
            '[': TokenType.LBRACKET,
            ']': TokenType.RBRACKET,
            '{': TokenType.LBRACE,
            '}': TokenType.RBRACE,
            '(': TokenType.LPAREN,
            ')': TokenType.RPAREN,
            ',': TokenType.COMMA,
            ':': TokenType.COLON,
        }

        if char in token_map:
            self.tokens.append(Token(token_map[char], char, self.pos))
            self.pos += 1
            return True

        return False

    def _try_identifier(self) -> bool:
        """Try to match an identifier (column name, keyword, etc.)."""
        if self.pos >= len(self.sql):
            return False

        if not (self.sql[self.pos].isalpha() or self.sql[self.pos] == '_'):
            return False

        start = self.pos
        while self.pos < len(self.sql) and (self.sql[self.pos].isalnum() or self.sql[self.pos] in '_$.'):
            self.pos += 1

        self.tokens.append(Token(TokenType.IDENTIFIER, self.sql[start:self.pos], start))
        return True


class LexerBasedConverter:
    """
    Convert Python syntax to SQL using lexical analysis.

    More robust than regex-based approach.
    """

    def __init__(self):
        pass

    def convert(self, sql: str) -> str:
        """Convert Python syntax to SQL."""
        lexer = SQLLexer(sql)
        tokens = lexer.tokenize()

        # Convert tokens
        converted_tokens = self._convert_tokens(tokens)

        # Reconstruct SQL
        return ''.join(token.value for token in converted_tokens)

    def _convert_tokens(self, tokens: List[Token]) -> List[Token]:
        """Convert tokens, replacing Python syntax with SQL."""
        result: List[Token] = []
        i = 0

        while i < len(tokens):
            token = tokens[i]

            # Check for array/object literals that should produce json_variant values.
            if token.type == TokenType.LBRACKET:
                if not self._is_postfix_bracket_access(tokens, i):
                    array_tokens, end_pos = self._try_extract_array(tokens, i)
                    if array_tokens is not None:
                        result.append(Token(TokenType.IDENTIFIER, 'to_variant_object', token.position))
                        result.append(Token(TokenType.LPAREN, '(', token.position))
                        result.append(Token(TokenType.IDENTIFIER, 'array', token.position))
                        result.append(Token(TokenType.LPAREN, '(', token.position))
                        result.extend(array_tokens)
                        result.append(Token(TokenType.RPAREN, ')', tokens[end_pos].position))
                        result.append(Token(TokenType.RPAREN, ')', tokens[end_pos].position))
                        i = end_pos + 1
                        continue

            elif token.type == TokenType.LBRACE:
                object_tokens, end_pos = self._try_extract_object(tokens, i)
                if object_tokens is not None:
                    result.append(Token(TokenType.IDENTIFIER, 'to_variant_object', token.position))
                    result.append(Token(TokenType.LPAREN, '(', token.position))
                    result.append(Token(TokenType.IDENTIFIER, 'named_struct', token.position))
                    result.append(Token(TokenType.LPAREN, '(', token.position))
                    result.extend(object_tokens)
                    result.append(Token(TokenType.RPAREN, ')', tokens[end_pos].position))
                    result.append(Token(TokenType.RPAREN, ')', tokens[end_pos].position))
                    i = end_pos + 1
                    continue

            # Keep token as-is
            result.append(token)
            i += 1

        return result

    @staticmethod
    def _is_postfix_bracket_access(tokens: List[Token], start: int) -> bool:
        keyword_like_identifiers = {
            "SELECT",
            "FROM",
            "WHERE",
            "WHEN",
            "THEN",
            "ELSE",
            "AS",
            "AND",
            "OR",
            "NOT",
            "IN",
            "ON",
            "JOIN",
            "LEFT",
            "RIGHT",
            "INNER",
            "OUTER",
            "WITH",
            "BY",
            "GROUP",
            "ORDER",
            "LIMIT",
            "CASE",
            "RETURN",
        }
        previous_index = start - 1
        while previous_index >= 0:
            previous = tokens[previous_index]
            if previous.type in {TokenType.WHITESPACE, TokenType.COMMENT}:
                previous_index -= 1
                continue
            if previous.type == TokenType.IDENTIFIER and previous.value.upper() in keyword_like_identifiers:
                return False
            return previous.type in {
                TokenType.IDENTIFIER,
                TokenType.NUMBER,
                TokenType.STRING,
                TokenType.RPAREN,
                TokenType.RBRACKET,
                TokenType.RBRACE,
                TokenType.COLON,
            }
        return False

    def _try_extract_array(self, tokens: List[Token], start: int) -> Tuple[Optional[List[Token]], int]:
        """
        Try to extract array contents from tokens.

        Returns (array_contents, end_position) or (None, start) if not an array literal.
        """
        if tokens[start].type != TokenType.LBRACKET:
            return None, start

        # Find matching ]
        depth = 0
        i = start

        while i < len(tokens):
            if tokens[i].type == TokenType.LBRACKET:
                depth += 1
            elif tokens[i].type == TokenType.RBRACKET:
                depth -= 1
                if depth == 0:
                    # Found matching bracket
                    # Extract and convert contents
                    contents = tokens[start+1:i]
                    converted_contents = self._convert_tokens(contents)
                    return converted_contents, i
            i += 1

        # No matching bracket found - not a valid array
        return None, start

    def _try_extract_object(self, tokens: List[Token], start: int) -> Tuple[Optional[List[Token]], int]:
        """
        Try to extract object contents and convert to named_struct format.

        Returns (object_contents, end_position) or (None, start) if not an object literal.
        """
        if tokens[start].type != TokenType.LBRACE:
            return None, start

        # Find matching }
        depth = 0
        i = start

        while i < len(tokens):
            if tokens[i].type == TokenType.LBRACE:
                depth += 1
            elif tokens[i].type == TokenType.RBRACE:
                depth -= 1
                if depth == 0:
                    contents = tokens[start+1:i]
                    object_contents = self._dict_contents_to_named_struct(contents)
                    return object_contents, i
            i += 1

        # No matching brace found
        return None, start

    def _dict_contents_to_named_struct(self, tokens: List[Token]) -> List[Token]:
        """
        Convert object contents to named_struct format.

        Input:  'key1': value1, 'key2': value2
        Output: 'key1', value1, 'key2', value2
        """
        result: List[Token] = []
        i = 0

        while i < len(tokens):
            # Skip whitespace
            if tokens[i].type == TokenType.WHITESPACE:
                i += 1
                continue

            # Expect key (string)
            if tokens[i].type != TokenType.STRING:
                result.append(tokens[i])
                i += 1
                continue

            key_token = tokens[i]
            i += 1

            # Skip whitespace
            while i < len(tokens) and tokens[i].type == TokenType.WHITESPACE:
                i += 1

            # Expect colon
            if i >= len(tokens) or tokens[i].type != TokenType.COLON:
                result.append(key_token)
                continue

            i += 1  # Skip colon

            # Skip whitespace
            while i < len(tokens) and tokens[i].type == TokenType.WHITESPACE:
                i += 1

            # Collect value tokens until comma or end
            value_tokens: List[Token] = []
            depth = 0

            while i < len(tokens):
                token = tokens[i]

                # Track depth
                if token.type in (TokenType.LPAREN, TokenType.LBRACKET, TokenType.LBRACE):
                    depth += 1
                elif token.type in (TokenType.RPAREN, TokenType.RBRACKET, TokenType.RBRACE):
                    depth -= 1

                # Stop at comma at depth 0
                if token.type == TokenType.COMMA and depth == 0:
                    break

                value_tokens.append(token)
                i += 1

            result.append(key_token)
            result.append(Token(TokenType.COMMA, ', ', key_token.position))

            converted_value = self._convert_tokens(value_tokens)
            result.extend(converted_value)

            # Add comma if there are more pairs
            if i < len(tokens) and tokens[i].type == TokenType.COMMA:
                result.append(tokens[i])
                i += 1

        return result


def convert_with_lexer(sql: str) -> str:
    """
    Convert Python syntax to SQL using lexer-based approach.

    More robust than regex for complex scenarios.
    """
    converter = LexerBasedConverter()
    return converter.convert(sql)


def normalize_python_syntax(sql: str) -> str:
    """
    Normalize parser-owned Python-like SQL syntax.

    Supported:
    - Python-style [] and {} literals
    - Triple-quoted string literals

    Rejected explicitly:
    - Python f-strings
    """
    _reject_f_strings(sql)
    normalized_sql = _normalize_triple_quoted_strings(sql)
    return convert_with_lexer(normalized_sql)


def _reject_f_strings(sql: str) -> None:
    i = 0
    while i < len(sql):
        if sql[i].isalpha():
            start = i
            while i < len(sql) and sql[i].isalpha():
                i += 1
            prefix = sql[start:i]
            previous_char = sql[start - 1] if start > 0 else ""
            if (
                i < len(sql)
                and sql[i] in {"'", '"'}
                and (start == 0 or not (previous_char.isalnum() or previous_char == "_"))
                and prefix
                and all(char in "rRuUbBfF" for char in prefix)
            ):
                if "f" in prefix.lower():
                    raise ValueError("Python f-strings are not supported in SQL scripts.")
            continue
        i += 1


def _normalize_triple_quoted_strings(sql: str) -> str:
    result: List[str] = []
    i = 0
    while i < len(sql):
        quote = _triple_quote_at(sql, i)
        if quote is None:
            result.append(sql[i])
            i += 1
            continue

        end = sql.find(quote, i + 3)
        if end == -1:
            raise ValueError("Unterminated triple-quoted string literal.")
        content = sql[i + 3:end]
        result.append(_sql_single_quoted_string(content))
        i = end + 3

    return "".join(result)


def _triple_quote_at(sql: str, index: int) -> Optional[str]:
    segment = sql[index:index + 3]
    if segment == "'''" or segment == '"""':
        return segment
    return None


def _sql_single_quoted_string(content: str) -> str:
    escaped = content.replace("'", "''")
    return f"'{escaped}'"
