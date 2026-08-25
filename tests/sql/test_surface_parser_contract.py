from __future__ import annotations

import pytest

from agentcicd.sql.ir.statements import BatchTableStmt, LoadStmt, QueryStmt
from agentcicd.sql.surface import TopLevelParser


pytestmark = pytest.mark.smoke


def test_top_level_parser_is_recipe_to_statement_ir_entrypoint():
    statements = TopLevelParser(
        """
        LOAD cases FROM 'cases.csv';

        CREATE BATCH TABLE evaluated
        SELECT id FROM cases;

        SELECT count(*) FROM evaluated;
        """
    ).parse()

    assert [type(statement) for statement in statements] == [LoadStmt, BatchTableStmt, QueryStmt]
    assert statements[1].name == "evaluated"
