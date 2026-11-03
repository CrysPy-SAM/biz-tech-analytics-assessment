import re
import logging
from typing import Dict, Any, Tuple
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from email_queue.models import EmailType, EmailDeliveryLog, EmailDeliveryStatus
from .email_provider import (
    BaseEmailProvider,
    ProviderResponse,
    FatalEmailProviderError,
    TransientEmailProviderError,
    get_email_provider,
)
from .idempotency import IdempotencyManager

logger = logging.getLogger(__name__)


class EmailPayloadValidationError(FatalEmailProviderError):
    """Raised when an email payload fails structural or semantic validation."""
    pass


class EmailService:
    """
    Core domain service that validates transactional email payloads,
    formats message templates, enforces idempotency, and delegates to the provider.
    Keeps business logic strictly decoupled from Celery task definitions.
    """

    @classmethod
    def validate_payload(cls, email_type: str, recipient: str, payload: Dict[str, Any]):
        """Validates recipient format, email type, and type-specific payload attributes."""
        # 1. Validate email syntax
        try:
            validate_email(recipient)
        except ValidationError as e:
            raise EmailPayloadValidationError(f"Invalid recipient email '{recipient}': {e}") from e

        # 2. Validate email type
        if email_type not in EmailType.values:
            raise EmailPayloadValidationError(
                f"Unsupported email_type '{email_type}'. Allowed types: {EmailType.values}"
            )

        # 3. Validate type-specific required fields
        if email_type == EmailType.ORDER_CONFIRMATION:
            if "order_number" not in payload or "total_amount" not in payload:
                raise EmailPayloadValidationError(
                    "ORDER_CONFIRMATION requires 'order_number' and 'total_amount' in payload"
                )
        elif email_type == EmailType.OTP:
            if "otp_code" not in payload:
                raise EmailPayloadValidationError("OTP requires 'otp_code' in payload")
        elif email_type == EmailType.TRANSACTION_ALERT:
            if "account_id" not in payload or "amount" not in payload:
                raise EmailPayloadValidationError(
                    "TRANSACTION_ALERT requires 'account_id' and 'amount' in payload"
                )

    @classmethod
    def build_email_content(
        cls, email_type: str, recipient: str, payload: Dict[str, Any]
    ) -> Tuple[str, str]:
        """Constructs subject line and body text based on email template."""
        if email_type == EmailType.ORDER_CONFIRMATION:
            subject = f"Order Confirmation: #{payload.get('order_number')}"
            body = (
                f"Hello,\n\nYour order #{payload.get('order_number')} for "
                f"${payload.get('total_amount')} has been confirmed.\n\n"
                f"Items: {payload.get('items_count', 'N/A')}\n"
                f"Thank you for shopping with Biz-Tech Analytics!"
            )
        elif email_type == EmailType.OTP:
            subject = "Your One-Time Password (OTP)"
            body = (
                f"Your verification code is: {payload.get('otp_code')}\n"
                f"This code will expire in {payload.get('expiry_minutes', 5)} minutes.\n"
                f"Do not share this code with anyone."
            )
        elif email_type == EmailType.TRANSACTION_ALERT:
            subject = f"Security Alert: Transaction of ${payload.get('amount')}"
            body = (
                f"A transaction of ${payload.get('amount')} was processed on "
                f"account {payload.get('account_id')}.\n"
                f"If you did not authorize this, please contact support immediately."
            )
        else:
            subject = "Biz-Tech Analytics Notification"
            body = f"Notification: {payload}"

        return subject, body

    @classmethod
    def process_and_send(
        cls,
        task_id: str,
        idempotency_key: str,
        email_type: str,
        recipient: str,
        payload: Dict[str, Any],
        provider: BaseEmailProvider = None,
    ) -> Dict[str, Any]:
        """
        Executes the transactional email delivery workflow with full audit logging and idempotency.
        """
        if provider is None:
            provider = get_email_provider()

        # 1. Idempotency verification: Has this email already been successfully dispatched?
        if IdempotencyManager.is_already_sent(idempotency_key):
            logger.info(
                f"[IDEMPOTENCY] Email for key '{idempotency_key}' already sent. Skipping transmission."
            )
            existing_log = EmailDeliveryLog.objects.get(idempotency_key=idempotency_key)
            return {
                "status": "ALREADY_SENT",
                "idempotency_key": idempotency_key,
                "provider_message_id": existing_log.provider_message_id,
            }

        # 2. Strict payload validation
        cls.validate_payload(email_type, recipient, payload)

        # 3. Create or update persistent audit log in PROCESSING state
        delivery_log, _ = EmailDeliveryLog.objects.update_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "task_id": task_id,
                "email_type": email_type,
                "recipient": recipient,
                "status": EmailDeliveryStatus.PROCESSING,
            },
        )
        delivery_log.attempts += 1
        delivery_log.save(update_fields=["attempts", "status", "updated_at"])

        # 4. Format template
        subject, body = cls.build_email_content(email_type, recipient, payload)

        # 5. Dispatch via email provider (may raise Transient or Fatal errors)
        response: ProviderResponse = provider.send(
            recipient=recipient,
            subject=subject,
            body=body,
            metadata={"idempotency_key": idempotency_key, "task_id": task_id},
        )

        # 6. Mark delivery as successfully SENT
        delivery_log.status = EmailDeliveryStatus.SENT
        delivery_log.provider_message_id = response.message_id
        delivery_log.save(update_fields=["status", "provider_message_id", "updated_at"])

        logger.info(
            f"Successfully dispatched {email_type} to {recipient} "
            f"(msg_id={response.message_id}, key={idempotency_key})"
        )

        return {
            "status": "SENT",
            "idempotency_key": idempotency_key,
            "provider_message_id": response.message_id,
        }
