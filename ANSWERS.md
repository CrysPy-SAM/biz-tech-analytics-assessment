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
*(Implementation reserved for Part 2 as instructed. Technical architecture documented in `DESIGN.md`.)*

---

## SECTION 3: WRITTEN ARCHITECTURE REVIEW
*(Will be finalized as part of comprehensive submission review.)*
