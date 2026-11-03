import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BaseRateLimiter(ABC):
    """
    Abstract interface for rate limiters.
    Prepares a clean, decoupled boundary for Part 3 Redis sliding-window implementation.
    """

    @abstractmethod
    def acquire(self, key: str, max_limit: int = 200, window_seconds: int = 60) -> bool:
        """
        Attempts to acquire capacity within the rolling window.
        Returns True if capacity was acquired, False if throttled.
        """
        raise NotImplementedError


class PassThroughRateLimiter(BaseRateLimiter):
    """
    Default pass-through implementation used during Part 2.
    The complete Redis atomic sliding-window rate limiter will be integrated in Part 3.
    """

    def acquire(self, key: str, max_limit: int = 200, window_seconds: int = 60) -> bool:
        return True


# Default instance
_active_rate_limiter: BaseRateLimiter = PassThroughRateLimiter()


def get_rate_limiter() -> BaseRateLimiter:
    """Returns the currently active rate limiter."""
    return _active_rate_limiter


def set_rate_limiter(limiter: BaseRateLimiter):
    """Allows injecting specialized rate limiters (e.g. during Part 3)."""
    global _active_rate_limiter
    _active_rate_limiter = limiter
