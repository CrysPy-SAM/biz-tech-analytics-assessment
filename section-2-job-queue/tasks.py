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
from services.redis_rate_limiter import RateLimitResult

logger = logging.getLogger(__name__)

# Configurable retry policy
BASE_RETRY_DELAY = 2  # Seconds
MAX_RETRY_DELAY = 300  # 5 minutes maximum backoff
DEFAULT_MAX_RETRIES = getattr(settings, "CELERY_MAX_RETRIES", 5)
RATE_LIMIT_PROVIDER_KEY = "email:rate_limit:provider"


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
    provider_retries: int = 0,
    rate_limit_deferrals: int = 0,
) -> Dict[str, Any]:
    """
    Asynchronous Celery task for reliable, rate-limited transactional email delivery.

    Execution Workflow:
    1. Validate payload format (fatal errors route to DLQ immediately).
    2. Check idempotency (duplicate deliveries return ALREADY_SENT without consuming slots).
    3. Reserve slot via atomic Redis Sliding Window Rate Limiter.
    4. If denied: Defer task safely via Celery countdown WITHOUT consuming provider retry budget.
    5. If allowed: Dispatch through EmailService.
    6. If provider transiently fails: Exponential backoff retry.
    7. If provider fatal/exhausted: Route to Dead-Letter Queue.
    """
    task_id = self.request.id or "direct_call"
    retries = max(getattr(self.request, "retries", 0), provider_retries)

    # 1. Deterministic idempotency key generation
    if not idempotency_key:
        unique_identifier = (
            payload.get("order_number")
            or payload.get("otp_code")
            or payload.get("account_id")
            or task_id
        )
        idempotency_key = f"{email_type}:{recipient}:{unique_identifier}"

    logger.info(
        f"[Task {task_id}] Received {email_type} -> {recipient} "
        f"(Provider attempt {retries + 1}/{DEFAULT_MAX_RETRIES + 1}, Deferrals: {rate_limit_deferrals})"
    )

    # 2. Strict Payload Validation upfront
    try:
        EmailService.validate_payload(email_type, recipient, payload)
    except EmailPayloadValidationError as val_err:
        logger.error(f"[Task {task_id}] Validation failed: {val_err}. Routing to Dead-Letter Queue.")
        DeadLetterService.record_permanent_failure(
            task_id=task_id,
            idempotency_key=idempotency_key,
            email_type=email_type,
            recipient=recipient,
            payload=payload,
            failure_reason=str(val_err),
            exception_class=val_err.__class__.__name__,
            retry_count=retries,
            traceback_str=traceback.format_exc(),
        )
        return {
            "status": "DEAD_LETTER",
            "reason": str(val_err),
            "idempotency_key": idempotency_key,
            "retry_count": retries,
        }

    # 3. Idempotency Check: Don't consume rate limit or resend if already SENT
    if IdempotencyManager.is_already_sent(idempotency_key):
        logger.info(f"[Task {task_id}] Key '{idempotency_key}' already SENT. Deduplicating.")
        return {
            "status": "ALREADY_SENT",
            "idempotency_key": idempotency_key,
        }

    # 4. Atomic Redis Sliding Window Rate Limiter Reservation
    rate_limiter = get_rate_limiter()
    if hasattr(rate_limiter, "acquire_detailed"):
        rl_res = rate_limiter.acquire_detailed(
            key=RATE_LIMIT_PROVIDER_KEY,
            max_limit=200,
            window_seconds=60,
        )
    else:
        allowed = rate_limiter.acquire(RATE_LIMIT_PROVIDER_KEY, max_limit=200, window_seconds=60)
        rl_res = RateLimitResult(allowed=allowed, retry_after_ms=1000 if not allowed else 0, current_count=0, reservation_id="")

    if not rl_res.allowed:
        # CAPACITY REACHED: Safe deferral WITHOUT burning provider retry count
        countdown = max(1.0, round(rl_res.retry_after_ms / 1000.0, 2))
        logger.warning(
            f"[Task {task_id}] Provider rate limit reached (Current: {rl_res.current_count}/200). "
            f"Deferring task for {countdown}s. (Deferral #{rate_limit_deferrals + 1})"
        )

        IdempotencyManager.release_lock(idempotency_key)

        # Reschedule for future window if in live Celery environment
        if hasattr(self, "request") and self.request.id:
            try:
                # Re-queue task using Celery countdown
                send_transactional_email_task.apply_async(
                    kwargs={
                        "email_type": email_type,
                        "recipient": recipient,
                        "payload": payload,
                        "idempotency_key": idempotency_key,
                        "provider_retries": retries,
                        "rate_limit_deferrals": rate_limit_deferrals + 1,
                    },
                    countdown=countdown,
                )
            except Exception as sched_err:
                logger.warning(f"Failed to apply_async deferred task: {sched_err}")

        return {
            "status": "DEFERRED",
            "retry_after_seconds": countdown,
            "idempotency_key": idempotency_key,
            "rate_limit_deferrals": rate_limit_deferrals + 1,
        }

    # 5. Rate limit reservation confirmed -> Dispatch via EmailService
    try:
        result = EmailService.process_and_send(
            task_id=task_id,
            idempotency_key=idempotency_key,
            email_type=email_type,
            recipient=recipient,
            payload=payload,
        )
        return result

    except FatalEmailProviderError as fatal_err:
        logger.error(f"[Task {task_id}] Permanent provider fatal error: {fatal_err}. Moving to DLQ.")
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
        # Check if maximum retries reached for upstream provider failures
        if retries >= self.max_retries:
            logger.error(
                f"[Task {task_id}] Maximum retries ({self.max_retries}) exhausted: {transient_err}. Moving to DLQ."
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

        countdown = calculate_exponential_backoff(retries, base=BASE_RETRY_DELAY)
        logger.warning(
            f"[Task {task_id}] Upstream provider error ({transient_err}). "
            f"Scheduling retry {retries + 1}/{self.max_retries} in {countdown:.2f}s."
        )

        IdempotencyManager.release_lock(idempotency_key)
        raise self.retry(exc=transient_err, countdown=countdown)
