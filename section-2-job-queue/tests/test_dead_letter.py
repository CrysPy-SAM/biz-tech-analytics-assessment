from django.test import TestCase
from email_queue.models import DeadLetterEmailJob, EmailType, EmailDeliveryLog, EmailDeliveryStatus
from services.dead_letter import DeadLetterService


class DeadLetterTestCase(TestCase):
    """Verifies that permanently failed jobs are captured in the dead-letter mechanism."""

    def test_record_permanent_failure_captures_all_required_metadata(self):
        """
        Assessment requirement:
        Store useful failure information:
        - Task ID
        - Email type
        - Recipient
        - Failure reason
        - Retry count
        - Timestamp
        """
        # Create initial pending delivery log
        EmailDeliveryLog.objects.create(
            task_id="task_fail_101",
            idempotency_key="key_fail_101",
            email_type=EmailType.OTP,
            recipient="permanently_failing@biztech.com",
            status=EmailDeliveryStatus.PROCESSING,
            attempts=5,
        )

        dlq_entry = DeadLetterService.record_permanent_failure(
            task_id="task_fail_101",
            idempotency_key="key_fail_101",
            email_type=EmailType.OTP,
            recipient="permanently_failing@biztech.com",
            payload={"otp_code": "999888"},
            failure_reason="Exhausted maximum 5 retries: 503 Service Unavailable",
            exception_class="TransientEmailProviderError",
            retry_count=5,
            traceback_str="Traceback (most recent call last)...",
        )

        # Assert DLQ model persistence
        self.assertIsNotNone(dlq_entry.id)
        self.assertEqual(dlq_entry.task_id, "task_fail_101")
        self.assertEqual(dlq_entry.email_type, EmailType.OTP)
        self.assertEqual(dlq_entry.recipient, "permanently_failing@biztech.com")
        self.assertEqual(dlq_entry.retry_count, 5)
        self.assertIn("Exhausted maximum 5 retries", dlq_entry.failure_reason)
        self.assertEqual(dlq_entry.exception_class, "TransientEmailProviderError")
        self.assertIsNotNone(dlq_entry.created_at)

        # Assert delivery log updated to DEAD_LETTER
        log = EmailDeliveryLog.objects.get(idempotency_key="key_fail_101")
        self.assertEqual(log.status, EmailDeliveryStatus.DEAD_LETTER)
