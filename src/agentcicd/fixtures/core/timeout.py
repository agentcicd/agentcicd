from typing import Optional, TypedDict

from .types import AgentCICDModel


class TimeoutConfig(AgentCICDModel):
    """
    Configuration for network timeout settings.

    Attributes:
        timeout: Overall timeout in seconds (default: None)
        read_timeout: Timeout for reading data in seconds (default: None)
        connect_timeout: Timeout for establishing connection in seconds (default: None)
        write_timeout: Timeout for writing data in seconds (default: None)
    """
    timeout: Optional[float] = 60
    read: Optional[float] = 60
    connect: Optional[float] = 20
    write: Optional[float] = 60
