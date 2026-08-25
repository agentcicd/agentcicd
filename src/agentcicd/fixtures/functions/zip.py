from __future__ import annotations

import base64
import io
import json
import tarfile
import zipfile
from typing import Any, Callable, Optional, Tuple

from agentcicd.fixtures.core.function import AsyncRowFunction, Function
from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import DType, FType, IntType, StringType
from agentcicd.fixtures.core.udf import Udf


def _safe_int(value: Optional[int], default: int, minimum: int = 1) -> int:
    try:
        resolved = int(value) if value is not None else default
    except Exception:
        resolved = default
    return max(minimum, resolved)


def _decode_base64_bytes(content_base64: Optional[str]) -> bytes:
    if not content_base64:
        return b""
    return base64.b64decode(content_base64, validate=True)


def _member_paths(member_paths_json: Optional[str]) -> list[str]:
    if not member_paths_json:
        return []
    parsed = json.loads(member_paths_json)
    if isinstance(parsed, dict) and parsed.get("__agentcicd_cell") is True and "value" in parsed:
        parsed = parsed["value"]
    if not isinstance(parsed, list):
        raise ValueError("Expected member_paths_json to be a JSON array")
    return [str(item) for item in parsed]


def _append_bundle(bundle_parts: list[str], text: str, idx: int, remaining: int) -> int:
    if remaining <= 0:
        return 0
    part = f"BEGIN DOCUMENT {idx}:\n{text}\nEND DOCUMENT {idx}"
    if len(part) > remaining:
        bundle_parts.append(part[:remaining])
        return 0
    bundle_parts.append(part)
    return remaining - len(part)


class UnzipRowFunction(AsyncRowFunction):
    async def transform(
        self,
        content_base64: Optional[str],
        member_paths_json: Optional[str],
        max_chars: Optional[int],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        _ = timeout, retry
        if not content_base64 or not member_paths_json:
            return None

        members = _member_paths(member_paths_json)
        remaining = _safe_int(max_chars, default=2_000_000)
        bundle_parts: list[str] = []

        with zipfile.ZipFile(io.BytesIO(_decode_base64_bytes(content_base64))) as archive:
            for idx, member in enumerate(members, start=1):
                with archive.open(member) as handle:
                    text = handle.read().decode("utf-8", errors="replace")
                remaining = _append_bundle(bundle_parts, text, idx, remaining)
                if remaining <= 0:
                    break

        return "\n\n".join(bundle_parts)


class UnzipUdf(Udf, name="zip.unzip"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), IntType())

    def input_args(self) -> Tuple[str, ...]:
        return ("content_base64", "member_paths_json", "max_chars")

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return UnzipRowFunction()


class UntarRowFunction(AsyncRowFunction):
    async def transform(
        self,
        content_base64: Optional[str],
        member_paths_json: Optional[str],
        max_chars: Optional[int],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        _ = timeout, retry
        if not content_base64 or not member_paths_json:
            return None

        members = _member_paths(member_paths_json)
        remaining = _safe_int(max_chars, default=2_000_000)
        bundle_parts: list[str] = []

        with tarfile.open(fileobj=io.BytesIO(_decode_base64_bytes(content_base64)), mode="r:*") as archive:
            for idx, member in enumerate(members, start=1):
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                text = handle.read().decode("utf-8", errors="replace")
                remaining = _append_bundle(bundle_parts, text, idx, remaining)
                if remaining <= 0:
                    break

        return "\n\n".join(bundle_parts)


class UntarUdf(Udf, name="zip.untar"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), IntType())

    def input_args(self) -> Tuple[str, ...]:
        return ("content_base64", "member_paths_json", "max_chars")

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return UntarRowFunction()
