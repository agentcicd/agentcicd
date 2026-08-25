from pathlib import Path

import json
import pytest

from agentcicd.sql.engine.entrypoint import EngineEntrypoint


GOLDEN_CASES = [
    ("basic_batch", []),
    ("window_cells", []),
]


@pytest.mark.parametrize(("case_name", "registered_functions"), GOLDEN_CASES)
def test_golden_lowering_cases(case_name, registered_functions):
    root = Path(__file__).resolve().parent / "golden"
    script = (root / "scripts" / f"{case_name}.sql").read_text(encoding="utf-8")
    expected = json.loads((root / "expected" / f"{case_name}_lowered.json").read_text(encoding="utf-8"))

    lowered = EngineEntrypoint(script, registered_functions=registered_functions).lower_script(include_cells=True)[0]

    for snippet in expected["contains"]:
        assert snippet in lowered
