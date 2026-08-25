from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

from agentcicd.fixtures.core.function import AsyncRowFunction, Function
from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import DType, FType, JsonType, StringType
from agentcicd.fixtures.core.udf import Param, Udf


def _matches_fence_type(candidate: str, expected: Optional[str]) -> bool:
    if expected is None:
        return True
    return candidate.strip().lower() == expected.strip().lower()


def _normalize_fence_content(content: str) -> str:
    if "\n" in content or "\r" in content:
        return content
    return (
        content
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
    )


def _extract_fenced_blocks(content: str, fence_type: Optional[str]) -> list[Any]:
    results: list[Any] = []
    lines = _normalize_fence_content(content).splitlines()
    idx = 0

    while idx < len(lines):
        line = lines[idx]
        stripped = line.lstrip()
        if not stripped.startswith("```"):
            idx += 1
            continue

        open_spec = stripped[3:].strip()
        block_type = open_spec.split(None, 1)[0] if open_spec else ""
        idx += 1
        block_lines: list[str] = []

        while idx < len(lines):
            candidate = lines[idx]
            if candidate.lstrip().startswith("```"):
                break
            block_lines.append(candidate)
            idx += 1

        if idx < len(lines) and lines[idx].lstrip().startswith("```"):
            if _matches_fence_type(block_type, fence_type):
                fenced_value = "\n".join(block_lines).strip()
                if fence_type is None:
                    results.append([block_type, fenced_value])
                else:
                    results.append(fenced_value)
            idx += 1

    return results


class ExtractFromFenceRowFunction(AsyncRowFunction):
    async def transform(
        self,
        content: Optional[str],
        fence_type: Optional[str],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> list[Any]:
        _ = timeout, retry
        if not content:
            return []
        return _extract_fenced_blocks(content, fence_type)


class ExtractFromFenceUdf(Udf, name="string.extract_from_fence"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType())

    def input_args(self) -> Tuple[str, ...]:
        return tuple(parameter.name for parameter in self.signature())

    def signature(self) -> Tuple[Param, ...]:
        return (
            Param("content", required=True),
            Param("fence_type", required=False),
        )

    def output_schema(self) -> DType:
        return JsonType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return ExtractFromFenceRowFunction()
