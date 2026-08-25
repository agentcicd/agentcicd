from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import sqlglot


@dataclass
class ValidationResult:
    ok: bool
    engine: str
    sql: str
    error: Optional[str] = None


def validate_lowered_sql(sql: str, *, spark_session=None) -> ValidationResult:
    if sql.strip().upper().startswith(("DECLARE VARIABLE ", "DECLARE OR REPLACE VARIABLE ")):
        return ValidationResult(ok=True, engine="spark_variable", sql=sql)
    if spark_session is not None:
        try:
            spark_session._jsparkSession.sessionState().sqlParser().parsePlan(sql)
            return ValidationResult(ok=True, engine="spark_parser", sql=sql)
        except Exception as exc:  # pragma: no cover - exercised only when Spark is available
            return ValidationResult(ok=False, engine="spark_parser", sql=sql, error=str(exc))

    try:
        sqlglot.parse_one(sql, read="spark")
        return ValidationResult(ok=True, engine="sqlglot_fallback", sql=sql)
    except Exception as exc:
        return ValidationResult(ok=False, engine="sqlglot_fallback", sql=sql, error=str(exc))
