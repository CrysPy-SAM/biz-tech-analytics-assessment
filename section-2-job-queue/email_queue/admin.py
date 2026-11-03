from django.contrib import admin
from .models import EmailDeliveryLog, DeadLetterEmailJob


@admin.register(EmailDeliveryLog)
class EmailDeliveryLogAdmin(admin.ModelAdmin):
    list_display = (
        "idempotency_key",
        "email_type",
        "recipient",
        "status",
        "attempts",
        "created_at",
    )
    list_filter = ("status", "email_type", "created_at")
    search_fields = ("idempotency_key", "recipient", "task_id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(DeadLetterEmailJob)
class DeadLetterEmailJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "email_type",
        "recipient",
        "retry_count",
        "exception_class",
        "resolved",
        "created_at",
    )
    list_filter = ("email_type", "resolved", "created_at")
    search_fields = ("idempotency_key", "recipient", "task_id", "failure_reason")
    readonly_fields = ("created_at",)
