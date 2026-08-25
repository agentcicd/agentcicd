from __future__ import annotations

import base64
import io
import json
import sys
import types
from unittest.mock import patch

import pytest

from agentcicd.fixtures.core.retry import RetryConfig
from agentcicd.fixtures.core.timeout import TimeoutConfig
from agentcicd.fixtures.core.types import FType, StringType
from agentcicd.fixtures.functions.data import (
    ParseCsvRowFunction,
    ParseCsvUdf,
    ParseJsonRowFunction,
    ParseJsonUdf,
    ParseParquetRowFunction,
    ParseParquetUdf,
    ParsePdfRowFunction,
    ParsePdfUdf,
)
from agentcicd.fixtures.functions.http import (
    HttpRequestRowFunction,
    HttpRequestUdf,
)
from agentcicd.fixtures.functions.zip import (
    UntarRowFunction,
    UntarUdf,
    UnzipRowFunction,
    UnzipUdf,
)


@pytest.fixture()
def timeout_config() -> TimeoutConfig:
    return TimeoutConfig()


@pytest.fixture()
def retry_config() -> RetryConfig:
    return RetryConfig()


def test_http_request_udf_metadata() -> None:
    udf = HttpRequestUdf()
    input_schema = udf.input_schema()
    assert len(input_schema) == 8
    assert isinstance(input_schema[0], StringType)
    assert isinstance(udf.output_schema(), StringType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), HttpRequestRowFunction)


def test_parse_json_udf_metadata() -> None:
    udf = ParseJsonUdf()
    input_schema = udf.input_schema()
    assert len(input_schema) == 1
    assert isinstance(input_schema[0], StringType)
    assert isinstance(udf.output_schema(), StringType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), ParseJsonRowFunction)


def test_parse_pdf_udf_metadata() -> None:
    udf = ParsePdfUdf()
    input_schema = udf.input_schema()
    assert len(input_schema) == 3
    assert isinstance(input_schema[0], StringType)
    assert isinstance(udf.output_schema(), StringType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), ParsePdfRowFunction)


def test_parse_csv_udf_metadata() -> None:
    udf = ParseCsvUdf()
    input_schema = udf.input_schema()
    assert len(input_schema) == 5
    assert isinstance(input_schema[0], StringType)
    assert isinstance(udf.output_schema(), StringType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), ParseCsvRowFunction)


def test_parse_parquet_udf_metadata() -> None:
    udf = ParseParquetUdf()
    input_schema = udf.input_schema()
    assert len(input_schema) == 3
    assert isinstance(input_schema[0], StringType)
    assert isinstance(udf.output_schema(), StringType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), ParseParquetRowFunction)


def test_unzip_udf_metadata() -> None:
    udf = UnzipUdf()
    input_schema = udf.input_schema()
    assert len(input_schema) == 3
    assert isinstance(input_schema[0], StringType)
    assert isinstance(udf.output_schema(), StringType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), UnzipRowFunction)


def test_untar_udf_metadata() -> None:
    udf = UntarUdf()
    input_schema = udf.input_schema()
    assert len(input_schema) == 3
    assert isinstance(input_schema[0], StringType)
    assert isinstance(udf.output_schema(), StringType)
    assert udf.ftype() == FType.BATCH_FUNCTION
    assert isinstance(udf.function()(), UntarRowFunction)


@pytest.mark.asyncio
async def test_http_request_row_function_json_output(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"ok":true}'
        url = "https://example.com/path"

        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, *args, **kwargs):
            return _FakeResponse()

    row_function = HttpRequestRowFunction()
    with patch("agentcicd_fixtures.functions.http.httpx.AsyncClient", _FakeClient):
        output = await row_function.transform(
            "GET",
            "https://example.com/path",
            None,
            None,
            None,
            False,
            True,
            1024,
            timeout_config,
            retry_config,
        )

    parsed = json.loads(output or "{}")
    assert parsed["status_code"] == 200
    assert parsed["body"] == '{"ok":true}'
    assert parsed["ok"] is True
    assert parsed["body_base64"] != ""


@pytest.mark.asyncio
async def test_parse_json_row_function(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    parse_json_fn = ParseJsonRowFunction()
    parsed = await parse_json_fn.transform('{"a":{"b":[{"c":"ok"}]}}', timeout_config, retry_config)
    assert json.loads(parsed or "{}") == {"a": {"b": [{"c": "ok"}]}}


@pytest.mark.asyncio
async def test_parse_pdf_row_function_uses_base64(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    class _FakePage:
        def __init__(self, text: str):
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _FakePdfReader:
        def __init__(self, data: io.BytesIO):
            assert isinstance(data, io.BytesIO)
            self.pages = [_FakePage("page one"), _FakePage("page two")]

    row_function = ParsePdfRowFunction()
    payload = base64.b64encode(b"%PDF-1.4").decode("ascii")
    with patch.dict(sys.modules, {"pypdf": types.SimpleNamespace(PdfReader=_FakePdfReader)}):
        output = await row_function.transform(payload, 10, 1000, timeout_config, retry_config)

    parsed = json.loads(output or "{}")
    assert parsed["page_count"] == 2
    assert parsed["parsed_pages"] == 2
    assert "page one" in parsed["text"]


@pytest.mark.asyncio
async def test_read_csv_row_function_from_content(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    row_function = ParseCsvRowFunction()
    output = await row_function.transform(
        "id,name\n1,alice\n2,bob\n",
        ",",
        '"',
        True,
        10,
        timeout_config,
        retry_config,
    )

    parsed = json.loads(output or "{}")
    assert parsed["columns"] == ["id", "name"]
    assert parsed["row_count"] == 2
    assert parsed["rows"][0]["name"] == "alice"


@pytest.mark.asyncio
async def test_parse_parquet_row_function_from_base64(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    table = pa.table({"id": [1, 2], "name": ["alice", "bob"]})
    out = io.BytesIO()
    pq.write_table(table, out)
    payload = base64.b64encode(out.getvalue()).decode("ascii")

    row_function = ParseParquetRowFunction()
    output = await row_function.transform(payload, "id,name", 1, timeout_config, retry_config)
    parsed = json.loads(output or "{}")
    assert parsed["row_count"] == 2
    assert parsed["truncated"] is True
    assert parsed["rows"][0]["name"] == "alice"


@pytest.mark.asyncio
async def test_unzip_row_function_preserves_order(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("lcr/A/doc1.txt", "alpha text")
        archive.writestr("lcr/A/doc2.txt", "beta text")

    payload = base64.b64encode(out.getvalue()).decode("ascii")
    row_function = UnzipRowFunction()
    output = await row_function.transform(
        payload,
        json.dumps(["lcr/A/doc2.txt", "lcr/A/doc1.txt"]),
        10_000,
        timeout_config,
        retry_config,
    )

    assert output is not None
    assert "BEGIN DOCUMENT 1:\nbeta text\nEND DOCUMENT 1" in output
    assert "BEGIN DOCUMENT 2:\nalpha text\nEND DOCUMENT 2" in output


@pytest.mark.asyncio
async def test_unzip_row_function_accepts_wrapped_member_paths(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("lcr/A/doc1.txt", "alpha text")

    payload = base64.b64encode(out.getvalue()).decode("ascii")
    wrapped_paths = json.dumps(
        {
            "value": ["lcr/A/doc1.txt"],
            "metadata": {"error": None, "subdatatype": None},
            "__agentcicd_cell": True,
        }
    )

    row_function = UnzipRowFunction()
    output = await row_function.transform(
        payload,
        wrapped_paths,
        10_000,
        timeout_config,
        retry_config,
    )

    assert output is not None
    assert "BEGIN DOCUMENT 1:\nalpha text\nEND DOCUMENT 1" in output


@pytest.mark.asyncio
async def test_untar_row_function_preserves_order(
    timeout_config: TimeoutConfig,
    retry_config: RetryConfig,
) -> None:
    import tarfile

    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        first = tarfile.TarInfo("docs/a.txt")
        first_bytes = b"alpha text"
        first.size = len(first_bytes)
        archive.addfile(first, io.BytesIO(first_bytes))
        second = tarfile.TarInfo("docs/b.txt")
        second_bytes = b"beta text"
        second.size = len(second_bytes)
        archive.addfile(second, io.BytesIO(second_bytes))

    payload = base64.b64encode(out.getvalue()).decode("ascii")
    row_function = UntarRowFunction()
    output = await row_function.transform(
        payload,
        json.dumps(["docs/b.txt", "docs/a.txt"]),
        10_000,
        timeout_config,
        retry_config,
    )

    assert output is not None
    assert "BEGIN DOCUMENT 1:\nbeta text\nEND DOCUMENT 1" in output
    assert "BEGIN DOCUMENT 2:\nalpha text\nEND DOCUMENT 2" in output
