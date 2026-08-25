from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Mapping, Optional, Tuple

from pydantic import Field
from agentcicd.fixtures.aisystem import CompletionRequest, acompletion, create_aiohttp_session
from agentcicd.fixtures.core.function import AsyncRowFunction, Function
from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import DType, FType, IntType, JsonEncodedPydanticType, AgentCICDModel, StringType
from agentcicd.fixtures.core.udf import Udf

from .utils.runtime_context import merge_litellm_payload_with_secret


class AgentMessage(AgentCICDModel):
    role: str
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class AgentInitialData(AgentCICDModel):
    messages: list[AgentMessage] = Field(default_factory=list)


class ToolHandlerLookupRow(AgentCICDModel):
    when: dict[str, Any]
    output: Any = None


class ToolHandler(AgentCICDModel):
    type: str = "echo"
    output: Any = None
    template: str = ""
    table: list[ToolHandlerLookupRow] = Field(default_factory=list)
    default: Any = None


class ToolSpec(AgentCICDModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    handler: Optional[ToolHandler] = None


class UserSimulatorConfig(AgentCICDModel):
    mode: str = "llm"
    max_turns: Optional[int] = None
    turns: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    prompt: Optional[str] = None


class SimpleAgentOptions(AgentCICDModel):
    model: str
    secret_id: Optional[str] = None
    tool_choice: Optional[str] = None
    parallel_tool_calls: Optional[bool] = None
    user_model: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> "SimpleAgentOptions":
        if isinstance(obj, Mapping):
            known = {
                "model": obj.get("model"),
                "secret_id": obj.get("secret_id"),
                "tool_choice": obj.get("tool_choice"),
                "parallel_tool_calls": obj.get("parallel_tool_calls"),
                "user_model": obj.get("user_model"),
            }
            extra = {
                str(key): value
                for key, value in obj.items()
                if key not in {"model", "secret_id", "tool_choice", "parallel_tool_calls", "user_model"}
            }
            return super().model_validate({**known, "extra": extra}, *args, **kwargs)
        return super().model_validate(obj, *args, **kwargs)


class AssistantToolFunction(AgentCICDModel):
    name: str = ""
    arguments: Any = None


class AssistantToolCall(AgentCICDModel):
    id: str = ""
    function: AssistantToolFunction = Field(default_factory=AssistantToolFunction)


class AssistantMessage(AgentCICDModel):
    content: str = ""
    tool_calls: list[AssistantToolCall] = Field(default_factory=list)


class AssistantChoice(AgentCICDModel):
    message: AssistantMessage = Field(default_factory=AssistantMessage)


class AssistantUsage(AgentCICDModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class AssistantResponsePayload(AgentCICDModel):
    choices: list[AssistantChoice] = Field(default_factory=list)
    usage: Optional[AssistantUsage] = None


def _to_json_obj(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if isinstance(parsed, Mapping):
        return dict(parsed)
    raise ValueError("Expected JSON object")


def _to_json_list(raw: Optional[str]) -> list[Any]:
    if not raw:
        return []
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        return list(parsed)
    raise ValueError("Expected JSON array")


def _now_ns() -> int:
    return time.time_ns()


def _trace_id() -> str:
    return uuid.uuid4().hex


def _span_id() -> str:
    return uuid.uuid4().hex[:16]


def _otel_any(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if value is None:
        return {"stringValue": "null"}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"stringValue": json.dumps(value, ensure_ascii=False)}


def _otel_attrs(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"key": str(k), "value": _otel_any(v)} for k, v in values.items()]


def _extract_assistant_message(payload: Mapping[str, Any]) -> AssistantMessage:
    parsed = AssistantResponsePayload.model_validate(payload)
    if not parsed.choices:
        return AssistantMessage()
    return parsed.choices[0].message


def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, Mapping):
        return dict(raw_args)
    if isinstance(raw_args, str):
        text = raw_args.strip()
        if not text:
            return {}
        parsed = json.loads(text)
        if isinstance(parsed, Mapping):
            return dict(parsed)
        return {"_value": parsed}
    return {}


def _tool_lookup_match(when: Mapping[str, Any], args: Mapping[str, Any]) -> bool:
    for key, expected in when.items():
        if key not in args or args[key] != expected:
            return False
    return True


def _render_template(template: str, args: Mapping[str, Any]) -> str:
    rendered = template
    for key, value in args.items():
        rendered = rendered.replace("{" + str(key) + "}", str(value))
    return rendered


def _execute_tool(tool_spec: ToolSpec, tool_args: Mapping[str, Any]) -> Any:
    handler = tool_spec.handler
    if handler is None:
        return {"ok": True, "tool": tool_spec.name, "arguments": dict(tool_args)}
    mode = handler.type.strip().lower()
    if mode == "static":
        return handler.output
    if mode == "template":
        return {"text": _render_template(handler.template, tool_args)}
    if mode == "lookup":
        for row in handler.table:
            if _tool_lookup_match(row.when, tool_args):
                return row.output
        return handler.default
    return {"ok": True, "tool": tool_spec.name, "arguments": dict(tool_args)}


class SimpleAgentChatRowFunction(AsyncRowFunction):
    def __init__(self) -> None:
        super().__init__()
        self._session = None

    async def _llm_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[Any]],
        options: SimpleAgentOptions,
    ) -> dict[str, Any]:
        if self._session is None:
            self._session = create_aiohttp_session(TimeoutConfig())
        payload: dict[str, Any] = dict(options.extra)
        request = CompletionRequest(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=options.tool_choice,
            parallel_tool_calls=options.parallel_tool_calls,
            **merge_litellm_payload_with_secret(
                {"shared_session": self._session, **payload},
                options.secret_id,
                payload,
            ),
        )
        response = await acompletion(request)
        return response.model_dump()

    async def transform(
        self,
        initial_data_json: Optional[str],
        tools_json: Optional[str],
        user_simulator_json: Optional[str],
        user_prompt: Optional[str],
        max_turns: Optional[int],
        options: Optional[Mapping[str, Any]],
        timeout: Optional[TimeoutConfig] = None,
        retry: Optional[RetryConfig] = None,
    ) -> Optional[str]:
        _ = timeout, retry
        agent_options = SimpleAgentOptions.model_validate(options or {})
        assistant_model = agent_options.model.strip()

        initial_data = AgentInitialData.model_validate(_to_json_obj(initial_data_json))
        tools_spec = [ToolSpec.model_validate(item) for item in _to_json_list(tools_json)]
        user_sim = UserSimulatorConfig.model_validate(_to_json_obj(user_simulator_json))
        turns = max(1, int(max_turns or 8))

        messages = [message.model_dump(exclude_none=True) for message in initial_data.messages]
        if user_prompt and not any(message.role == "user" for message in initial_data.messages):
            messages.append({"role": "user", "content": user_prompt})

        tool_schema = []
        tools_by_name: dict[str, ToolSpec] = {}
        for tool in tools_spec:
            name = tool.name.strip()
            if not name:
                continue
            tools_by_name[name] = tool
            tool_schema.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )

        trace = _trace_id()
        root_span_id = _span_id()
        spans: list[dict[str, Any]] = []
        trajectory: list[dict[str, Any]] = list(messages)
        simulated_turns = 0
        termination = "max_turns"
        usage_prompt = 0
        usage_completion = 0

        root_start = _now_ns()
        for turn in range(turns):
            simulated_turns = turn + 1

            assistant_span = _span_id()
            assistant_start = _now_ns()
            assistant_raw = await self._llm_complete(
                model=assistant_model,
                messages=trajectory,
                tools=tool_schema if tool_schema else None,
                options=agent_options,
            )
            assistant_end = _now_ns()
            assistant_payload = AssistantResponsePayload.model_validate(assistant_raw)
            if assistant_payload.usage is not None:
                usage_prompt += assistant_payload.usage.prompt_tokens
                usage_completion += assistant_payload.usage.completion_tokens
            assistant_msg = _extract_assistant_message(assistant_raw)
            trajectory.append(
                {
                    "role": "assistant",
                    "content": assistant_msg.content,
                    "tool_calls": [call.model_dump() for call in assistant_msg.tool_calls],
                }
            )
            spans.append(
                {
                    "traceId": trace,
                    "spanId": assistant_span,
                    "parentSpanId": root_span_id,
                    "name": "assistant.generate",
                    "kind": 1,
                    "startTimeUnixNano": str(assistant_start),
                    "endTimeUnixNano": str(assistant_end),
                    "attributes": _otel_attrs(
                        {
                            "turn": turn + 1,
                            "model": assistant_model,
                            "tool_call_count": len(assistant_msg.tool_calls),
                        }
                    ),
                    "events": [],
                }
            )

            tool_calls = assistant_msg.tool_calls
            if tool_calls:
                for call in tool_calls:
                    tool_name = call.function.name.strip()
                    args = _parse_tool_args(call.function.arguments)
                    tool_start = _now_ns()
                    tool_spec = tools_by_name.get(tool_name)
                    if tool_spec is None:
                        tool_spec = ToolSpec(name=tool_name)
                    tool_output = _execute_tool(tool_spec, args)
                    tool_end = _now_ns()
                    trajectory.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": tool_name,
                            "content": json.dumps(tool_output, ensure_ascii=False),
                        }
                    )
                    spans.append(
                        {
                            "traceId": trace,
                            "spanId": _span_id(),
                            "parentSpanId": assistant_span,
                            "name": f"tool.{tool_name}",
                            "kind": 2,
                            "startTimeUnixNano": str(tool_start),
                            "endTimeUnixNano": str(tool_end),
                            "attributes": _otel_attrs(
                                {
                                    "tool.name": tool_name,
                                    "tool.arguments": json.dumps(args, ensure_ascii=False),
                                }
                            ),
                            "events": [],
                        }
                    )
                continue

            user_mode = user_sim.mode.strip().lower()
            user_stop_after = int(user_sim.max_turns or turns)
            if turn + 1 >= user_stop_after:
                termination = "user_stop"
                break

            if user_mode == "scripted":
                scripted = user_sim.turns
                if turn < len(scripted):
                    text = str(scripted[turn])
                else:
                    termination = "user_script_done"
                    break
                trajectory.append({"role": "user", "content": text})
                continue

            user_model = str(user_sim.model or agent_options.user_model or assistant_model).strip()
            user_prompt_template = str(user_sim.prompt or "").strip()
            user_system = (
                user_prompt_template
                or "You are a realistic user in a support chat. Respond with one concise user message."
            )
            user_messages = [{"role": "system", "content": user_system}]
            user_messages.extend(trajectory)
            user_start = _now_ns()
            user_raw = await self._llm_complete(
                model=user_model,
                messages=user_messages,
                tools=None,
                options=agent_options,
            )
            user_end = _now_ns()
            user_msg = _extract_assistant_message(user_raw)
            user_text = user_msg.content.strip()
            if not user_text:
                termination = "user_empty"
                break
            trajectory.append({"role": "user", "content": user_text})
            spans.append(
                {
                    "traceId": trace,
                    "spanId": _span_id(),
                    "parentSpanId": root_span_id,
                    "name": "user_simulator.generate",
                    "kind": 1,
                    "startTimeUnixNano": str(user_start),
                    "endTimeUnixNano": str(user_end),
                    "attributes": _otel_attrs({"turn": turn + 1, "model": user_model}),
                    "events": [],
                }
            )

        root_end = _now_ns()
        root_span = {
            "traceId": trace,
            "spanId": root_span_id,
            "name": "simple_agent.chat",
            "kind": 1,
            "startTimeUnixNano": str(root_start),
            "endTimeUnixNano": str(root_end),
            "attributes": _otel_attrs(
                {
                    "turn_count": simulated_turns,
                    "termination_reason": termination,
                    "assistant_model": assistant_model,
                }
            ),
            "events": [],
        }
        otel_payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": _otel_attrs(
                            {
                                "service.name": "agentcicd.simple_agent",
                                "agent.type": "generic",
                            }
                        )
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "agent.simple_agent.chat"},
                            "spans": [root_span, *spans],
                        }
                    ],
                }
            ],
            "summary": {
                "trace_id": trace,
                "turn_count": simulated_turns,
                "termination_reason": termination,
                "usage": {
                    "prompt_tokens": usage_prompt,
                    "completion_tokens": usage_completion,
                    "total_tokens": usage_prompt + usage_completion,
                },
            },
            "trajectory": trajectory,
        }
        return json.dumps(otel_payload, ensure_ascii=False)


class SimpleAgentChatUdf(Udf, name="agent.simple_agent.chat"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (
            StringType(),
            StringType(),
            StringType(),
            StringType(),
            IntType(),
            JsonEncodedPydanticType(SimpleAgentOptions),
        )

    def input_args(self) -> Tuple[str, ...]:
        return (
            "initial_data_json",
            "tools_json",
            "user_simulator_json",
            "user_prompt",
            "max_turns",
            "options",
        )

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return SimpleAgentChatRowFunction()
