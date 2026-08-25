from __future__ import annotations

from typing import Any, Mapping

from agentcicd.fixtures.core.tracing import runtime_trace_span


class AgentHarnessEnvironmentHandle:
    __agentcicd_lazy_environment__ = True

    def __init__(self, *, env_id: str, payload: Mapping[str, Any]) -> None:
        from agentcicd.fixtures.functions.simulators import materialized_mcp_map

        self.kind = "agent_harness"
        self.env_id = env_id
        self.setup_spec = dict(payload)
        self.mcps = materialized_mcp_map(self.setup_spec.get("mcps"))
        if self.mcps:
            self.setup_spec["mcps"] = {
                name: handle.to_agent_mcp_spec()
                for name, handle in self.mcps.items()
            }
        self._session: Any = None
        self.initialized = False

    @property
    def session(self) -> Any | None:
        return self._session

    async def setup(self) -> Any:
        if not self.initialized:
            from agentcicd.fixtures.functions.agent_harness import create_session, setup_spec_from_aisystem_payload

            with runtime_trace_span("agent_harness.setup", {"environment_kind": "agent_harness"}):
                await self._setup_mcps()
                setup_spec = setup_spec_from_aisystem_payload(
                    {"session_id": self.env_id, **dict(self.setup_spec)}
                )
                self._session = create_session(setup_spec)
            self.initialized = True
        return self._session

    async def _setup_mcps(self) -> None:
        for handle in self.mcps.values():
            if handle.start_mode == "early" or handle.requires_setup_for_agent:
                await handle.setup()

    async def setup_attached_mcps(self) -> None:
        await self._setup_mcps()

    async def run_task(
        self,
        task: str | None = None,
        *,
        input: str | None = None,
        timeout: float | None = None,
        timeout_seconds: float | None = None,
        transcript_file: str | None = None,
    ) -> str | None:
        resolved_task = input if input is not None else task
        if resolved_task is None:
            raise ValueError("agent.run_task requires task or input")
        resolved_timeout = timeout_seconds if timeout_seconds is not None else timeout
        with runtime_trace_span(
            "agent_harness.run_task",
            {
                "environment_kind": "agent_harness",
                "method": "run_task",
                "arg_count": 1,
                "kwarg_count": sum(
                    value is not None
                    for value in (
                        resolved_timeout,
                        transcript_file,
                    )
                ),
            },
        ):
            session = await self.setup()
            result = await session.run_task(
                input=resolved_task,
                timeout=resolved_timeout,
                transcript_file=transcript_file,
            )
        return result.final_output

    async def teardown(self, reason: Any = None) -> None:
        session = self._session
        self._session = None
        self.initialized = False
        if session is not None:
            await session.teardown(reason or type("Reason", (), {"code": "teardown", "message": None})())
        for handle in reversed(tuple(self.mcps.values())):
            await handle.teardown(reason)
