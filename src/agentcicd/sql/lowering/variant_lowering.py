from __future__ import annotations

from typing import Optional, Sequence, Set
from sqlglot import expressions as exp

from agentcicd.sql.json_semantics import json_path_from_segments, lower_json_access


def lower_variant_path(
    base: exp.Expression,
    path: Sequence[str | int],
    *,
    variant_columns: Optional[Set[str]] = None,
) -> exp.Expression:
    return lower_json_access(base, json_path_from_segments(path), variant_columns=variant_columns)
