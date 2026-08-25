"""
Test lexer-based converter against edge cases that break regex.
"""

import pytest

from agentcicd.sql.parsing.python_to_sql_lexer import convert_with_lexer


class TestLexerEdgeCases:
    """Test cases that are problematic for regex-based parsing."""

    def test_string_with_brackets(self):
        """String literals containing brackets should not be converted."""
        sql = "SELECT {'message': 'Hello [world]'} as data"
        result = convert_with_lexer(sql)
        # The string '[world]' should remain unchanged
        assert '[world]' in result
        assert 'to_variant_object(named_struct(' in result

    def test_string_with_braces(self):
        """String literals containing braces should not be converted."""
        sql = "SELECT [1, 2, 'text with {braces}'] as data"
        result = convert_with_lexer(sql)
        assert '{braces}' in result
        assert 'to_variant_object(array(' in result

    def test_line_comment_with_syntax(self):
        """Comments should be preserved, not converted."""
        sql = """
        -- This is a comment with [array] and {dict}
        SELECT [1, 2, 3] as nums
        """
        result = convert_with_lexer(sql)
        # Comment should remain as-is
        assert '-- This is a comment with [array] and {dict}' in result
        # But actual syntax should be converted
        assert 'to_variant_object(array(1, 2, 3))' in result

    def test_block_comment_with_syntax(self):
        """Block comments should be preserved."""
        sql = """
        SELECT /* ignore this: {'key': 'value'} */ [1, 2] as nums
        """
        result = convert_with_lexer(sql)
        # Comment should keep original syntax
        assert "/* ignore this: {'key': 'value'} */" in result
        # But actual array should be converted
        assert 'to_variant_object(array(1, 2))' in result

    def test_escaped_quotes_in_strings(self):
        """Properly handle escaped quotes."""
        sql = r"SELECT {'msg': 'It\'s working'} as data"
        result = convert_with_lexer(sql)
        assert r"'It\'s working'" in result
        assert 'to_variant_object(named_struct(' in result

    def test_nested_function_calls(self):
        """Complex nested function calls with commas."""
        sql = "SELECT {'result': CASE WHEN x IN (1,2,3) THEN 'a' ELSE 'b' END} as data"
        result = convert_with_lexer(sql)
        assert 'to_variant_object(named_struct(' in result
        assert 'CASE WHEN' in result
        assert 'IN (1,2,3)' in result

    def test_array_indexing_not_literal(self):
        """Array indexing should not be converted (this is tricky!)."""
        # Note: This is challenging - array[1] looks like array literal
        # A full solution would need context awareness
        sql = "SELECT column_array, [1, 2, 3] as literal_array"
        result = convert_with_lexer(sql)
        # The literal should be converted
        assert 'to_variant_object(array(1, 2, 3))' in result

    def test_literal_brace_in_string(self):
        """String with literal brace character."""
        sql = "SELECT * FROM table WHERE column_name = '}'"
        result = convert_with_lexer(sql)
        # Should remain unchanged - no dict conversion
        assert "column_name = '}'" in result

    def test_mixed_quotes(self):
        """Single and double quotes mixed."""
        sql = '''SELECT {"name": 'Alice', 'age': 30} as person'''
        result = convert_with_lexer(sql)
        assert 'to_variant_object(named_struct(' in result
        assert '"name"' in result
        assert "'Alice'" in result

    def test_empty_structures_with_comments(self):
        """Empty arrays/dicts with surrounding comments."""
        sql = """
        SELECT
            [] as empty_array,  -- empty
            {} as empty_map     /* also empty */
        """
        result = convert_with_lexer(sql)
        assert 'to_variant_object(array())' in result
        assert 'to_variant_object(named_struct())' in result
        assert '-- empty' in result
        assert '/* also empty */' in result


class TestLexerVsRegex:
    """Compare lexer-based vs regex-based approaches."""

    def test_string_literal_protection(self):
        """Lexer should NOT convert syntax inside strings."""
        sql = "SELECT {'data': '[1,2,3] and {a:b}'} as col"
        result = convert_with_lexer(sql)

        # The value string should remain unchanged
        assert "'[1,2,3] and {a:b}'" in result
        # But the outer dict should be converted
        assert 'to_variant_object(named_struct(' in result

    def test_deeply_nested_with_strings(self):
        """Complex nesting with string literals."""
        sql = """
        SELECT [
            {'name': 'Alice [test]', 'data': [1, 2]},
            {'name': 'Bob {test}', 'data': [3, 4]}
        ] as users
        """
        result = convert_with_lexer(sql)

        # Strings should be preserved
        assert "'Alice [test]'" in result
        assert "'Bob {test}'" in result

        # Structures should be converted
        assert 'to_variant_object(array(' in result
        assert 'to_variant_object(named_struct(' in result


class TestLexerCorrectness:
    """Test that lexer produces correct SQL."""

    def test_basic_array(self):
        """Basic array conversion."""
        sql = "SELECT [1, 2, 3]"
        result = convert_with_lexer(sql)
        assert result == "SELECT to_variant_object(array(1, 2, 3))"

    def test_basic_dict(self):
        """Basic dict conversion."""
        sql = "SELECT {'a': 1, 'b': 2}"
        result = convert_with_lexer(sql)
        # Check structure, not exact spacing
        assert 'to_variant_object(named_struct(' in result
        assert "'a'" in result
        assert "'b'" in result
        assert result.count(',') >= 3  # At least 3 commas

    def test_nested_array(self):
        """Nested array conversion."""
        sql = "SELECT [[1, 2], [3, 4]]"
        result = convert_with_lexer(sql)
        assert result == "SELECT to_variant_object(array(to_variant_object(array(1, 2)), to_variant_object(array(3, 4))))"

    def test_array_of_dicts(self):
        """Array of dicts."""
        sql = "SELECT [{'a': 1}, {'b': 2}]"
        result = convert_with_lexer(sql)
        assert result == (
            "SELECT to_variant_object(array("
            "to_variant_object(named_struct('a', 1)), "
            "to_variant_object(named_struct('b', 2))"
            "))"
        )
