from __future__ import annotations

from typing import Mapping, Optional

from aiohttp import ClientSession, ClientTimeout

from agentcicd.fixtures.core.timeout import TimeoutConfig


def build_aiohttp_timeout(config: Optional[TimeoutConfig]) -> Optional[ClientTimeout]:
    if not config:
        return None
    return ClientTimeout(
        total=config.timeout,
        connect=config.connect,
        sock_read=config.read,        
    )


def create_aiohttp_session(
    timeout_config: Optional[TimeoutConfig] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> ClientSession:
    timeout = build_aiohttp_timeout(timeout_config)
    return ClientSession(timeout=timeout, headers=headers)

