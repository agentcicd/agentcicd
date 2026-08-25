from __future__ import annotations

import json
from pathlib import Path


def load_golden_script(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def load_golden_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

