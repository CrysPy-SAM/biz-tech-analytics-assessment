from django.test import TestCase
from config.celery import app as celery_app
from tasks import (
    send_transactional_email_task,
    calculate_exponential_backoff,
    BASE_RETRY_DELAY,
    MAX_RETRY_DELAY,
)


class WorkerReliabilityConfigurationTestCase(TestCase):
    """
    Verifies that the Celery application and tasks are configured strictly
    for worker crash resilience (SIGKILL), task re-queuing, and exponential backoff.
    """

    def test_celery_app_worker_crash_configuration(self):
        """
        Validates Celery application-level guarantees:
        1. task_acks_late: ACK sent only after task completion (prevents task loss on SIGKILL).
        2. task_reject_on_worker_lost: Un-ACKed tasks re-queued when child worker dies unexpectedly.
        3. worker_prefetch_multiplier: Prevents task hoarding during burst traffic.
        4. visibility_timeout: Redis unacknowledged recovery window.
        """
        conf = celery_app.conf

        self.assertTrue(
            conf.task_acks_late,
            "task_acks_late must be True to prevent job loss when a worker receives SIGKILL",
        )
        self.assertTrue(
            conf.task_reject_on_worker_lost,
            "task_reject_on_worker_lost must be True so dead child processes release tasks back to queue",
        )
        self.assertEqual(
            conf.worker_prefetch_multiplier,
            1,
            "worker_prefetch_multiplier must be 1 to prevent worker starvation and task hoarding",
        )
        self.assertEqual(
            conf.broker_transport_options.get("visibility_timeout"),
            3600,
            "Redis visibility_timeout must be configured (3600s)",
        )

    def test_email_task_reliability_attributes(self):
        """Validates that send_transactional_email_task inherits crash-resilient attributes."""
        self.assertTrue(send_transactional_email_task.acks_late)
        self.assertTrue(send_transactional_email_task.reject_on_worker_lost)
        self.assertGreaterEqual(send_transactional_email_task.max_retries, 3)

    def test_exponential_backoff_and_jitter_calculation(self):
        """
        Validates exponential backoff progression:
        Attempt 0: ~2s
        Attempt 1: ~4s
        Attempt 2: ~8s
        Attempt 3: ~16s
        Never exceeds MAX_RETRY_DELAY (300s).
        """
        delays_attempt_0 = [calculate_exponential_backoff(0) for _ in range(5)]
        delays_attempt_1 = [calculate_exponential_backoff(1) for _ in range(5)]
        delays_attempt_2 = [calculate_exponential_backoff(2) for _ in range(5)]
        delays_attempt_3 = [calculate_exponential_backoff(3) for _ in range(5)]

        # Verify increasing averages
        avg_0 = sum(delays_attempt_0) / 5
        avg_1 = sum(delays_attempt_1) / 5
        avg_2 = sum(delays_attempt_2) / 5
        avg_3 = sum(delays_attempt_3) / 5

        self.assertTrue(avg_0 < avg_1 < avg_2 < avg_3)
        self.assertAlmostEqual(avg_0, 2.5, delta=1.0)
        self.assertAlmostEqual(avg_1, 4.5, delta=1.0)
        self.assertAlmostEqual(avg_2, 8.5, delta=1.0)
        self.assertAlmostEqual(avg_3, 16.5, delta=1.0)

        # Verify jitter exists (not all delays in the same attempt are identical)
        self.assertNotEqual(delays_attempt_0[0], delays_attempt_0[1])

        # Verify ceiling clamp
        high_retry_delay = calculate_exponential_backoff(20)
        self.assertEqual(high_retry_delay, MAX_RETRY_DELAY)
