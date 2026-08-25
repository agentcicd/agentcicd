from typing import Optional

from .types import AgentCICDModel


class RetryConfig(AgentCICDModel):
    """
    Configuration for retry behavior with exponential backoff.

    Attributes:
        num_retries: Maximum number of retry attempts (default: 3)
        backoff_exponent: Exponential backoff factor (default: 2.0)
            Wait time = base_delay * (backoff_exponent ** attempt_number)
        max_wait_time: Maximum wait time between retries in seconds (default: 60.0)
    """
    num_retries: Optional[int] = 3
    backoff_exponent: Optional[float] = 1.1
    max_wait_time: Optional[float] = 600.0