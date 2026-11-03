import json
import logging
from typing import Dict, Any, Optional
from django.utils import timezone
from email_queue.models import DeadLetterEmailJob, EmailDeliveryLog, EmailDeliveryStatus
from .idempotency import IdempotencyManager

logger = logging.getLogger(__name__)


class DeadLetterService:
    """
    Manages capturing, persisting, and routing exhausted or fatal jobs to the Dead-Letter Queue (DLQ).
    Ensures complete forensic auditability and operational triage capability.
    """

    @classmethod
    def record_permanent_failure(
        cls,
        task_id: str,
        idempotency_key: str,
        email_type: str,
        recipient: str,
        payload: Dict[str, Any],
        failure_reason: str,
        exception_class: str,
        retry_count: int,
        traceback_str: str = "",
    ) -> DeadLetterEmailJob:
        """
        Persists a permanently failed task to the relational database
        and pushes to the Redis DLQ buffer.
        """
        logger.error(
            f"Recording DEAD-LETTER task {task_id} ({email_type} -> {recipient}): "
            f"Failed after {retry_count} retries. Reason: {failure_reason}"
        )

        # 1. Persist to relational DeadLetterEmailJob model
        dlq_record = DeadLetterEmailJob.objects.create(
            task_id=task_id,
            idempotency_key=idempotency_key,
            email_type=email_type,
            recipient=recipient,
            payload=payload,
            failure_reason=failure_reason,
            exception_class=exception_class,
            retry_count=retry_count,
            traceback=traceback_str,
        )

        # 2. Update persistent delivery log
        EmailDeliveryLog.objects.filter(idempotency_key=idempotency_key).update(
            status=EmailDeliveryStatus.DEAD_LETTER,
            attempts=retry_count,
        )

        # 3. Publish to Redis DLQ list for stream consumers
        redis_client = IdempotencyManager.get_redis_client()
        if redis_client:
            try:
                dlq_payload = json.dumps(
                    {
                        "task_id": task_id,
                        "idempotency_key": idempotency_key,
                        "email_type": email_type,
                        "recipient": recipient,
                        "payload": payload,
                        "failure_reason": failure_reason,
                        "exception_class": exception_class,
                        "retry_count": retry_count,
                        "timestamp": timezone.now().isoformat(),
                    }
                )
                redis_client.rpush("dlq:transactional_emails", dlq_payload)
            except Exception as e:
                logger.warning(f"Could not push to Redis DLQ list: {e}")

        # 4. Release execution lock
        IdempotencyManager.release_lock(idempotency_key)

        return dlq_record
