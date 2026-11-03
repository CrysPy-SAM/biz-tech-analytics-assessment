from django.db import models


class EmailType(models.TextChoices):
    ORDER_CONFIRMATION = "ORDER_CONFIRMATION", "Order Confirmation"
    OTP = "OTP", "One-Time Password (OTP)"
    TRANSACTION_ALERT = "TRANSACTION_ALERT", "Transaction Alert"


class EmailDeliveryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"
    DEAD_LETTER = "DEAD_LETTER", "Moved to Dead-Letter Queue"


class EmailDeliveryLog(models.Model):
    """
    Persistent audit log for all transactional email jobs.
    Enforces idempotency and tracks lifecycle states.
    """

    task_id = models.CharField(max_length=255, db_index=True)
    idempotency_key = models.CharField(max_length=255, unique=True, db_index=True)
    email_type = models.CharField(max_length=50, choices=EmailType.choices)
    recipient = models.EmailField()
    status = models.CharField(
        max_length=30,
        choices=EmailDeliveryStatus.choices,
        default=EmailDeliveryStatus.PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    provider_message_id = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "email_delivery_logs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.status}] {self.email_type} -> {self.recipient} (Key: {self.idempotency_key})"


class DeadLetterEmailJob(models.Model):
    """
    Dead-Letter Queue (DLQ) storage for permanently failed transactional email jobs.
    Retains complete diagnostics, failure reasons, payload, and retry counts for inspection.
    """

    task_id = models.CharField(max_length=255, db_index=True)
    idempotency_key = models.CharField(max_length=255, db_index=True)
    email_type = models.CharField(max_length=50, choices=EmailType.choices)
    recipient = models.EmailField()
    payload = models.JSONField(default=dict)
    failure_reason = models.TextField()
    exception_class = models.CharField(max_length=255)
    retry_count = models.PositiveIntegerField(default=0)
    traceback = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved = models.BooleanField(default=False)

    class Meta:
        db_table = "dead_letter_email_jobs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"DLQ: {self.email_type} -> {self.recipient} (Retries: {self.retry_count})"
