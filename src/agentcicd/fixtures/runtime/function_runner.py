from __future__ import annotations

from agentcicd.sandbox import function_runner as _function_runner

globals().update(
    {
        name: value
        for name, value in _function_runner.__dict__.items()
        if name not in {"__builtins__", "__cached__", "__file__", "__loader__", "__name__", "__package__", "__spec__"}
    }
)


if __name__ == "__main__":
    raise SystemExit(_function_runner.main())
