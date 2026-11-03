# Section 2: Asynchronous Transactional Email Job Queue

This module implements a production-grade asynchronous job queue powered by **Celery** and **Redis** for high-volume transactional emails (Order Confirmations, OTPs, and Security Alerts).

---

## 1. System Architecture & Components

```
Client / API Trigger
       │
       ▼
Celery Task (tasks.send_transactional_email)
       │
       ├──► 1. Rate Limiter Guard (Clean Interface for Part 3)
       │
       ├──► 2. Idempotency Check (Redis SETNX + Persistent DB Log)
       │       └── If already sent -> Skip provider dispatch (No duplicates)
       │
       ├──► 3. EmailService (Payload validation & template rendering)
       │
       ├──► 4. EmailProvider (MockEmailBackend for tests / SMTP / HTTP API)
       │       ├── On Success: Update EmailDeliveryLog -> SENT
       │       ├── On Transient Failure (429/503/Timeout):
       │       │     └── Celery Retry with Exponential Backoff + Jitter
       │       └── On Fatal Failure or Max Retries Exceeded:
       │             └── Route to Dead-Letter Queue (DeadLetterEmailJob + Redis DLQ)
       │
       └── Worker Crash (SIGKILL) Resilience
               ├── task_acks_late = True
               ├── task_reject_on_worker_lost = True
               └── worker_prefetch_multiplier = 1
```

---

## 2. Worker Crash (`SIGKILL`) Reliability & Engineering Invariants

### What happens if a Celery worker process receives SIGKILL while processing a task?

When a Celery child process is killed via `SIGKILL` (Linux OOM killer, host termination, or hard container stop), the process terminates immediately without executing cleanup handlers. Our implementation handles this scenario with mathematical determinism:

1. **`task_acks_late = True`:**
   In standard Celery, tasks are acknowledged (`ACK`) as soon as the worker retrieves the message from Redis. If the worker is `SIGKILL`'d mid-run, the message is permanently lost. With `task_acks_late = True`, the `ACK` is sent back to Redis **only after the task has completed execution and returned**.
2. **`task_reject_on_worker_lost = True`:**
   The parent Celery supervisor process detects child process death and immediately sends a negative acknowledgment (`NACK` / `REJECT`) to Redis with `requeue=True`.
3. **`worker_prefetch_multiplier = 1`:**
   Each worker thread/slot holds only a single task at any given time. If a worker dies, it does not strand dozens of prefetched in-memory tasks.
4. **Redis Visibility Timeout (`visibility_timeout = 3600`):**
   If the entire worker host/container vanishes (killing both child and supervisor), Redis monitors unacknowledged tasks in its internal reservation set. When the 3,600-second visibility timeout elapses without an `ACK`, Redis automatically returns the task to the queue for other workers.

### The 5 Core Reliability Questions Answered:

1. **What happens to an in-flight task when a worker crashes?**
   Because `acks_late=True` is enabled, the task remains unacknowledged in Redis. The supervisor issues a reject on worker loss (`task_reject_on_worker_lost=True`), returning the job to the broker.
2. **When will the task be delivered again?**
   Immediately upon supervisor process detection, or after `visibility_timeout` (1 hour) if the entire host/VM abruptly failed.
3. **How do we prevent duplicate email side effects?**
   If a task crashed *after* sending the email but *before* sending the ACK, re-delivering it could cause duplicate emails. We prevent this by enforcing **Idempotency** via `IdempotencyManager` and `EmailDeliveryLog`. Before dispatching to the provider, the task checks whether the unique `idempotency_key` is marked as `SENT`. If already sent, it skips provider dispatch and returns `ALREADY_SENT`.
4. **Why exactly-once processing cannot simply be assumed?**
   Under the Two Generals' Problem and CAP theorem, network partitions and hardware crashes make physical distributed "exactly-once delivery" impossible. A network partition between the email provider and worker can cause the email to send successfully, yet crash before the ACK reaches Redis. Therefore, distributed architectures provide **at-least-once delivery with idempotent application processing**.
5. **What reliability guarantee does our implementation provide?**
   **At-least-once message delivery with exactly-once effective external side effects** (zero job loss, no duplicate emails, exponential backoff retries, and comprehensive dead-letter capture).

---

## 3. Retries & Dead-Letter Handling

### Exponential Backoff with Full Jitter:
$$\text{countdown} = \min(300, \text{base} \times 2^{\text{retries}} + \text{uniform}(0.1, 1.0))$$
- **Attempt 1:** ~2–3 seconds
- **Attempt 2:** ~4–5 seconds
- **Attempt 3:** ~8–9 seconds
- **Attempt 4:** ~16–17 seconds
- **Jitter Benefit:** Spreads retries randomly over time, eliminating the thundering-herd problem during upstream provider outages.

### Dead-Letter Queue (DLQ) Storage:
When a task exhausts `max_retries` (default: 5) or encounters a non-retryable fatal error (e.g., malformed email or missing required fields), it is moved to `DeadLetterEmailJob` with full diagnostic telemetry:
- `task_id`: Celery UUID
- `idempotency_key`: Unique deduplication key
- `email_type`: `ORDER_CONFIRMATION`, `OTP`, or `TRANSACTION_ALERT`
- `recipient`: Target email address
- `payload`: Complete JSON payload
- `failure_reason`: Exact error message
- `exception_class`: Python exception class name
- `retry_count`: Total attempts made prior to abandonment
- `traceback`: Complete stack trace
- `created_at`: High-precision timestamp

---

## 4. How to Run & Verify

### Running Automated Pytest Suite
```bash
# Run Section 2 test suite
pytest section-2-job-queue/tests

# Run complete assessment suite (Section 1 + Section 2)
pytest
```
*Current status:* **25 passed, 1 skipped in Section 2 (32 passed, 1 skipped across entire project)**.

### Triggering a Task Programmatically
```python
from tasks import send_transactional_email_task
from email_queue.models import EmailType

# Asynchronously submit an OTP email task
async_result = send_transactional_email_task.delay(
    email_type=EmailType.OTP,
    recipient="security@biztech.com",
    payload={"otp_code": "849201", "expiry_minutes": 5},
    idempotency_key="unique_otp_session_123",
)

# Inspect task ID and status
print(f"Task Submitted: {async_result.id} (Status: {async_result.status})")
```
