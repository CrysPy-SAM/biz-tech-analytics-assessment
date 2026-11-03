import os
import uuid
import pytest
from django.test import TestCase
from services.redis_rate_limiter import RedisSlidingWindowRateLimiter


def is_live_redis_available() -> bool:
    """Probes whether a genuine Redis server is reachable on localhost:6379."""
    try:
        import redis

        client = redis.Redis(host="localhost", port=6379, socket_timeout=0.5)
        client.ping()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not is_live_redis_available(),
    reason="Real Redis server on localhost:6379 is offline. Real Redis integration test runs in Docker Compose environment.",
)
class RealRedisIntegrationTestCase(TestCase):
    """
    Genuine live Redis integration test.
    Executes actual Lua script on live Redis daemon (e.g. inside Docker or host Redis).
    """

    def setUp(self):
        import redis

        self.redis_client = redis.Redis(host="localhost", port=6379, socket_timeout=2)
        self.test_key = f"live:test:rate_limit:{uuid.uuid4().hex[:8]}"
        self.limiter = RedisSlidingWindowRateLimiter(
            redis_client=self.redis_client,
            default_key=self.test_key,
        )

    def tearDown(self):
        try:
            self.redis_client.delete(self.test_key)
        except Exception:
            pass

    def test_live_redis_lua_execution(self):
        """Verifies real Redis server executes the Lua script atomically."""
        # 1. First request allowed
        res1 = self.limiter.acquire_detailed(max_limit=2, window_seconds=60)
        self.assertTrue(res1.allowed)
        self.assertEqual(res1.current_count, 1)

        # 2. Second request allowed
        res2 = self.limiter.acquire_detailed(max_limit=2, window_seconds=60)
        self.assertTrue(res2.allowed)
        self.assertEqual(res2.current_count, 2)

        # 3. Third request denied (capacity = 2 exceeded)
        res3 = self.limiter.acquire_detailed(max_limit=2, window_seconds=60)
        self.assertFalse(res3.allowed)
        self.assertEqual(res3.current_count, 2)
        self.assertGreater(res3.retry_after_ms, 0)
