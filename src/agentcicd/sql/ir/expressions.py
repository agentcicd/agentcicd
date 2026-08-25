from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

from sqlglot import expressions as exp


@dataclass
class ExprIR:
    pass


@dataclass
class SqlAstExpr(ExprIR):
    expression: exp.Expression


@dataclass
class LiteralExpr(ExprIR):
    value: Any


@dataclass
class ColumnRefExpr(ExprIR):
    name: str


@dataclass
class KeywordArgExpr(ExprIR):
    name: str
    value: ExprIR


@dataclass
class CallExpr(ExprIR):
    function_name: str
    args: List[ExprIR] = field(default_factory=list)

    @property
    def positional_args(self) -> List[ExprIR]:
        return [arg for arg in self.args if not isinstance(arg, KeywordArgExpr)]

    @property
    def keyword_args(self) -> List[KeywordArgExpr]:
        return [arg for arg in self.args if isinstance(arg, KeywordArgExpr)]


@dataclass
class VariantPathExpr(ExprIR):
    base: ExprIR
    path: Sequence[str | int]


@dataclass
class AssignmentExpr(ExprIR):
    name: str
    value: ExprIR


@dataclass
class ReturnExpr(ExprIR):
    value: ExprIR
