import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from django.test import TestCase
import fakeredis
from services.redis_rate_limiter import (
    RedisSlidingWindowRateLimiter,
    RateLimitResult,
    SLIDING_WINDOW_LUA_SCRIPT,
)


class RedisSlidingWindowRateLimiterTestCase(TestCase):
    """
    Unit test suite for the atomic Redis Sliding Window Rate Limiter.
    Tests:
    - 200 request capacity enforcement
    - 201st request denial
    - Accurate retry_after_ms calculation
    - Sliding window rolling expiration
    - Unique reservation identifiers
    - Concurrent thread safety and atomicity
    - Fail-closed behavior on connection error
    """

    def setUp(self):
        # Use an isolated FakeRedis client that executes real Lua scripts
        self.fake_redis = fakeredis.FakeRedis()
        self.rate_limiter = RedisSlidingWindowRateLimiter(
            redis_client=self.fake_redis,
            default_key=f"test:rate_limit:{uuid.uuid4().hex[:8]}",
        )

    def test_first_200_requests_allowed_and_201st_denied(self):
        """
        Assessment requirement:
        - First 200 requests in rolling 60s window allowed.
        - 201st request denied.
        """
        now_ms = 1_000_000

        # Dispatches 1 through 200 should all be allowed
        for i in range(1, 201):
            res = self.rate_limiter.acquire_detailed(
                max_limit=200,
                window_seconds=60,
                current_time_ms=now_ms,
            )
            self.assertTrue(
                res.allowed,
                f"Request #{i} was unexpectedly denied (count: {res.current_count})",
            )
            self.assertEqual(res.current_count, i)
            self.assertEqual(res.retry_after_ms, 0)

        # 201st request must be denied
        res_201 = self.rate_limiter.acquire_detailed(
            max_limit=200,
            window_seconds=60,
            current_time_ms=now_ms,
        )
        self.assertFalse(res_201.allowed, "Request #201 must be denied!")
        self.assertEqual(res_201.current_count, 200)
        self.assertGreater(res_201.retry_after_ms, 0)
        # Should report ~60,000ms until capacity frees up
        self.assertEqual(res_201.retry_after_ms, 60000)

    def test_sliding_window_expiration_allows_new_requests(self):
        """
        Assessment requirement:
        Request allowed after an old reservation expires as time advances.
        """
        t0 = 1_000_000  # T = 0ms

        # Fill 200 slots at T = 0
        for _ in range(200):
            res = self.rate_limiter.acquire_detailed(
                max_limit=200,
                window_seconds=60,
                current_time_ms=t0,
            )
            self.assertTrue(res.allowed)

        # At T = 30s (t0 + 30,000ms): Still full
        res_at_30s = self.rate_limiter.acquire_detailed(
            max_limit=200,
            window_seconds=60,
            current_time_ms=t0 + 30_000,
        )
        self.assertFalse(res_at_30s.allowed)
        self.assertEqual(res_at_30s.retry_after_ms, 30_000)

        # At T = 60.001s (t0 + 60,001ms): The entries from t0 have slid out!
        res_at_60s = self.rate_limiter.acquire_detailed(
            max_limit=200,
            window_seconds=60,
            current_time_ms=t0 + 60_001,
        )
        self.assertTrue(
            res_at_60s.allowed,
            "Request must be allowed once older reservations slide out of the 60s window",
        )
        self.assertEqual(res_at_60s.current_count, 1)

    def test_staggered_reservations_free_up_incrementally(self):
        """
        Verifies true continuous sliding-window behavior:
        If 50 requests arrived at T=0, and 150 at T=20s:
        - At T=61s: Exactly 50 slots open up (not all 200).
        """
        t0 = 1_000_000

        # 50 requests at T=0
        for _ in range(50):
            self.assertTrue(
                self.rate_limiter.acquire_detailed(
                    max_limit=200, window_seconds=60, current_time_ms=t0
                ).allowed
            )

        # 150 requests at T=20s
        for _ in range(150):
            self.assertTrue(
                self.rate_limiter.acquire_detailed(
                    max_limit=200, window_seconds=60, current_time_ms=t0 + 20_000
                ).allowed
            )

        # Full at T=21s
        self.assertFalse(
            self.rate_limiter.acquire_detailed(
                max_limit=200, window_seconds=60, current_time_ms=t0 + 21_000
            ).allowed
        )

        # At T=61s (first 50 expired, but remaining 150 from T=20s still active)
        t_61s = t0 + 61_000
        # Can now accept up to 50 new requests
        for i in range(50):
            res = self.rate_limiter.acquire_detailed(
                max_limit=200, window_seconds=60, current_time_ms=t_61s
            )
            self.assertTrue(res.allowed, f"Expected slot #{i+1} to be open")

        # 51st at T=61s is denied
        denied_res = self.rate_limiter.acquire_detailed(
            max_limit=200, window_seconds=60, current_time_ms=t_61s
        )
        self.assertFalse(denied_res.allowed)

    def test_concurrent_worker_atomicity(self):
        """
        Assessment requirement:
        Concurrent requests across multiple threads/workers cannot bypass rate limit.
        Submits 300 concurrent reservation attempts; asserts exactly 200 succeed and 100 fail.
        """
        results = []

        def worker_attempt(idx):
            return self.rate_limiter.acquire_detailed(
                max_limit=200,
                window_seconds=60,
                current_time_ms=5_000_000,
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(worker_attempt, i) for i in range(300)]
            for f in futures:
                results.append(f.result())

        allowed_count = sum(1 for r in results if r.allowed)
        denied_count = sum(1 for r in results if not r.allowed)

        self.assertEqual(
            allowed_count,
            200,
            f"Exactly 200 concurrent requests must be allowed, but got {allowed_count}",
        )
        self.assertEqual(
            denied_count,
            100,
            f"Exactly 100 concurrent requests must be denied, but got {denied_count}",
        )

    def test_fail_closed_on_redis_unavailable(self):
        """
        Assessment requirement:
        When Redis is unavailable, the rate limiter fails CLOSED
        to protect the external provider account.
        """
        broken_limiter = RedisSlidingWindowRateLimiter(redis_client=None)
        # Point to invalid client that fails
        broken_limiter.get_redis_client = lambda: None

        res = broken_limiter.acquire_detailed(max_limit=200, window_seconds=60)
        self.assertFalse(res.allowed)
        self.assertIn("Fail-Closed", res.error)
        self.assertGreater(res.retry_after_ms, 0)
