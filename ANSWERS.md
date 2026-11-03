# Biz-Tech Analytics Technical Assessment: Written Answers

---

## SECTION 1: DIAGNOSE A BROKEN SYSTEM

### 1. Incident Investigation Log
**Scenario:** A mobile dashboard endpoint `GET /api/orders/summary/` historically responded in ~80ms. Following a routine deployment, it began timing out at 30+ seconds for users with >200 orders. No code change was made to the view itself.

#### Investigation Sequence:
1. **Initial Review (Views & Deploy Diff):** Verified that `views.py` remained unchanged (`Order.objects.filter(customer=customer)`). Recognized that in Django applications, latency regressions frequently originate in model properties, serializers, or signals that trigger lazy ORM evaluations.
2. **APM & Production Telemetry Inspection:** Examined request duration by customer order volume. Noticed that response time was strictly proportional to order volume ($O(N)$ growth): users with 5 orders experienced negligible latency, while users with >200 orders exceeded the 30-second gateway timeout.
3. **Serializer & Model Audit:** Discovered that in the routine deployment, new fields were added to the API payload (`customer_name`, `customer_tier`, `items_count`, `total_amount`, `total_weight_grams`, `categories`). These fields were implemented as `@property` methods on `Order` and traversed foreign keys (`customer`), reverse relations (`items.count()`, `items.all()`), and nested foreign keys (`item.product.weight_grams`, `item.product.category.name`).
4. **Hypothesis & Root Cause Narrowing:** Because the view did not eagerly load related objects via `select_related()` or `prefetch_related()`, each property access on every serialized order forced Django's ORM to dispatch a synchronous SQL query.
5. **Controlled Reproduction & Profiling:** Seeded 250 orders for a test user. Profiling via Django Silk revealed **5,078 SQL queries** taking **29,353 ms (29.35 seconds)**, confirming the exact 30-second production timeout mechanism.
6. **Resolution & Parity Verification:** Applied `select_related('customer')`, `prefetch_related(Prefetch('items', queryset=OrderItem.objects.select_related('product__category')))`, and database-level annotations (`Count`, `Sum`). Query count plummeted to **3 queries** taking **225 ms** (with 100% exact data parity).

---

### 2. Root Cause Category & Justification
**Category:** **ORM N+1 Query Explosion caused by Serializer / Related-Object Lazy-Loading Regression.**

**Justification:**
Django QuerySets are lazy and load only the columns of the model queried unless explicitly instructed otherwise. When the serialization layer accesses related entities (`order.customer`, `order.items.count()`, `item.product.category`), Django executes an independent SQL query for each record.
For $N$ orders and $M$ items per order, the query complexity grew to:
$$\text{Queries} = 1 + N (\text{customer}) + N (\text{count}) + N (\text{items}) + (N \times M)(\text{product}) + (N \times M)(\text{category})$$
For $N = 250$ orders, this resulted in **5,078 SQL queries** per HTTP request. Over any standard database connection pool or network interface (with 5–8ms latency per query), 5,000 queries require 25 to 40 seconds, triggering an HTTP 504 Gateway Timeout. The view was untouched because the regression was introduced in serializer field mapping and model properties.

---

### 3. Code Implementation & ORM-Level Explanation

#### A. Original (Problematic) View & Serializer
```python
# View:
orders = Order.objects.filter(customer=customer).order_by("-created_at")
serializer = UnoptimizedOrderSummarySerializer(orders, many=True)

# Model / Serializer Behavior:
# - order.customer.name -> executes SELECT FROM customers WHERE id = %s (for each order)
# - order.items.count() -> executes SELECT COUNT(*) FROM order_items WHERE order_id = %s (for each order)
# - order.items.all()   -> executes SELECT FROM order_items WHERE order_id = %s (for each order)
# - item.product.weight -> executes SELECT FROM products WHERE id = %s (for each item)
# - item.product.category -> executes SELECT FROM categories WHERE id = %s (for each product)
```

#### B. Optimized Production Implementation
```python
optimized_items_prefetch = Prefetch(
    "items",
    queryset=OrderItem.objects.select_related("product__category"),
)

orders = (
    Order.objects.filter(customer=customer)
    .select_related("customer")
    .prefetch_related(optimized_items_prefetch)
    .annotate(
        annotated_items_count=Count("items", distinct=True),
        annotated_total_amount=Sum(
            F("items__quantity") * F("items__unit_price")
        ),
    )
    .order_by("-created_at")
)
serializer = OptimizedOrderSummarySerializer(orders, many=True)
```

#### C. Why the Fix Works at the Database and ORM Level
1. **`select_related("customer")`:**
   Applies an SQL `INNER JOIN` in the initial `SELECT` statement:
   ```sql
   SELECT orders.*, customers.*
   FROM orders
   INNER JOIN customers ON (orders.customer_id = customers.id)
   WHERE orders.customer_id = %s;
   ```
   This resolves all customer attributes in the primary query, eliminating $N$ queries.
2. **`prefetch_related(Prefetch("items", queryset=OrderItem.objects.select_related("product__category")))`:**
   Executes exactly **one** secondary SQL query utilizing an `IN` clause with multi-table joins:
   ```sql
   SELECT order_items.*, products.*, categories.*
   FROM order_items
   INNER JOIN products ON (order_items.product_id = products.id)
   INNER JOIN categories ON (products.category_id = categories.id)
   WHERE order_items.order_id IN (1, 2, ..., 250);
   ```
   Django's ORM populates the `_prefetched_objects_cache` in RAM. Subsequent evaluations of `order.items.all()` and `item.product.category` read directly from memory with zero database roundtrips.
3. **`annotate(Count, Sum)`:**
   Executes `COUNT(DISTINCT order_items.id)` and `SUM(quantity * unit_price)` directly inside PostgreSQL/SQLite. This avoids redundant count queries and removes CPU-heavy Python iteration over hundreds of model instances.

#### D. Trade-offs Introduced by the Fix
- **Memory Consumption:** Storing prefetched objects in memory consumes additional RAM. For 250 orders (~900 items), memory overhead is minimal (<2 MB). For endpoints returning tens of thousands of rows, pagination (e.g. cursor pagination) must accompany prefetching.
- **SQL Join Complexity:** Multi-table joins and aggregation group-by clauses require database query planner optimization. However, executing 2 set-based queries is exponentially more efficient than managing 5,000 connection pool roundtrips.

---

### 4. Profiler Integration & Genuine Telemetry Results

Django Silk is fully integrated (`/silk/`). Benchmarking was executed against genuine seeded data (250 orders, 889 order items).

| Metric | Unoptimized View | Optimized View | Improvement |
| :--- | :--- | :--- | :--- |
| **Endpoint** | `/api/orders/summary/unoptimized/` | `/api/orders/summary/` | Clean Drop-in |
| **Orders Processed** | 250 | 250 | Baseline Equal |
| **Application Query Count** | **5,078 queries** | **11 queries (3 business SQL)** | **99.78% reduction** |
| **Wall Clock Latency** | **29,353.71 ms (29.35s)** | **225.63 ms** | **99.23% faster** |
| **Silk Request ID** | `4e293118-d426-4543-a2bb-e5d051cb5c2d` | `2293d062-d441-4cd0-a5be-1652c3b95eb9` | Stored in DB |
| **Data Parity Check** | Baseline | Identical | **100% Match** |

**Profiler Evidence Artifacts (Available in `section-1-django-debugging/profiler-evidence/`):**
- `query_analysis.json` — Machine-readable performance metrics.
- `before_optimization_sql.txt` — 15,240 lines of captured unoptimized SQL statements.
- `after_optimization_sql.txt` — Exact SQL statements for optimized endpoint.
- `silk_benchmark_report.md` — In-depth profiler analysis.
- `how_to_capture_silk_ui.md` — Step-by-step instructions for viewing the Silk web UI.

---

### 5. Preventative Engineering Recommendations
1. **Automated Query Budget Tests:** Integrate `django-assert-num-queries` into CI/CD pipelines to assert that list/summary endpoints execute $O(1)$ constant queries regardless of fixture scale.
2. **Linting & Code Review Gates:** Implement pre-commit checks or AST linters flagging un-prefetched relational access inside serializers.
3. **Strict Serializer Profiling:** Require Silk or Debug Toolbar query counts in PR descriptions when modifying serializers.

---

## SECTION 2: DESIGN A RATE-LIMITED ASYNC JOB QUEUE

### 1. Architectural Architecture & Core Components
- **Broker & Backend:** Celery 5.6+ with Redis 7+ backend.
- **Worker Configuration:** Dedicated concurrency pool with Redis broker visibility timeout of 3600s.
- **Modular Services:**
  - `EmailService`: Decoupled business logic responsible for payload validation, template formatting, state transitions, and audit logging.
  - `MockEmailBackend`: In-memory configurable testing provider allowing precise simulation of transient errors, permanent failures, and delivery tracking without sending real emails.
  - `IdempotencyManager`: Redis atomic locks (`SET NX EX`) and persistent database log checking to enforce effective exactly-once delivery.
  - `DeadLetterService`: Dual-destination dead-letter logging (`DeadLetterEmailJob` model + `dlq:transactional_emails` Redis list).
  - `RateLimiterInterface`: Modular rate-limiter abstraction preparing for Part 3 Redis Lua sliding-window rate limiting.

---

### 2. Retry Logic & Dead-Letter Handling
1. **Error Categorization:**
   - *Transient Errors (`TransientEmailProviderError`):* 429 Too Many Requests, 502/503/504 Upstream Gateway Errors, socket timeouts. Eligible for automated retry.
   - *Fatal Errors (`FatalEmailProviderError`, `EmailPayloadValidationError`):* Invalid recipient syntax, missing required fields (e.g., missing `order_number` in `ORDER_CONFIRMATION`), blacklisted addresses. Non-retryable; routed immediately to Dead-Letter Queue.
2. **Exponential Backoff with Full Jitter:**
   $$\text{delay} = \min(300, \text{base} \times 2^{\text{retry\_count}} + \text{uniform}(0.1, 1.0))$$
   - Prevents retry synchronization (thundering herd) against recovering third-party mail APIs.
3. **Dead-Letter Storage:**
   When `max_retries` is reached or a fatal error occurs, the task writes a structured record to `DeadLetterEmailJob` capturing:
   - Celery `task_id`
   - `idempotency_key`
   - `email_type`
   - `recipient`
   - `payload` (JSON)
   - `failure_reason`
   - `exception_class`
   - `retry_count`
   - Full `traceback`
   - High-precision `created_at` timestamp

---

### 3. Worker Crash Reliability Analysis (SIGKILL)

#### Question: What happens if a Celery worker process receives SIGKILL while processing a task?

When a worker process receives `SIGKILL`, the process is terminated immediately by the operating system kernel. Operating system signal handlers do not run. Our implementation guarantees zero job loss and zero duplicate emails through four coordinating mechanisms:

1. **`task_acks_late = True`:**
   By default, Celery acknowledges messages upon popping them off the Redis broker *before* execution starts (`acks_early`). If a worker is killed via `SIGKILL` mid-run, the task is permanently lost. By setting `task_acks_late = True`, the `ACK` is deferred until after the Python task function has completed successfully and returned its result.
2. **`task_reject_on_worker_lost = True`:**
   The Celery supervisor process continuously monitors child worker processes. If a child dies abruptly (from `SIGKILL`, segmentation fault, or kernel OOM), the supervisor intercepts the exit code and issues a `NACK` / `REJECT` with `requeue=True` to Redis, instantly returning the task to the queue for another worker.
3. **`worker_prefetch_multiplier = 1`:**
   Ensures each worker slot prefetches at most 1 task, preventing worker starvation and task hoarding during sudden burst spikes.
4. **Redis Broker `visibility_timeout` (3600s):**
   If catastrophic host failure kills both child worker and supervisor, Redis monitors the unacknowledged queue. If no ACK is received within the visibility window, Redis safely re-queues the message.

#### The 5 Specific Questions Answered:
1. **What happens to an in-flight task when a worker crashes?**
   The task remains unacknowledged in Redis. When the supervisor detects child termination, it sends a reject command with `requeue=True`, returning the task to the active queue.
2. **When will the task be delivered again?**
   Immediately upon supervisor process detection, or after `visibility_timeout` (3,600 seconds) if the entire server/container halted.
3. **How do we prevent duplicate email side effects?**
   Because the worker might have died *after* communicating with the email provider but *before* transmitting the ACK, re-delivery creates a duplicate execution risk. We eliminate this via `IdempotencyManager`: before dispatching, the task inspects `EmailDeliveryLog` where `idempotency_key` is marked `SENT`. If already sent, the task returns `ALREADY_SENT` without contacting the provider again.
4. **Why exactly-once processing cannot simply be assumed?**
   Under the Two Generals' Problem, physical network boundaries prevent atomic synchronization between third-party SMTP/API networks and local Redis broker queues. Hence, distributed architectures guarantee **at-least-once delivery**, which requires application-layer idempotency to achieve effective exactly-once external side effects.
5. **What reliability guarantee does our implementation provide?**
   **At-least-once message delivery with idempotent execution semantics**, guaranteeing zero job loss, resilience across worker crashes (`SIGKILL`), exponential backoff retries with jitter, and comprehensive dead-letter capture.

---

## SECTION 3: WRITTEN ARCHITECTURE REVIEW
*(Will be finalized as part of comprehensive submission review.)*

