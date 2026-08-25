from __future__ import annotations


def wrapped_runtime_alias(runtime_alias: str) -> str:
    alias = str(runtime_alias or "").strip()
    if not alias:
        raise ValueError("Runtime alias is required for wrapped runtime registration")
    return f"agentcicd_wrapped_{alias}"
