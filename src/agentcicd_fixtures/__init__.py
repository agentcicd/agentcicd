from __future__ import annotations

import importlib
import sys

import agentcicd.fixtures as _fixtures

for _submodule in ("aisystem", "builtin_authoring", "functions"):
    sys.modules[f"{__name__}.{_submodule}"] = importlib.import_module(f"agentcicd.fixtures.{_submodule}")

sys.modules[__name__] = _fixtures
