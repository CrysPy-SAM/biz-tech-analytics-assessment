import logging
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)


class BaseRateLimiter(ABC):
    """
    Abstract interface for rate limiters.
    Defines the contract for sliding-window capacity reservation.
    """

    @abstractmethod
    def acquire(self, key: str, max_limit: int = 200, window_seconds: int = 60) -> bool:
        """
        Attempts to acquire capacity within the rolling window.
        Returns True if capacity was acquired, False if throttled.
        """
        raise NotImplementedError


class PassThroughRateLimiter(BaseRateLimiter):
    """Pass-through implementation for test bypass when required."""

    def acquire(self, key: str, max_limit: int = 200, window_seconds: int = 60) -> bool:
        return True


_active_rate_limiter: Optional[BaseRateLimiter] = None


def get_rate_limiter() -> BaseRateLimiter:
    """Returns the currently active rate limiter, lazily instantiating default."""
    global _active_rate_limiter
    if _active_rate_limiter is None:
        from .redis_rate_limiter import RedisSlidingWindowRateLimiter
        _active_rate_limiter = RedisSlidingWindowRateLimiter()
    return _active_rate_limiter


def set_rate_limiter(limiter: BaseRateLimiter):
    """Allows injecting specialized or mocked rate limiters for testing."""
    global _active_rate_limiter
    _active_rate_limiter = limiter


def reset_rate_limiter():
    """Resets to default RedisSlidingWindowRateLimiter."""
    global _active_rate_limiter
    _active_rate_limiter = None
