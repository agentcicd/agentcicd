from __future__ import annotations

import sys

from agentcicd.sandbox import manager as _manager

sys.modules[__name__] = _manager
