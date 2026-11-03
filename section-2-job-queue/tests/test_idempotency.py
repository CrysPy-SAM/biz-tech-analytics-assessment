from django.test import TestCase
from email_queue.models import EmailType, EmailDeliveryLog, EmailDeliveryStatus
from services.email_service import EmailService
from services.email_provider import MockEmailBackend
from services.idempotency import IdempotencyManager


class IdempotencyTestCase(TestCase):
    """Verifies that duplicated tasks do not generate duplicate external emails."""

    def setUp(self):
        self.mock_provider = MockEmailBackend()
        self.mock_provider.reset()
        IdempotencyManager.reset_in_memory_store()

    def test_duplicate_delivery_prevented_by_idempotency(self):
        """
        Simulates worker re-delivery of an already-sent email job.
        Asserts that the provider is called exactly once.
        """
        idempotency_key = "idemp_test_999"

        # 1. First execution
        res1 = EmailService.process_and_send(
            task_id="task_original",
            idempotency_key=idempotency_key,
            email_type=EmailType.ORDER_CONFIRMATION,
            recipient="buyer@biztech.com",
            payload={"order_number": "ORD-500", "total_amount": 79.99},
            provider=self.mock_provider,
        )
        self.assertEqual(res1["status"], "SENT")
        self.assertEqual(len(self.mock_provider.sent_emails), 1)

        # 2. Second execution (re-delivery due to crash or duplicate trigger)
        res2 = EmailService.process_and_send(
            task_id="task_redelivered_after_sigkill",
            idempotency_key=idempotency_key,
            email_type=EmailType.ORDER_CONFIRMATION,
            recipient="buyer@biztech.com",
            payload={"order_number": "ORD-500", "total_amount": 79.99},
            provider=self.mock_provider,
        )
        self.assertEqual(res2["status"], "ALREADY_SENT")
        # Critical assertion: Provider sent_emails count must remain 1!
        self.assertEqual(len(self.mock_provider.sent_emails), 1)

    def test_idempotency_lock_acquisition_and_release(self):
        """Verifies acquisition and release of task locks."""
        key = "lock_test_key"
        acquired = IdempotencyManager.acquire_lock(key, "task_1")
        self.assertTrue(acquired)

        # Duplicate acquire while in flight fails
        acquired_again = IdempotencyManager.acquire_lock(key, "task_2")
        self.assertFalse(acquired_again)

        # Release lock allows subsequent acquisition
        IdempotencyManager.release_lock(key)
        acquired_third = IdempotencyManager.acquire_lock(key, "task_3")
        self.assertTrue(acquired_third)
