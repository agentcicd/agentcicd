from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised when pyspark is installed
    from pyspark.sql import functions as F
    from pyspark.sql.window import Window
except Exception:  # pragma: no cover
    F = Any  # type: ignore[misc,assignment]
    Window = Any  # type: ignore[misc,assignment]
