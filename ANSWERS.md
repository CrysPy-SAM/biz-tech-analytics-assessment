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

## SECTION 2 & 3: RATE-LIMITED ASYNC JOB QUEUE & SLIDING WINDOW LIMITER

### 1. How the Limiter Guarantees Maximum 200 Emails per Rolling 60 Seconds
The rate limiter uses a **Redis Sorted Set (`ZSET`)** where each element's score is its submission millisecond timestamp (`now_ms`), and its member is a unique reservation UUID.
When evaluating a reservation request:
1. `ZREMRANGEBYSCORE key -inf (now_ms - 60000)` purges all reservations older than 60 seconds from the current instant.
2. `ZCARD key` calculates the exact number of active reservations in the rolling 60-second window.
3. If `current_count < 200`, the transaction inserts the new reservation with `ZADD key now_ms <uuid>` and refreshes key expiration with `PEXPIRE key 65000`.
4. If `current_count >= 200`, the transaction queries the oldest active reservation via `ZRANGE key 0 0 WITHSCORES` and returns the exact millisecond delta until that slot expires:
   $$\text{retry\_after\_ms} = (\text{oldest\_ts} + 60000) - \text{now\_ms}$$
Because time is evaluated continuously rather than at fixed bucket boundaries, boundary bursts (e.g. 200 emails at second 59 and 200 at second 61) are mathematically impossible.

---

### 2. Why Lua Scripting is Strictly Mandatory
If the sequence `ZREMRANGEBYSCORE` $\to$ `ZCARD` $\to$ `ZADD` were implemented as separate Python commands:
- Under concurrent execution (e.g. 20 Celery workers processing burst requests), Worker A and Worker B could both execute `ZCARD` simultaneously when the count is 199.
- Both workers would read `count = 199 < 200`, both would conclude capacity exists, and both would issue `ZADD`, resulting in 201 emails being dispatched.
- Under flash sale bursts (2,000 requests in 10s), non-atomic checks create catastrophic race conditions that cause immediate provider rate limit bans.
- By executing inside a **Redis Lua script**, Redis's single-threaded event loop executes the prune, count, check, and reservation as a single, uninterrupted atomic unit of work with zero interleaving from other clients.

---

### 3. Redis Failure Strategy (Fail-Closed)
- When Redis is unavailable or raises network errors, the rate limiter **fails closed** (`allowed = False`).
- **Rationale:** Failing open would flood the external email provider with 2,000 un-throttled emails during an outage, causing immediate provider suspension, IP blacklisting, and permanent notification drops.
- Failing closed ensures that tasks are safely deferred with Celery countdowns until Redis connectivity is restored, preserving 100% of jobs without violating provider limits.

---

### 4. How 500 Queued Jobs are Processed Safely
During the 500-job stress test:
1. **Window 1 (T = 0s to 60s):** The first 200 jobs acquire slots and are dispatched immediately (`SENT`). The remaining 300 jobs receive `allowed = False` and are rescheduled with Celery countdowns matching their `retry_after_seconds`.
2. **Retry Budget Preservation:** Rate-limit deferrals do **not** increment `provider_retries`. Deferrals are volume throttling events, not provider faults; tasks preserve their full 5-attempt retry budget and never land in the Dead-Letter Queue due to queue depth.
3. **Window 2 (T = 60s to 120s):** As initial reservations slide out of the 60-second window, capacity opens up. The next 200 deferred jobs execute and reach `SENT`.
4. **Window 3 (T = 120s to 180s):** The final 100 deferred jobs execute and reach `SENT`.
5. **Outcome:** Exactly 500 emails delivered, 0 duplicates, 0 dropped jobs, and 0 entries in the Dead-Letter Queue.

---

### 5. Worker Crash Reliability (SIGKILL Analysis)
- **`task_acks_late = True`:** Prevents job loss by holding message acknowledgment in Redis until task execution successfully completes.
- **`task_reject_on_worker_lost = True`:** Supervisor catches abrupt worker process deaths (`SIGKILL`, OOM) and issues a `NACK` with `requeue=True` to Redis.
- **`worker_prefetch_multiplier = 1`:** Workers take 1 task at a time, preventing worker starvation and task hoarding during bursts.
- **Idempotency Invariant:** Re-delivered tasks inspect `EmailDeliveryLog` where `idempotency_key` is marked `SENT`. If already sent, the task returns `ALREADY_SENT` without contacting the provider again.

---

## SECTION 3: WRITTEN ARCHITECTURE REVIEW

*(Per assessment guidelines, two questions have been selected: **Question B** and **Question C**, each strictly containing between 200 and 350 words.)*

---

### Question B: Offset vs Cursor Pagination
*(Word count: 292 words | Assessment limit: 200–350 words)*

Offset pagination calculates position using an index offset: `SELECT * FROM orders ORDER BY id LIMIT 20 OFFSET 10000` (in DRF, `PageNumberPagination`). Cursor pagination anchors retrieval to a unique, sequential pointer: `SELECT * FROM orders WHERE id < 8452 ORDER BY id DESC LIMIT 20` (in DRF, `CursorPagination`).

When an API scales to 10,000+ records, offset pagination degrades severely. Relational databases execute `OFFSET N` by scanning and sorting all $N$ records before discarding them, yielding $\mathcal{O}(N)$ sequential I/O cost and linear latency growth. Conversely, cursor pagination uses a B-Tree index seek directly on the cursor column, executing in $\mathcal{O}(\log N + M)$ constant time regardless of dataset size.

For mobile infinite scrolling, offset pagination causes severe data drift. Because offsets are positional rather than identity-based:
- **Insertions:** If a new record is created while a user scrolls from page 1 to page 2, existing rows shift downward. The last record of page 1 shifts into the first position of page 2, showing the mobile user a duplicate record.
- **Deletions:** If a record on page 1 is deleted, subsequent rows shift upward. The user skips a record entirely.

Cursor pagination guarantees strict consistency during real-time mutations. Because the client requests records relative to an immutable value (`created_at`, `id`), newly inserted records remain behind or ahead of the cursor without displacing the relative window.

**Decision Matrix:**
- **Choose Cursor Pagination:** For mobile feeds, infinite scrolling, high-concurrency event streams, and large datasets (>10,000 rows) where constant response time and insertion consistency are mandatory. Trade-off: clients cannot navigate directly to arbitrary page numbers (e.g., 'jump to page 45').
- **Choose Offset Pagination:** For internal dashboards and administrative tables with small datasets (<5,000 rows) where multi-column sorting, total page counts, and direct page navigation are required.

---

### Question C: File Upload Security
*(Word count: 302 words | Assessment limit: 200–350 words)*

A high-risk file upload endpoint exposes applications to severe vulnerabilities. Five distinct attack vectors and their Django application-layer mitigations include:

1. **Remote Code Execution via Executable Uploads:**
   - *Attack & Impact:* An attacker uploads a script (`shell.py`, `exploit.php`) executed by the web server or cron job, giving full server compromise.
   - *Django Mitigation:* Disallow user extensions using an explicit allowlist in serializer validation. Store uploads with server-generated UUIDs (`upload_to=uuid_filename`) outside web roots and mount media storage with execution flags disabled (`noexec`).

2. **MIME-Type & Extension Spoofing:**
   - *Attack & Impact:* Malicious binaries disguised with benign extensions (`exploit.jpg`) or spoofed `Content-Type: image/jpeg` bypass naive filename filters, executing during server-side processing or client-side sniffing.
   - *Django Mitigation:* Inspect file magic headers in memory using `python-magic` (`magic.from_buffer(file.read(2048), mime=True)`). For images, re-encode files via Pillow (`Image.open(file).save(...)`), stripping polyglot shellcode and verifying true structure.

3. **Path Traversal via Unsanitized Filenames:**
   - *Attack & Impact:* Supplying `filename="../../etc/cron.d/backdoor"` overwrites critical operating system files or configuration.
   - *Django Mitigation:* Pass names through `django.utils.text.get_valid_filename()`. Django's `FileSystemStorage` automatically resolves canonical paths and raises `SuspiciousFileOperation` if a file attempts to escape `MEDIA_ROOT`.

4. **Resource Exhaustion & Decompression Bombs:**
   - *Attack & Impact:* Massive multi-gigabyte uploads or nested zip/pixel bombs exhaust server memory, CPU, and disk, causing Denial of Service (DoS).
   - *Django Mitigation:* Enforce `DATA_UPLOAD_MAX_MEMORY_SIZE` and `FILE_UPLOAD_MAX_MEMORY_SIZE` in `settings.py`. Validate file bounds in Django forms (`if file.size > 10 * 1024 * 1024: raise ValidationError()`). Inspect image dimensions (`Image.open().size`) before full decoding.

5. **Cross-Site Scripting (XSS) via Vector Graphics (SVG):**
   - *Attack & Impact:* SVG files containing `<script>` or malicious XML entities (`<!ENTITY ...>`) execute in the victim's browser session or trigger XML External Entity (XXE) attacks.
   - *Django Mitigation:* Reject SVG uploads or sanitize XML payloads using `defusedxml` and `bleach`. Serve all user-generated media with HTTP header `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`.
