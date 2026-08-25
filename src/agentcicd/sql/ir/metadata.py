from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlglot import expressions as exp


@dataclass
class CellComponentsIR:
    value_sql: exp.Expression
    error_sql: Optional[exp.Expression] = None
    cell_sql: Optional[exp.Expression] = None
    representation: str = "raw"
    latency_sql: Optional[exp.Expression] = None
