import pytest
from django.test import TestCase
from email_queue.models import EmailType, EmailDeliveryLog, EmailDeliveryStatus
from services.email_service import EmailService, EmailPayloadValidationError
from services.email_provider import (
    MockEmailBackend,
    TransientEmailProviderError,
    FatalEmailProviderError,
)


class EmailServiceTestCase(TestCase):
    """Unit tests for EmailService business logic, payload validation, and templating."""

    def setUp(self):
        self.mock_provider = MockEmailBackend()
        self.mock_provider.reset()

    def test_validate_payload_valid_order_confirmation(self):
        """Validates proper order confirmation payload structure."""
        EmailService.validate_payload(
            email_type=EmailType.ORDER_CONFIRMATION,
            recipient="customer@biztech.com",
            payload={"order_number": "ORD-12345", "total_amount": 199.99, "items_count": 3},
        )

    def test_validate_payload_invalid_email_syntax(self):
        """Rejects malformed recipient email addresses."""
        with self.assertRaises(EmailPayloadValidationError) as ctx:
            EmailService.validate_payload(
                email_type=EmailType.OTP,
                recipient="not-a-valid-email",
                payload={"otp_code": "123456"},
            )
        self.assertIn("Invalid recipient email", str(ctx.exception))

    def test_validate_payload_missing_required_fields(self):
        """Rejects order confirmation missing order_number or total_amount."""
        with self.assertRaises(EmailPayloadValidationError) as ctx:
            EmailService.validate_payload(
                email_type=EmailType.ORDER_CONFIRMATION,
                recipient="user@biztech.com",
                payload={"items_count": 2},  # Missing order_number & total_amount
            )
        self.assertIn("requires 'order_number'", str(ctx.exception))

    def test_build_email_content_templates(self):
        """Tests that subject and body generation works across all supported email types."""
        # Order confirmation
        subject, body = EmailService.build_email_content(
            EmailType.ORDER_CONFIRMATION,
            "buyer@biztech.com",
            {"order_number": "ORD-99", "total_amount": 50.0},
        )
        self.assertIn("ORD-99", subject)
        self.assertIn("$50.0", body)

        # OTP
        subject, body = EmailService.build_email_content(
            EmailType.OTP,
            "user@biztech.com",
            {"otp_code": "884920", "expiry_minutes": 10},
        )
        self.assertIn("OTP", subject)
        self.assertIn("884920", body)
        self.assertIn("10 minutes", body)

        # Transaction alert
        subject, body = EmailService.build_email_content(
            EmailType.TRANSACTION_ALERT,
            "user@biztech.com",
            {"account_id": "ACC-552", "amount": 1250.0},
        )
        self.assertIn("Transaction of $1250.0", subject)
        self.assertIn("ACC-552", body)

    def test_process_and_send_success(self):
        """Verifies successful delivery updates delivery log to SENT."""
        result = EmailService.process_and_send(
            task_id="task_001",
            idempotency_key="key_001",
            email_type=EmailType.OTP,
            recipient="alice@biztech.com",
            payload={"otp_code": "443219"},
            provider=self.mock_provider,
        )

        self.assertEqual(result["status"], "SENT")
        self.assertEqual(len(self.mock_provider.sent_emails), 1)

        log = EmailDeliveryLog.objects.get(idempotency_key="key_001")
        self.assertEqual(log.status, EmailDeliveryStatus.SENT)
        self.assertEqual(log.attempts, 1)
        self.assertTrue(log.provider_message_id.startswith("msg_"))
