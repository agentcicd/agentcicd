from __future__ import annotations

import base64
import csv
import io
import json
from typing import Any, Callable, Optional, Tuple

from agentcicd.fixtures.core.function import AsyncRowFunction, Function
from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import BooleanType, DType, FType, IntType, StringType
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


class ParseJsonRowFunction(AsyncRowFunction):
    async def transform(
        self,
        content: Optional[str],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        _ = timeout, retry
        if not content:
            return None
        parsed = json.loads(content)
        return json.dumps(parsed, ensure_ascii=False)


class ParseJsonUdf(Udf, name="data.parse_json"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(),)

    def input_args(self) -> Tuple[str, ...]:
        return ("content",)

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return ParseJsonRowFunction()


class ParsePdfRowFunction(AsyncRowFunction):
    async def transform(
        self,
        content_base64: Optional[str],
        max_pages: Optional[int],
        max_chars: Optional[int],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        _ = timeout, retry
        if not content_base64:
            return None

        resolved_max_pages = _safe_int(max_pages, default=50)
        resolved_max_chars = _safe_int(max_chars, default=200_000)

        try:
            from pypdf import PdfReader
        except Exception as exc:  # pragma: no cover
            return json.dumps({"error": f"pypdf_unavailable: {exc}"}, ensure_ascii=False)

        reader = PdfReader(io.BytesIO(_decode_base64_bytes(content_base64)))

        page_count = len(reader.pages)
        page_texts: list[dict[str, Any]] = []
        collected: list[str] = []
        char_count = 0
        truncated = False

        for idx, page in enumerate(reader.pages):
            if idx >= resolved_max_pages:
                truncated = True
                break
            text = page.extract_text() or ""
            page_texts.append({"page": idx + 1, "text": text})
            collected.append(text)
            char_count += len(text)
            if char_count > resolved_max_chars:
                truncated = True
                break

        merged = "\n\n".join(collected)
        if len(merged) > resolved_max_chars:
            merged = merged[:resolved_max_chars]

        return json.dumps(
            {
                "page_count": page_count,
                "parsed_pages": len(page_texts),
                "text": merged,
                "pages": page_texts,
                "truncated": truncated,
            },
            ensure_ascii=False,
        )


class ParsePdfUdf(Udf, name="data.parse_pdf"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), IntType(), IntType())

    def input_args(self) -> Tuple[str, ...]:
        return ("content_base64", "max_pages", "max_chars")

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return ParsePdfRowFunction()


class ParseCsvRowFunction(AsyncRowFunction):
    async def transform(
        self,
        content: Optional[str],
        delimiter: Optional[str],
        quotechar: Optional[str],
        has_header: Optional[bool],
        max_rows: Optional[int],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        _ = timeout, retry
        if content is None:
            return None

        resolved_delimiter = (delimiter or ",")[:1] or ","
        resolved_quotechar = (quotechar or '"')[:1] or '"'
        resolved_has_header = True if has_header is None else bool(has_header)
        resolved_max_rows = _safe_int(max_rows, default=500)

        stream = io.StringIO(content)
        rows: list[dict[str, Any]] = []
        columns: list[str] = []
        truncated = False

        if resolved_has_header:
            reader = csv.DictReader(stream, delimiter=resolved_delimiter, quotechar=resolved_quotechar)
            columns = [str(column) for column in (reader.fieldnames or [])]
            for idx, row in enumerate(reader):
                if idx >= resolved_max_rows:
                    truncated = True
                    break
                rows.append({str(key): ("" if value is None else str(value)) for key, value in (row or {}).items()})
        else:
            reader = csv.reader(stream, delimiter=resolved_delimiter, quotechar=resolved_quotechar)
            for idx, row in enumerate(reader):
                if idx >= resolved_max_rows:
                    truncated = True
                    break
                if idx == 0:
                    columns = [f"c{i + 1}" for i in range(len(row))]
                rows.append({columns[i]: ("" if row[i] is None else str(row[i])) for i in range(len(row))})

        return json.dumps(
            {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
            },
            ensure_ascii=False,
        )


class ParseCsvUdf(Udf, name="data.parse_csv"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), StringType(), BooleanType(), IntType())

    def input_args(self) -> Tuple[str, ...]:
        return ("content", "delimiter", "quotechar", "has_header", "max_rows")

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return ParseCsvRowFunction()


class ParseParquetRowFunction(AsyncRowFunction):
    async def transform(
        self,
        content_base64: Optional[str],
        columns_csv: Optional[str],
        max_rows: Optional[int],
        timeout: TimeoutConfig,
        retry: RetryConfig,
    ) -> Optional[str]:
        _ = timeout, retry
        if not content_base64:
            return None

        resolved_max_rows = _safe_int(max_rows, default=500)
        columns: Optional[list[str]] = None
        if columns_csv and columns_csv.strip():
            columns = [part.strip() for part in columns_csv.split(",") if part.strip()]

        try:
            import pyarrow.parquet as pq
        except Exception as exc:  # pragma: no cover
            return json.dumps({"error": f"pyarrow_unavailable: {exc}"}, ensure_ascii=False)

        table = pq.read_table(io.BytesIO(_decode_base64_bytes(content_base64)), columns=columns)
        total_rows = int(table.num_rows)
        sliced = table.slice(0, resolved_max_rows)

        return json.dumps(
            {
                "columns": [str(name) for name in table.column_names],
                "rows": sliced.to_pylist(),
                "row_count": total_rows,
                "truncated": total_rows > resolved_max_rows,
            },
            ensure_ascii=False,
        )


class ParseParquetUdf(Udf, name="data.parse_parquet"):
    def input_schema(self) -> Tuple[DType, ...]:
        return (StringType(), StringType(), IntType())

    def input_args(self) -> Tuple[str, ...]:
        return ("content_base64", "columns_csv", "max_rows")

    def output_schema(self) -> DType:
        return StringType()

    def ftype(self) -> FType:
        return FType.BATCH_FUNCTION

    def function(self) -> Callable[..., Function]:
        return self._create_function

    def _create_function(self) -> Function:
        return ParseParquetRowFunction()
