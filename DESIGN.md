# Architectural Design: Rate-Limited Asynchronous Job Queue

**System Context:** High-throughput transactional email delivery system (order confirmations, OTPs, security alerts).  
**Upstream Provider Constraint:** Maximum 200 emails per minute (hard rate limit in any rolling 60-second window).  
**Traffic Profile:** Sudden burst loads of 2,000 requests in under 10 seconds during flash sales.  
**Reliability Invariant:** Zero job loss, strict rate limit enforcement, exponential backoff retries, crash-resilient worker execution under ungraceful termination (`SIGKILL`), and atomic sliding-window throttling.

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
   - *Mechanism:* Fixed bucket of tokens refilled at steady rate.
   - *Limitation:* Prone to boundary bursts if tokens refill in discrete intervals; complex state management for refill timers.
2. **Option B: Fixed Window (Redis `INCR` + `EXPIRE`)**
   - *Mechanism:* Counter reset at fixed intervals (e.g., minute boundaries).
   - *Limitation:* Severe boundary burst vulnerability. 200 emails could be dispatched at second 59, and another 200 emails at second 61 (400 emails in 2 seconds), violating provider limits.
3. **Option C: Sliding Window via Redis Sorted Set (`ZSET`)**
   - *Mechanism:* Each task timestamp is stored as both score and member in a Redis Sorted Set (`ZSET`).
   - *Selection:* **Option C is selected.** It provides a continuous, rolling 60-second window that mathematically guarantees the rate of 200 emails/minute is never exceeded under any burst pattern.

### Atomic Execution via Redis Lua Script:
Network round trips between Celery workers and Redis could create race conditions under concurrent execution. To guarantee absolute atomicity, the sliding window logic is encapsulated in a **single Redis Lua script** executed via `EVALSHA`:

```lua
local key = KEYS[1]
local window_ms = tonumber(ARGV[1])
local max_limit = tonumber(ARGV[2])
local explicit_now = tonumber(ARGV[3])
local reservation_id = ARGV[4]

-- 1. Determine current timestamp in milliseconds (Redis server time avoids clock drift)
local now_ms
if explicit_now and explicit_now > 0 then
    now_ms = explicit_now
else
    local redis_time = redis.call('TIME')
    now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
end

local window_start = now_ms - window_ms

-- 2. Prune entries that have slid outside the rolling 60-second window
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- 3. Count remaining active reservations
local current_count = redis.call('ZCARD', key)

-- 4. Evaluate capacity
if current_count < max_limit then
    -- Allowed: record this reservation with score = now_ms
    redis.call('ZADD', key, now_ms, reservation_id)
    -- Set TTL to window + 5s buffer so idle keys clean up automatically
    local ttl_ms = math.ceil(window_ms + 5000)
    redis.call('PEXPIRE', key, ttl_ms)
    return {1, 0, current_count + 1}
else
    -- Denied: find the oldest reservation still in the rolling window
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after_ms = window_ms
    if oldest and #oldest >= 2 then
        local oldest_ts = tonumber(oldest[2])
        retry_after_ms = math.max(1, (oldest_ts + window_ms) - now_ms)
    end
    return {0, retry_after_ms, current_count}
end
```

### Time and Space Complexity Analysis:
- `ZREMRANGEBYSCORE`: $\mathcal{O}(\log N + M)$ where $N \le 200$ and $M$ is the number of expired entries removed.
- `ZCARD`: $\mathcal{O}(1)$ constant time lookup.
- `ZADD`: $\mathcal{O}(\log N)$ insertion where $N \le 200$.
- `ZRANGE`: $\mathcal{O}(\log N + 1) = \mathcal{O}(1)$ retrieval of oldest member.
- `PEXPIRE`: $\mathcal{O}(1)$.
- **Total Latency:** $< 0.1$ ms per evaluation.
- **Memory Footprint:** 200 members in Redis Sorted Set occupies $< 15$ KB.

### Clock Consistency Strategy:
- In production, the Lua script executes `redis.call('TIME')` internally, retrieving the high-precision microsecond clock directly from the Redis daemon. This eliminates clock drift across distributed Celery worker hosts.
- In unit testing, `explicit_now` enables deterministic time advancement without sleeping.

### Redis Failure Strategy: Fail Open vs. Fail Closed
- **Decision: FAIL CLOSED.**
  - If Redis is unreachable, the rate limiter raises a `RedisRateLimiterUnavailableError` or returns `allowed=False`.
  - *Rationale:* Failing open would immediately spam the third-party provider with 2,000 emails, resulting in account suspension, API bans, and dropped customer notifications. Failing closed guarantees safety while Celery preserves pending jobs for scheduled retry.

---

## 3. Rate-Limit Deferral vs. Provider Failure Retry Budget

A critical engineering principle implemented in `tasks.py`:
- **Rate-Limit Deferrals DO NOT consume the provider retry budget (`max_retries`).**
  - When a task is rate-limited, it is rescheduled via Celery (`countdown = retry_after_seconds`) with `status = DEFERRED`.
  - Its `provider_retries` counter remains `0`.
  - It NEVER enters the Dead-Letter Queue (DLQ) due to traffic volume.
- **Provider Retries ARE counted against `max_retries`:**
  - If the external email API returns 503 or 429, `provider_retries` increments.
  - After 5 true provider failures, the task moves to `DeadLetterEmailJob`.

---

## 4. Worker Crash & `SIGKILL` Reliability Mechanics

### Scenario:
A Celery worker process is abruptly killed with `SIGKILL` (signal 9, uncatchable process termination by Linux OOM killer or host shutdown) while an email task is in-flight.

### Celery Configuration Guarantee:
To guarantee **zero message loss**, the Celery worker and broker are configured with:

1. **`task_acks_late = True`:**
   By default, Celery acknowledges (`ACK`) messages upon retrieval from the broker before execution begins. If `SIGKILL` strikes during execution, the task is permanently lost. With `task_acks_late = True`, the `ACK` is sent **only after** the task successfully finishes execution.
2. **`task_reject_on_worker_lost = True`:**
   When a worker child process terminates abruptly (e.g. `SIGKILL`, segmentation fault), the parent supervisor process catches the death and issues a `NACK` / `REJECT` to the broker with `requeue=True`. The broker re-delivers the job to another healthy worker.
3. **`worker_prefetch_multiplier = 1`:**
   Prevents task hoarding and worker starvation during sudden bursts.
4. **Idempotency Invariant:**
   Because a task can be re-delivered after a crash, the task implementation uses an idempotency key (e.g. `idempotency:lock:{key}` in Redis + `EmailDeliveryLog` in the DB) to prevent duplicate email transmissions.
