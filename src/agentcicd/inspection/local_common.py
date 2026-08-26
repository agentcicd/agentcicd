from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


SAFE_ARTIFACT_SUFFIXES = frozenset({".html", ".json", ".jsonl", ".log", ".md", ".sql", ".txt"})
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 1000
LOCAL_FIXTURE_CALL_PATTERN = re.compile(r"\blocal\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.IGNORECASE)
ANNOTATION_CONSENSUS_POLICIES = {"none", "majority", "unanimous"}


@dataclass(frozen=True, slots=True)
class LocalRunReference:
    run_id: str
    path: Path
