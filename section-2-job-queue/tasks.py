import random
import traceback
import logging
from typing import Dict, Any, Optional
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from django.conf import settings

from services.email_service import EmailService, EmailPayloadValidationError
from services.email_provider import (
    TransientEmailProviderError,
    FatalEmailProviderError,
)
from services.dead_letter import DeadLetterService
from services.idempotency import IdempotencyManager
from services.rate_limiter_interface import get_rate_limiter

logger = logging.getLogger(__name__)

# Configurable retry policy
BASE_RETRY_DELAY = 2  # Seconds
MAX_RETRY_DELAY = 300  # 5 minutes maximum backoff
DEFAULT_MAX_RETRIES = getattr(settings, "CELERY_MAX_RETRIES", 5)


def calculate_exponential_backoff(retry_count: int, base: float = BASE_RETRY_DELAY) -> float:
    """
    Computes exponential backoff with full jitter:
    countdown = min(MAX_RETRY_DELAY, base * (2 ** retry_count) + uniform(0, 1))
    """
    exponential = base * (2 ** retry_count)
    jitter = random.uniform(0.1, 1.0)
    return min(MAX_RETRY_DELAY, exponential + jitter)


@shared_task(
    bind=True,
    name="tasks.send_transactional_email",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=DEFAULT_MAX_RETRIES,
)
def send_transactional_email_task(
    self,
    email_type: str,
    recipient: str,
    payload: Dict[str, Any],
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Asynchronous Celery task for reliable transactional email delivery.

    Reliability Invariants:
    1. Worker Crash Resilience: acks_late=True and reject_on_worker_lost=True ensure
       tasks killed via SIGKILL are re-queued rather than lost.
    2. Idempotency: Duplicate executions (from retry or worker crash re-delivery)
       are deduplicated and will not send duplicate emails.
    3. Exponential Backoff: Temporary provider failures retry with full jitter.
    4. Dead-Letter Queue (DLQ): Fatal errors and exhausted retries are recorded
       with full diagnostic metadata.
    """
    task_id = self.request.id or "direct_call"
    retries = self.request.retries

    # Generate default deterministic idempotency key if none provided
    if not idempotency_key:
        unique_identifier = payload.get("order_number") or payload.get("otp_code") or payload.get("account_id") or task_id
        idempotency_key = f"{email_type}:{recipient}:{unique_identifier}"

    logger.info(
        f"[Task {task_id}] Processing {email_type} to {recipient} (Attempt {retries + 1}/{self.max_retries + 1})"
    )

    # 1. Rate limiter check (Interface prepared for Part 3)
    rate_limiter = get_rate_limiter()
    if not rate_limiter.acquire("ratelimit:email_provider", max_limit=200, window_seconds=60):
        # Local rate limit reached; schedule retry with small backoff
        logger.warning(f"[Task {task_id}] Rate limiter throttled dispatch. Scheduling retry.")
        countdown = calculate_exponential_backoff(retries, base=1)
        raise self.retry(
            exc=TransientEmailProviderError("Local rate limit capacity reached"),
            countdown=countdown,
        )

    # 2. Process and dispatch via EmailService
    try:
        result = EmailService.process_and_send(
            task_id=task_id,
            idempotency_key=idempotency_key,
            email_type=email_type,
            recipient=recipient,
            payload=payload,
        )
        return result

    except (EmailPayloadValidationError, FatalEmailProviderError) as fatal_err:
        # Non-recoverable fatal errors must NOT be retried; dispatch to DLQ immediately
        logger.error(f"[Task {task_id}] Permanent fatal error: {fatal_err}. Moving to Dead-Letter Queue.")
        DeadLetterService.record_permanent_failure(
            task_id=task_id,
            idempotency_key=idempotency_key,
            email_type=email_type,
            recipient=recipient,
            payload=payload,
            failure_reason=str(fatal_err),
            exception_class=fatal_err.__class__.__name__,
            retry_count=retries,
            traceback_str=traceback.format_exc(),
        )
        return {
            "status": "DEAD_LETTER",
            "reason": str(fatal_err),
            "idempotency_key": idempotency_key,
            "retry_count": retries,
        }

    except (TransientEmailProviderError, Exception) as transient_err:
        # Check if maximum retries reached
        if retries >= self.max_retries:
            logger.error(
                f"[Task {task_id}] Maximum retries ({self.max_retries}) exceeded. "
                f"Moving task to Dead-Letter Queue."
            )
            DeadLetterService.record_permanent_failure(
                task_id=task_id,
                idempotency_key=idempotency_key,
                email_type=email_type,
                recipient=recipient,
                payload=payload,
                failure_reason=f"Exhausted {self.max_retries} retries: {transient_err}",
                exception_class=transient_err.__class__.__name__,
                retry_count=retries,
                traceback_str=traceback.format_exc(),
            )
            return {
                "status": "DEAD_LETTER",
                "reason": f"Max retries exceeded: {transient_err}",
                "idempotency_key": idempotency_key,
                "retry_count": retries,
            }

        # Calculate exponential backoff countdown with jitter
        countdown = calculate_exponential_backoff(retries, base=BASE_RETRY_DELAY)
        logger.warning(
            f"[Task {task_id}] Transient failure ({transient_err}). "
            f"Scheduling retry {retries + 1}/{self.max_retries} in {countdown:.2f} seconds."
        )

        # Release lock so retry attempt can execute
        IdempotencyManager.release_lock(idempotency_key)

        # Re-raise via Celery retry
        raise self.retry(exc=transient_err, countdown=countdown)
