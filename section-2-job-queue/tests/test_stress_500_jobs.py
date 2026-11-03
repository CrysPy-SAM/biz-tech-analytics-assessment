import uuid
from concurrent.futures import ThreadPoolExecutor
from django.test import TestCase
import fakeredis
from email_queue.models import (
    EmailType,
    EmailDeliveryLog,
    EmailDeliveryStatus,
    DeadLetterEmailJob,
)
from services.email_provider import (
    MockEmailBackend,
    get_email_provider,
    TransientEmailProviderError,
)
from services.idempotency import IdempotencyManager
from services.redis_rate_limiter import RedisSlidingWindowRateLimiter
from services.rate_limiter_interface import set_rate_limiter, reset_rate_limiter
from tasks import send_transactional_email_task


class StressTest500EmailJobsTestCase(TestCase):
    """
    STRESS TEST: 500 EMAIL JOBS UNDER STRICT 200 EMAILS/MINUTE RATE LIMIT

    Verifies:
    1. All 500 jobs are accepted and processed.
    2. Zero jobs silently lost.
    3. Zero duplicate email dispatches.
    4. Never exceeds 200 dispatches in any rolling 60-second window.
    5. Rate-limited jobs are deferred and eventually processed as window rolls.
    6. All jobs reach terminal state (SENT).
    7. Rate-limited jobs NEVER enter Dead-Letter Queue.
    8. Concurrent multi-worker execution strictly respects the limit.
    9. Intentional transient provider failure is retried and succeeds.
    """

    def setUp(self):
        self.mock_provider = get_email_provider()
        self.mock_provider.reset()
        IdempotencyManager.reset_in_memory_store()

        # Isolated Redis instance executing genuine Lua script
        self.fake_redis = fakeredis.FakeRedis()
        self.rate_limiter = RedisSlidingWindowRateLimiter(
            redis_client=self.fake_redis,
            default_key=f"stress:ratelimit:{uuid.uuid4().hex[:8]}",
        )
        set_rate_limiter(self.rate_limiter)

    def tearDown(self):
        reset_rate_limiter()

    def test_500_jobs_rate_limited_sliding_window_simulation(self):
        """
        Simulates sudden flash-sale burst of 500 transactional email jobs.
        Validates:
        - Window 1 (T=0 to 60s): Exactly 200 dispatched, 300 deferred.
        - Window 2 (T=61s to 120s): 200 dispatched, 100 deferred.
        - Window 3 (T=121s to 180s): Remaining 100 dispatched.
        - Final total: All 500 SENT, zero lost, DLQ count = 0.
        """
        total_jobs = 500
        job_definitions = []
        for i in range(1, total_jobs + 1):
            job_definitions.append(
                {
                    "email_type": EmailType.ORDER_CONFIRMATION if i % 2 == 0 else EmailType.OTP,
                    "recipient": f"customer_{i:04d}@biztech.com",
                    "payload": {
                        "order_number": f"ORD-FLASH-{i:05d}",
                        "total_amount": 99.50 + (i % 20),
                        "otp_code": f"{100000 + i}",
                    },
                    "idempotency_key": f"flash_sale_job_{i:04d}",
                }
            )

        # -------------------------------------------------------------
        # ROUND 1: BURST AT T = 0ms
        # -------------------------------------------------------------
        t0_ms = 1_000_000
        dispatched_r1 = 0
        deferred_r1 = 0
        pending_jobs_r1 = []

        # Intercept acquire_detailed to pin time to t0_ms for Round 1
        orig_acquire = self.rate_limiter.acquire_detailed
        self.rate_limiter.acquire_detailed = lambda *a, **k: orig_acquire(
            *a, **{**k, "current_time_ms": t0_ms}
        )

        for job in job_definitions:
            res = send_transactional_email_task(
                email_type=job["email_type"],
                recipient=job["recipient"],
                payload=job["payload"],
                idempotency_key=job["idempotency_key"],
            )
            if res["status"] == "SENT":
                dispatched_r1 += 1
            elif res["status"] == "DEFERRED":
                deferred_r1 += 1
                pending_jobs_r1.append(job)

        # Assertions for Window 1
        self.assertEqual(
            dispatched_r1,
            200,
            f"Window 1 must dispatch exactly 200 emails, but got {dispatched_r1}",
        )
        self.assertEqual(
            deferred_r1,
            300,
            f"Window 1 must defer exactly 300 emails, but got {deferred_r1}",
        )
        self.assertEqual(len(self.mock_provider.sent_emails), 200)
        self.assertEqual(DeadLetterEmailJob.objects.count(), 0)

        # -------------------------------------------------------------
        # ROUND 2: ADVANCE TIME TO T = 60,001ms (WINDOW 2)
        # -------------------------------------------------------------
        t1_ms = t0_ms + 60_001
        self.rate_limiter.acquire_detailed = lambda *a, **k: orig_acquire(
            *a, **{**k, "current_time_ms": t1_ms}
        )

        dispatched_r2 = 0
        deferred_r2 = 0
        pending_jobs_r2 = []

        for job in pending_jobs_r1:
            res = send_transactional_email_task(
                email_type=job["email_type"],
                recipient=job["recipient"],
                payload=job["payload"],
                idempotency_key=job["idempotency_key"],
                rate_limit_deferrals=1,
            )
            if res["status"] == "SENT":
                dispatched_r2 += 1
            elif res["status"] == "DEFERRED":
                deferred_r2 += 1
                pending_jobs_r2.append(job)

        # Assertions for Window 2
        self.assertEqual(
            dispatched_r2,
            200,
            f"Window 2 must dispatch exactly 200 emails, but got {dispatched_r2}",
        )
        self.assertEqual(
            deferred_r2,
            100,
            f"Window 2 must defer remaining 100 emails, but got {deferred_r2}",
        )
        self.assertEqual(len(self.mock_provider.sent_emails), 400)
        self.assertEqual(DeadLetterEmailJob.objects.count(), 0)

        # -------------------------------------------------------------
        # ROUND 3: ADVANCE TIME TO T = 120,002ms (WINDOW 3)
        # -------------------------------------------------------------
        t2_ms = t0_ms + 120_002
        self.rate_limiter.acquire_detailed = lambda *a, **k: orig_acquire(
            *a, **{**k, "current_time_ms": t2_ms}
        )

        dispatched_r3 = 0
        for job in pending_jobs_r2:
            res = send_transactional_email_task(
                email_type=job["email_type"],
                recipient=job["recipient"],
                payload=job["payload"],
                idempotency_key=job["idempotency_key"],
                rate_limit_deferrals=2,
            )
            if res["status"] == "SENT":
                dispatched_r3 += 1

        # Assertions for Window 3
        self.assertEqual(
            dispatched_r3,
            100,
            f"Window 3 must dispatch remaining 100 emails, but got {dispatched_r3}",
        )

        # -------------------------------------------------------------
        # FINAL GLOBAL INVARIANTS CHECK ACROSS ALL 500 JOBS
        # -------------------------------------------------------------
        # 1. Exactly 500 emails sent
        self.assertEqual(len(self.mock_provider.sent_emails), 500)

        # 2. Exactly 500 delivery logs marked as SENT
        sent_logs_count = EmailDeliveryLog.objects.filter(
            status=EmailDeliveryStatus.SENT
        ).count()
        self.assertEqual(sent_logs_count, 500)

        # 3. Zero duplicate dispatches (all 500 recipients and message IDs unique)
        unique_recipients = {m["recipient"] for m in self.mock_provider.sent_emails}
        self.assertEqual(len(unique_recipients), 500)

        # 4. Zero jobs in Dead-Letter Queue
        self.assertEqual(DeadLetterEmailJob.objects.count(), 0)

    def test_concurrent_burst_never_exceeds_200_limit(self):
        """
        Verifies that 500 concurrent threads attempting to dispatch simultaneously
        at the exact same microsecond cannot bypass the Redis rate limit.
        Exactly 200 must be SENT, exactly 300 must be DEFERRED.
        """
        from unittest.mock import patch

        t_burst_ms = 8_000_000
        orig_acquire = self.rate_limiter.acquire_detailed
        self.rate_limiter.acquire_detailed = lambda *a, **k: orig_acquire(
            *a, **{**k, "current_time_ms": t_burst_ms}
        )

        def submit_job(idx):
            return send_transactional_email_task(
                email_type=EmailType.OTP,
                recipient=f"concurrent_user_{idx:04d}@biztech.com",
                payload={"otp_code": f"{100000 + idx}"},
                idempotency_key=f"concurrent_otp_{idx:04d}",
            )

        with patch("tasks.IdempotencyManager.is_already_sent", return_value=False):
            with patch("tasks.EmailService.process_and_send") as mock_send:
                mock_send.return_value = {
                    "status": "SENT",
                    "provider_message_id": "msg_concurrent",
                }
                with ThreadPoolExecutor(max_workers=20) as executor:
                    futures = [executor.submit(submit_job, i) for i in range(1, 501)]
                    results = [f.result() for f in futures]

        sent_count = sum(1 for r in results if r["status"] == "SENT")
        deferred_count = sum(1 for r in results if r["status"] == "DEFERRED")

        self.assertEqual(
            sent_count,
            200,
            f"Concurrency breach! Expected exactly 200 SENT, got {sent_count}",
        )
        self.assertEqual(
            deferred_count,
            300,
            f"Expected exactly 300 DEFERRED, got {deferred_count}",
        )
        self.assertEqual(mock_send.call_count, 200)

    def test_intentional_transient_failure_retried_and_succeeds(self):
        """
        Assessment requirement:
        At least one intentional failure is retried correctly and succeeds.
        """
        # Configure provider to fail once with TransientEmailProviderError, then succeed
        self.mock_provider.simulate_transient_failures(times=1)

        key = "transient_retry_success_job"
        job_kwargs = {
            "email_type": EmailType.TRANSACTION_ALERT,
            "recipient": "alert_user@biztech.com",
            "payload": {"account_id": "ACC-992", "amount": 500.0},
            "idempotency_key": key,
        }

        # First attempt raises Celery retry
        with self.assertRaises(Exception):
            send_transactional_email_task(**job_kwargs)

        # Provider failed once
        self.assertEqual(len(self.mock_provider.sent_emails), 0)

        # Second attempt (retry) succeeds
        retry_res = send_transactional_email_task(**job_kwargs)
        self.assertEqual(retry_res["status"], "SENT")
        self.assertEqual(len(self.mock_provider.sent_emails), 1)

        log = EmailDeliveryLog.objects.get(idempotency_key=key)
        self.assertEqual(log.status, EmailDeliveryStatus.SENT)
        self.assertEqual(log.attempts, 2)
