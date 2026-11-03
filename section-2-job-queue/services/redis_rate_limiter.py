import time
import uuid
import logging
from typing import Optional, Tuple, NamedTuple
from django.conf import settings
from .rate_limiter_interface import BaseRateLimiter

logger = logging.getLogger(__name__)


class RateLimitResult(NamedTuple):
    allowed: bool
    retry_after_ms: int
    current_count: int
    reservation_id: str
    error: Optional[str] = None


class RedisRateLimiterUnavailableError(Exception):
    """Raised when Redis is unreachable and the rate limiter fails closed."""
    pass


# Production Redis Lua Script implementing Atomic Sliding Window Rate Limiting
# KEYS[1]: rate limit key (e.g. "email:rate_limit:provider")
# ARGV[1]: window size in milliseconds (e.g. 60000 for 60 seconds)
# ARGV[2]: maximum capacity allowed in window (e.g. 200)
# ARGV[3]: explicit current timestamp in ms (optional; if <= 0 or nil, uses redis.call('TIME'))
# ARGV[4]: unique reservation identifier (UUID)
SLIDING_WINDOW_LUA_SCRIPT = """
local key = KEYS[1]
local window_ms = tonumber(ARGV[1])
local max_limit = tonumber(ARGV[2])
local explicit_now = tonumber(ARGV[3])
local reservation_id = ARGV[4]

-- 1. Determine current timestamp in milliseconds
local now_ms
if explicit_now and explicit_now > 0 then
    now_ms = explicit_now
else
    local redis_time = redis.call('TIME')
    -- redis_time[1] is seconds, redis_time[2] is microseconds
    now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
end

local window_start = now_ms - window_ms

-- 2. Prune entries that have slid outside the rolling window
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- 3. Count remaining active reservations
local current_count = redis.call('ZCARD', key)

-- 4. Evaluate capacity
if current_count < max_limit then
    -- Allowed: record this reservation with score = now_ms
    redis.call('ZADD', key, now_ms, reservation_id)
    -- Set TTL to window + 5s buffer so idle keys clean up automatically
    local ttl_ms = math.ceil(window_ms + 5000)
    redis.call('PEXPIRE', key, ttl_ms)
    return {1, 0, current_count + 1}
else
    -- Denied: find the oldest reservation still in the rolling window
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after_ms = window_ms
    if oldest and #oldest >= 2 then
        local oldest_ts = tonumber(oldest[2])
        retry_after_ms = math.max(1, (oldest_ts + window_ms) - now_ms)
    end
    return {0, retry_after_ms, current_count}
end
"""


class RedisSlidingWindowRateLimiter(BaseRateLimiter):
    """
    Production-grade Atomic Sliding Window Rate Limiter using Redis Sorted Sets and Lua.

    Guarantees:
    - Never exceeds max_limit (200 emails) in ANY rolling window_seconds (60s).
    - 100% atomic: Concurrent workers cannot create race conditions.
    - Clock consistency: Defaults to Redis server TIME in production.
    - Testability: Supports controlled test clock injection.
    - Fail-Closed: Rejects dispatches if Redis is unreachable to protect provider reputation.
    """

    def __init__(self, redis_client=None, default_key: str = "email:rate_limit:provider"):
        self._redis_client = redis_client
        self.default_key = default_key
        self._script_sha: Optional[str] = None

    _shared_fake_redis = None

    def get_redis_client(self):
        """Retrieves or instantiates configured Redis client."""
        if self._redis_client is not None:
            return self._redis_client
        try:
            import redis

            redis_url = getattr(settings, "REDIS_URL", "redis://localhost:6379/0")
            client = redis.Redis.from_url(redis_url, socket_timeout=2.0)
            client.ping()
            self._redis_client = client
            return client
        except Exception as e:
            if getattr(settings, "IS_TESTING", False):
                import fakeredis

                if RedisSlidingWindowRateLimiter._shared_fake_redis is None:
                    RedisSlidingWindowRateLimiter._shared_fake_redis = fakeredis.FakeRedis()
                return RedisSlidingWindowRateLimiter._shared_fake_redis
            logger.error(f"Failed to initialize Redis client: {e}")
            return None

    def set_redis_client(self, client):
        """Allows injecting client (e.g. fakeredis or mock) for testing."""
        self._redis_client = client
        self._script_sha = None

    def acquire_detailed(
        self,
        key: Optional[str] = None,
        max_limit: int = 200,
        window_seconds: int = 60,
        current_time_ms: Optional[int] = None,
        reservation_id: Optional[str] = None,
    ) -> RateLimitResult:
        """
        Executes atomic sliding-window check via Redis Lua script.

        Returns RateLimitResult:
        - allowed: True if reservation succeeded, False if throttled.
        - retry_after_ms: Milliseconds until capacity becomes available.
        - current_count: Current count of active reservations in rolling window.
        - reservation_id: Unique reservation member stored in ZSET.
        """
        limiter_key = key or self.default_key
        window_ms = int(window_seconds * 1000)
        res_id = reservation_id or f"res_{uuid.uuid4().hex}"
        explicit_time = int(current_time_ms) if current_time_ms is not None else -1

        client = self.get_redis_client()
        if client is None:
            # FAIL-CLOSED: Cannot confirm reservation, protect provider limits!
            logger.error(
                f"[RateLimiter] Redis client unavailable for key '{limiter_key}'. "
                f"Failing CLOSED to protect email provider limit."
            )
            return RateLimitResult(
                allowed=False,
                retry_after_ms=5000,
                current_count=-1,
                reservation_id=res_id,
                error="Redis client unavailable (Fail-Closed)",
            )

        try:
            # Execute Lua script
            # eval(script, numkeys, *keys, *args)
            raw_result = client.eval(
                SLIDING_WINDOW_LUA_SCRIPT,
                1,
                limiter_key,
                window_ms,
                max_limit,
                explicit_time,
                res_id,
            )

            is_allowed = bool(raw_result[0] == 1)
            retry_after = int(raw_result[1])
            count = int(raw_result[2])

            return RateLimitResult(
                allowed=is_allowed,
                retry_after_ms=retry_after,
                current_count=count,
                reservation_id=res_id,
                error=None,
            )

        except Exception as exc:
            # FAIL-CLOSED on network or execution error
            logger.error(
                f"[RateLimiter] Redis Lua execution failed ({exc}). Failing CLOSED."
            )
            return RateLimitResult(
                allowed=False,
                retry_after_ms=5000,
                current_count=-1,
                reservation_id=res_id,
                error=str(exc),
            )

    def acquire(self, key: str, max_limit: int = 200, window_seconds: int = 60) -> bool:
        """Standard interface implementation returning boolean."""
        res = self.acquire_detailed(key, max_limit, window_seconds)
        return res.allowed
