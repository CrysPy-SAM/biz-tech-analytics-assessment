from unittest.mock import patch, MagicMock
from django.test import TestCase
from email_queue.models import EmailType, EmailDeliveryLog, EmailDeliveryStatus, DeadLetterEmailJob
from services.email_provider import (
    MockEmailBackend,
    TransientEmailProviderError,
    FatalEmailProviderError,
    get_email_provider,
)
from services.idempotency import IdempotencyManager
from tasks import send_transactional_email_task


class SendTransactionalEmailTaskTestCase(TestCase):
    """
    Comprehensive tests for send_transactional_email_task covering:
    - Happy path dispatch across all email types
    - Temporary failure handling and Celery retry invocation
    - Exhaustion of retries and dead-letter routing
    - Immediate DLQ routing for fatal non-retryable errors
    - Idempotent task re-execution
    """

    def setUp(self):
        self.mock_provider = get_email_provider()
        self.mock_provider.reset()
        IdempotencyManager.reset_in_memory_store()

    def test_task_success_order_confirmation(self):
        """Tests successful asynchronous delivery of an order confirmation email."""
        result = send_transactional_email_task.apply(
            kwargs={
                "email_type": EmailType.ORDER_CONFIRMATION,
                "recipient": "buyer@biztech.com",
                "payload": {"order_number": "ORD-7712", "total_amount": 249.50},
                "idempotency_key": "test_order_confirm_001",
            }
        ).get()

        self.assertEqual(result["status"], "SENT")
        self.assertEqual(result["idempotency_key"], "test_order_confirm_001")
        self.assertEqual(len(self.mock_provider.sent_emails), 1)

        # Assert persistent log
        log = EmailDeliveryLog.objects.get(idempotency_key="test_order_confirm_001")
        self.assertEqual(log.status, EmailDeliveryStatus.SENT)
        self.assertEqual(log.attempts, 1)

    def test_task_success_otp_and_transaction_alert(self):
        """Tests OTP and Transaction Alert email types."""
        # 1. OTP
        res_otp = send_transactional_email_task.apply(
            kwargs={
                "email_type": EmailType.OTP,
                "recipient": "user@biztech.com",
                "payload": {"otp_code": "551293", "expiry_minutes": 5},
                "idempotency_key": "test_otp_001",
            }
        ).get()
        self.assertEqual(res_otp["status"], "SENT")

        # 2. Transaction Alert
        res_alert = send_transactional_email_task.apply(
            kwargs={
                "email_type": EmailType.TRANSACTION_ALERT,
                "recipient": "user@biztech.com",
                "payload": {"account_id": "ACC-991", "amount": 850.0},
                "idempotency_key": "test_alert_001",
            }
        ).get()
        self.assertEqual(res_alert["status"], "SENT")
        self.assertEqual(len(self.mock_provider.sent_emails), 2)

    @patch.object(send_transactional_email_task, "retry")
    def test_task_temporary_failure_triggers_retry(self, mock_retry):
        """
        When the email provider encounters a transient error (e.g. 503 or 429),
        the task must call self.retry() with an exponential countdown.
        """
        mock_retry.side_effect = Exception("Celery retry triggered")
        self.mock_provider.simulate_transient_failures(times=1)

        with self.assertRaises(Exception) as ctx:
            send_transactional_email_task.apply(
                kwargs={
                    "email_type": EmailType.OTP,
                    "recipient": "retry_user@biztech.com",
                    "payload": {"otp_code": "112233"},
                    "idempotency_key": "retry_key_001",
                }
            )

        self.assertIn("Celery retry triggered", str(ctx.exception))
        self.assertTrue(mock_retry.called)
        # Verify that countdown was passed to retry
        _, kwargs = mock_retry.call_args
        self.assertIn("countdown", kwargs)
        self.assertGreater(kwargs["countdown"], 0)

    def test_fatal_validation_error_routes_immediately_to_dead_letter(self):
        """
        Non-retryable fatal validation errors (e.g. missing required payload fields)
        must route directly to DeadLetterEmailJob without wasting retries.
        """
        result = send_transactional_email_task.apply(
            kwargs={
                "email_type": EmailType.ORDER_CONFIRMATION,
                "recipient": "invalid_payload@biztech.com",
                "payload": {"invalid": "missing order_number and total_amount"},
                "idempotency_key": "fatal_payload_key",
            }
        ).get()

        self.assertEqual(result["status"], "DEAD_LETTER")
        self.assertIn("requires 'order_number'", result["reason"])

        # Check DLQ model
        dlq_entry = DeadLetterEmailJob.objects.filter(idempotency_key="fatal_payload_key").first()
        self.assertIsNotNone(dlq_entry)
        self.assertEqual(dlq_entry.email_type, EmailType.ORDER_CONFIRMATION)
        self.assertEqual(dlq_entry.retry_count, 0)
        self.assertEqual(dlq_entry.exception_class, "EmailPayloadValidationError")

    def test_exhausted_retries_routes_to_dead_letter(self):
        """
        When retries reaches max_retries, the task stops retrying and commits
        the permanent failure to DeadLetterEmailJob.
        """
        self.mock_provider.simulate_permanent_transient_failure()

        # Simulate task invocation where retries has reached max_retries
        result = send_transactional_email_task(
            email_type=EmailType.OTP,
            recipient="exhausted@biztech.com",
            payload={"otp_code": "999111"},
            idempotency_key="exhausted_key_001",
            provider_retries=5,
        )

        self.assertEqual(result["status"], "DEAD_LETTER")
        self.assertIn("Max retries exceeded", result["reason"])

        # Verify entry in DeadLetterEmailJob
        dlq_entry = DeadLetterEmailJob.objects.filter(idempotency_key="exhausted_key_001").first()
        self.assertIsNotNone(dlq_entry)
        self.assertEqual(dlq_entry.retry_count, 5)
        self.assertIn("Exhausted 5 retries", dlq_entry.failure_reason)

    def test_idempotent_task_execution(self):
        """Submitting the same task multiple times dispatches the email only once."""
        idempotency_key = "idemp_celery_test_42"

        # 1. First run
        res1 = send_transactional_email_task.apply(
            kwargs={
                "email_type": EmailType.TRANSACTION_ALERT,
                "recipient": "idemp_user@biztech.com",
                "payload": {"account_id": "ACC-77", "amount": 100.0},
                "idempotency_key": idempotency_key,
            }
        ).get()
        self.assertEqual(res1["status"], "SENT")
        self.assertEqual(len(self.mock_provider.sent_emails), 1)

        # 2. Second run with same idempotency key
        res2 = send_transactional_email_task.apply(
            kwargs={
                "email_type": EmailType.TRANSACTION_ALERT,
                "recipient": "idemp_user@biztech.com",
                "payload": {"account_id": "ACC-77", "amount": 100.0},
                "idempotency_key": idempotency_key,
            }
        ).get()
        self.assertEqual(res2["status"], "ALREADY_SENT")
        # Ensure email provider was NOT called a second time
        self.assertEqual(len(self.mock_provider.sent_emails), 1)
