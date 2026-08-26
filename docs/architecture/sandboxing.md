# Sandboxing

AgentCICD routes fixture calls through a local runtime boundary instead of executing arbitrary fixture logic directly in recipe text.

## Current Components

- `src/agentcicd/sandbox/manager.py`: local sandbox manager boundary.
- `src/agentcicd/sandbox/function_runner.py`: function-runner protocol implementation.
- `containers/function_runner/Dockerfile`: container image entry point for function runner work.
- `containers/gvisor_helper/Dockerfile`: gVisor helper container entry point.

## Configuration Surface

Fixture groups in `agentcicd.toml` can specify manager and worker settings:

```toml
[[fixture_groups]]
name = "default"
paths = ["fixtures"]
manager_mode = "executor_local"
worker_substrate = "subprocess"
pool_kind = "service"
max_workers = 1
timeout_seconds = 300
```

Current enum values in configuration include:

- manager modes: `executor_local`, `driver_local`, `static_http`
- worker substrates: `subprocess`, `docker`, `gvisor`
- pool kinds: `service`, `session`, `sandbox`

Keep user-facing claims conservative unless the mode is covered by tests and local runner behavior.
