from __future__ import annotations

import json

import pytest

from agentcicd.sql.runtime.rate_limits import limiter_from_control_values
from agentcicd.sql.runtime.transports.http import http_timeout_seconds, runtime_http_request


pytestmark = pytest.mark.smoke


def test_http_runtime_request_payload_snapshot():
    request = runtime_http_request(
        base_url="https://fixtures.local/",
        invoke_path="/invoke",
        args={"prompt": "hello", "n": 2},
        timeout_seconds=30,
    )

    assert request.url == "https://fixtures.local/invoke"
    assert json.loads(request.body().decode("utf-8")) == {"args": {"prompt": "hello", "n": 2}}
    urllib_request = request.to_urllib_request()
    assert urllib_request.method == "POST"
    assert urllib_request.headers["Content-type"] == "application/json"


def test_timeout_config_and_limiter_control_model():
    assert http_timeout_seconds({"timeout_seconds": "15"}, default=900) == 15
    assert http_timeout_seconds({"timeout_seconds": "-1"}, default=900) == 900

    limiter = limiter_from_control_values(
        [{"kind": "ratelimit", "key": "fixture-a", "max_in_flight": "3"}],
        fallback_key="runtime",
    )
    assert limiter.key == "fixture-a"
    assert limiter.max_in_flight == 3
