# Architectural Design: Rate-Limited Asynchronous Job Queue

**System Context:** High-throughput transactional email delivery system (order confirmations, OTPs, security alerts).  
**Upstream Provider Constraint:** Maximum 200 emails per minute (hard rate limit).  
**Traffic Profile:** Sudden burst loads of 2,000 requests in under 10 seconds during flash sales.  
**Reliability Invariant:** Zero job loss, rate limit enforcement, exponential backoff retries, and crash-resilient worker execution under ungraceful termination (`SIGKILL`).

---

## 1. Architectural Technology Choice & Trade-off Analysis

| Architecture Option | Pros | Cons / Risks | Verdict |
| :--- | :--- | :--- | :--- |
| **Option 1: Celery + Redis** | • Battle-tested distributed task queue.<br>• Native support for `acks_late` and `task_reject_on_worker_lost` for SIGKILL resilience.<br>• Excellent integration with Redis atomic primitives (Lua scripts, sorted sets).<br>• Scalable independent worker pools. | • Operational overhead of broker and worker daemons.<br>• Configuration complexity (prefetch limits, serialization, task routing). | **SELECTED**<br>Best-in-class reliability for burst handling and crash recovery. |
| **Option 2: Django Q (ORM Broker)** | • Zero external broker dependency (stores jobs in Django relational database).<br>• Simpler operational footprint for small workloads. | • Relational database table locks and I/O bottlenecks during 2,000 req/10s flash sales.<br>• Lacks sub-millisecond atomic counters needed for high-frequency sliding-window rate limiting. | **REJECTED**<br>Database polling under 2,000 burst requests risks degrading the primary DB. |
| **Option 3: Custom Python/Redis Queue** | • Full control over queue data structures (Redis `LPUSH` / `BRPOPLPUSH`). | • Re-inventing battle-tested distributed worker mechanics: heartbeat monitoring, process management, backoff scheduling, and worker dead-letter handling. | **REJECTED**<br>Unnecessary engineering complexity and reliability risk. |

### Architectural Decision:
We select **Celery + Redis**. Redis serves as both the task queue broker and the shared atomic state store for the rate limiter.

---

## 2. Rate Limiter Design: Sliding Window with Redis Atomic Lua Script

### Evaluated Patterns:
1. **Option A: Token Bucket (Redis `DECR` + TTL)**
   - *Limitation:* Prone to boundary bursts if tokens refill in discrete intervals; complex state management for refill timers.
2. **Option B: Fixed Window (Redis `INCR` + `EXPIRE`)**
   - *Limitation:* Severe boundary burst vulnerability. 200 emails could be dispatched at second 59, and another 200 emails at second 61 (400 emails in 2 seconds), violating provider limits.
3. **Option C: Sliding Window via Redis Sorted Set (`ZSET`)**
   - *Mechanism:* Each task timestamp is stored as both score and member in a Redis Sorted Set (`ZSET`).
   - *Selection:* **Option C is selected.** It provides a continuous, rolling 60-second window that mathematically guarantees the rate of 200 emails/minute is never exceeded under any burst pattern.

### Atomic Execution via Redis Lua Script:
Network round trips between Celery workers and Redis could create race conditions under concurrent execution. To guarantee absolute atomicity, the sliding window logic is encapsulated in a **single Redis Lua script** executed via `EVALSHA`:

```lua
-- KEYS[1]: rate_limit_key (e.g., "ratelimit:email_provider")
-- ARGV[1]: current_unix_timestamp_float (e.g., 1711200000.123)
-- ARGV[2]: window_seconds (60)
-- ARGV[3]: max_capacity (200)

local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_limit = tonumber(ARGV[3])
local clear_before = now - window

-- 1. Remove timestamps older than the rolling window
redis.call('ZREMRANGEBYSCORE', key, '-inf', clear_before)

-- 2. Count active events in the current window
local current_count = redis.call('ZCARD', key)

-- 3. Check capacity
if current_count < max_limit then
    -- Atomically record this request timestamp
    redis.call('ZADD', key, now, now)
    -- Refresh TTL so idle keys expire automatically
    redis.call('EXPIRE', key, math.ceil(window) + 10)
    return 1 -- Allowed
else
    return 0 -- Throttled
end
```

### Redis Failure Strategy: Fail Open vs. Fail Closed
- **Design Decision: Fail Closed for Third-Party Rate Limits.**
  - If Redis is unreachable or cluster partition occurs, the rate limiter raises a transient operational error, forcing the Celery task to self-throttle and reschedule itself with exponential backoff.
  - *Rationale:* Failing open would immediately spam the third-party provider with 2,000 emails, resulting in account suspension, API bans, and dropped customer notifications. Failing closed guarantees safety while Celery preserves the pending jobs.

---

## 3. Worker Crash & `SIGKILL` Reliability Mechanics

### Scenario:
A Celery worker process is abruptly killed with `SIGKILL` (signal 9, uncatchable process termination by Linux OOM killer or host shutdown) while an email task is in-flight.

### Celery Configuration Guarantee:
To guarantee **zero message loss**, the Celery worker and broker are configured with:

1. **`task_acks_late = True`:**
   By default, Celery acknowledges (`ACK`) messages upon retrieval from the broker before execution begins. If `SIGKILL` strikes during execution, the task is permanently lost.
   With `task_acks_late = True`, the `ACK` is sent **only after** the task successfully finishes execution.
2. **`task_reject_on_worker_lost = True`:**
   When a worker child process terminates abruptly (e.g. `SIGKILL`, segmentation fault), the parent supervisor process catches the death and issues a `NACK` / `REJECT` to the broker with `requeue=True`. The broker re-delivers the job to another healthy worker.
3. **Idempotency Invariant:**
   Because a task can be re-delivered after a crash, the task implementation uses an idempotency key (e.g. `email_id` stored in Redis with an atomic `SET key value NX EX 86400`) to prevent duplicate email transmissions.

---

## 4. Retry Logic & Dead-Letter Handling

- **Exponential Backoff with Full Jitter:**
  $$\text{Delay} = \min(\text{MaxDelay}, \text{BaseDelay} \times 2^{\text{attempt}} + \text{jitter})$$
- **Dead-Letter Handling (DLQ):**
  When a task exceeds its maximum retry threshold (e.g., 5 retries due to persistent recipient SMTP rejection or invalid format), it is moved to a dead-letter state:
  - Dispatches to a dedicated dead-letter queue or persists to `FailedJob` database model for engineering inspection and alert triage.

