# Django Silk Profiler Benchmark Report: /api/orders/summary/

## Executive Summary

| Metric | Unoptimized (Pre-Fix) | Optimized (Post-Fix) | Improvement |
| :--- | :--- | :--- | :--- |
| **Endpoint** | `/api/orders/summary/unoptimized/` | `/api/orders/summary/` | Clean drop-in fix |
| **Total Orders** | 250 orders | 250 orders | Identical test volume |
| **Database Queries** | **5078 queries** | **11 queries** | **99.78% reduction** |
| **Wall Clock Latency** | **29353.71 ms** | **225.63 ms** | **99.23% faster** |
| **Data Parity** | Equivalent | Equivalent | **100% Exact Match** |
| **Django Silk ID** | `4e293118-d426-4543-a2bb-e5d051cb5c2d` | `2293d062-d441-4cd0-a5be-1652c3b95eb9` | Recorded in Silk DB |

---

## Detailed Root Cause Analysis

### 1. The Incident Mechanism
Under normal circumstances with 5 to 10 orders, the unoptimized view required ~50 database queries, completing within ~80ms in staging.
However, when a user had 200+ orders:
1. `Order.objects.filter(customer=customer)` fetched 250 orders in 1 query.
2. For every order (250 times), DRF's serializer accessed:
   - `order.customer.name` -> Triggered `SELECT ... FROM customers WHERE id = ...` (250 individual queries).
   - `order.items.count()` -> Triggered `SELECT COUNT(*) FROM order_items WHERE order_id = ...` (250 individual queries).
   - `order.items.all()` -> Triggered `SELECT ... FROM order_items WHERE order_id = ...` (250 queries).
   - For every order item (approx 889 items), accessed `item.product.name` and `item.product.category.name`, triggering additional queries for each product and category.
3. Total queries exploded to **5078 queries** for a single HTTP request! Over a production network or connection pool with 10–15ms per round trip, 2,000+ queries took > 30 seconds, causing Gateway Timeouts (504).

---

## The Solution & ORM Mechanics

```python
# Optimized Queryset
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
        annotated_total_amount=Sum(F("items__quantity") * F("items__unit_price")),
    )
    .order_by("-created_at")
)
```

### Why This Fix Works
1. **`select_related("customer")`**:
   Performs an SQL `INNER JOIN` between `orders` and `customers`. Resolves the customer entity in the initial query without any secondary round trip.
2. **`prefetch_related(Prefetch("items", queryset=OrderItem.objects.select_related("product__category")))`**:
   Executes exactly **one** secondary SQL query using `WHERE order_items.order_id IN (...)` joined with `products` and `categories` in SQL (`INNER JOIN products INNER JOIN categories`). Django matches these records in memory.
3. **`annotate(Count, Sum)`**:
   Computes count and total monetary sum directly inside the database SQL engine using `COUNT(DISTINCT order_items.id)` and `SUM(quantity * unit_price)`.

Total SQL queries drops from **5078** to **11**!

---

## Profiler Telemetry Files
- Machine readable summary: [`query_analysis.json`](./query_analysis.json)
- Full unoptimized SQL statement log: [`before_optimization_sql.txt`](./before_optimization_sql.txt)
- Full optimized SQL statement log: [`after_optimization_sql.txt`](./after_optimization_sql.txt)
