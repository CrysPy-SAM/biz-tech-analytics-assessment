# Biz-Tech Analytics: Full-Stack Developer Technical Assessment

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Django 6.1](https://img.shields.io/badge/django-6.1-green.svg)](https://www.djangoproject.com/)
[![Tests](https://img.shields.io/badge/pytest-passing-success.svg)](./section-1-django-debugging/orders/tests/)
[![Profiler](https://img.shields.io/badge/profiler-django--silk-purple.svg)](http://127.0.0.1:8000/silk/)

Comprehensive technical solution for the **Biz-Tech Analytics Full-Stack Developer Assessment**.

---

## Repository Structure

```
biz-tech-analytics-assessment/
│
├── section-1-django-debugging/          # Section 1: Broken System Diagnostics & Fix
│   ├── orders/                          # Django application (Models, Views, Serializers)
│   │   ├── models.py                    # Customer, Category, Product, Order, OrderItem
│   │   ├── views.py                     # Optimized & Unoptimized views + Benchmark view
│   │   ├── serializers.py               # Eager-loading & unoptimized DRF serializers
│   │   ├── urls.py                      # /api/orders/ routes
│   │   ├── admin.py                     # Optimized Django Admin interfaces
│   │   ├── management/commands/         # seed_orders management command
│   │   └── tests/                       # Pytest test suite (7 tests verifying N+1 fix)
│   ├── profiler-evidence/               # Genuine Django Silk evidence & SQL logs
│   │   ├── query_analysis.json          # Machine-readable before/after telemetry
│   │   ├── before_optimization_sql.txt  # 15,240 lines of unoptimized SQL queries
│   │   ├── after_optimization_sql.txt   # Exact SQL queries of optimized endpoint
│   │   ├── silk_benchmark_report.md     # In-depth benchmark analysis report
│   │   └── how_to_capture_silk_ui.md    # Guide to view Silk Web UI in browser
│   ├── generate_profiler_evidence.py    # Automated profiling harness
│   └── INVESTIGATION_LOG.md             # Detailed chronological post-mortem
│
├── section-2-job-queue/                 # Section 2: Async Job Queue (Prepared for Part 2)
│   └── README.md
│
├── config/                              # Django Project Configuration
│   ├── settings.py                      # Dynamic environment settings & Silk config
│   ├── urls.py                          # Root routing table (/api/orders/, /silk/, /admin/)
│   ├── wsgi.py
│   └── asgi.py
│
├── DESIGN.md                            # Comprehensive Architecture Design for Section 2
├── ANSWERS.md                           # Formal assessment written answers
├── requirements.txt                     # Pinned project dependencies
├── pytest.ini                           # Test runner configuration
├── docker-compose.yml                   # Multi-container Docker stack (Web, Postgres, Redis)
├── Dockerfile                           # Production Python 3.12 container image
├── .env.example                         # Environment variable template
├── .gitignore                           # Git ignore rules
└── README.md                            # Setup guide and quickstart documentation
```

---

## Quickstart: Run Locally in Under 5 Minutes

### Prerequisites
- Python 3.12+ (or Docker & Docker Compose)
- Git

### Option A: Local Python Environment (Fastest)

```bash
# 1. Clone or navigate into the project directory
cd biz-tech-analytics-assessment

# 2. Create and activate a virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
# source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy environment configuration
cp .env.example .env

# 5. Run database migrations
python manage.py migrate

# 6. Seed test customer and 250 orders (reproduces assessment scenario)
python manage.py seed_orders --orders 250

# 7. Start Django development server
python manage.py runserver
```

The application is now running at `http://127.0.0.1:8000/`.

---

### Option B: Docker Compose (PostgreSQL + Redis + Django)

```bash
cd biz-tech-analytics-assessment
docker compose up -d --build
docker compose exec web python manage.py seed_orders --orders 250
```

---

## Verifying Section 1 Implementation

### 1. Execute the Test Suite
Run the test suite via `pytest`:
```bash
pytest
```
*Expected result:* **7 tests passing**, verifying:
- `/api/orders/summary/` returns HTTP 200 and accurate payload schema.
- Unoptimized view demonstrates the $O(N \times M)$ query explosion (>1,000 queries).
- Optimized view enforces constant $O(1)$ database queries ($\le 3$ queries).
- 100% exact data parity between both implementations.
- Benchmark endpoint returns accurate telemetry.

### 2. View Django Silk Profiler Web UI
1. Ensure the server is running (`python manage.py runserver`).
2. Open your browser and navigate to:
   ```
   http://127.0.0.1:8000/silk/
   ```
3. Trigger the endpoints to inspect requests:
   - **Optimized (Production):** `http://127.0.0.1:8000/api/orders/summary/`
   - **Unoptimized (Reproduction):** `http://127.0.0.1:8000/api/orders/summary/unoptimized/`
   - **Diagnostic Benchmark:** `http://127.0.0.1:8000/api/orders/benchmark/`

---

## Section 1 Performance Summary

| Metric | Unoptimized (Pre-Fix) | Optimized (Post-Fix) | Improvement |
| :--- | :--- | :--- | :--- |
| **Endpoint** | `/api/orders/summary/unoptimized/` | `/api/orders/summary/` | Drop-in Fix |
| **Test Volume** | 250 orders | 250 orders | 100% Parity |
| **Database Queries** | **5,078 queries** | **11 queries (3 business SQL)** | **99.78% reduction** |
| **Execution Latency** | **29,353.71 ms (29.35s)** | **225.63 ms** | **99.23% faster** |

Full evidence, analysis, and SQL query logs are available in:
[`section-1-django-debugging/profiler-evidence/`](./section-1-django-debugging/profiler-evidence/)

---

## Verifying Section 2: Celery Asynchronous Job Queue

### 1. Start Celery Worker Daemon
In a separate terminal with virtual environment active:
```bash
# On Linux / macOS / Docker:
celery -A config worker --loglevel=info -Q celery,emails

# On Windows:
celery -A config worker --loglevel=info -P solo -Q celery,emails
```

### 2. Programmatically Dispatch Transactional Email Tasks
Open a Django shell (`python manage.py shell`):
```python
from tasks import send_transactional_email_task
from email_queue.models import EmailType

# Submit Order Confirmation email
res = send_transactional_email_task.delay(
    email_type=EmailType.ORDER_CONFIRMATION,
    recipient="customer@biztech.com",
    payload={"order_number": "ORD-2026-9901", "total_amount": 149.95, "items_count": 2},
    idempotency_key="order_confirm_9901",
)
print("Task ID:", res.id)
print("Task Status:", res.status)
print("Task Result:", res.get())
```

### 3. Inspect Task & Dead-Letter Telemetry
Failed or dead-lettered jobs can be inspected via Django Admin or Django ORM:
```python
from email_queue.models import EmailDeliveryLog, DeadLetterEmailJob

# View all delivery logs:
print(EmailDeliveryLog.objects.all())

# View all dead-lettered jobs (captures error reason, retry count, traceback, timestamp):
print(DeadLetterEmailJob.objects.all().values())
```
Or open Django Admin at `http://127.0.0.1:8000/admin/` and navigate to:
- **Email delivery logs** (`/admin/email_queue/emaildeliverylog/`)
- **Dead letter email jobs** (`/admin/email_queue/deadletteremailjob/`)

---

## Running the Automated Test Suite

Run the complete test suite across Section 1, Section 2, the Atomic Rate Limiter, and the 500-Job Stress Test:
```bash
pytest
```
*Current test suite:* **32 passed, 1 skipped in ~25.7 seconds**.

```
collected 33 items

section-1-django-debugging/orders/tests/test_orders_summary.py (7 tests) . PASSED
section-2-job-queue/tests/test_services.py (5 tests) .................... PASSED
section-2-job-queue/tests/test_idempotency.py (2 tests) ................. PASSED
section-2-job-queue/tests/test_dead_letter.py (1 test) .................. PASSED
section-2-job-queue/tests/test_worker_reliability.py (3 tests) .......... PASSED
section-2-job-queue/tests/test_email_tasks.py (6 tests) ................. PASSED
section-2-job-queue/tests/test_rate_limiter.py (5 tests) ................ PASSED
section-2-job-queue/tests/test_stress_500_jobs.py (3 tests) ............. PASSED
section-2-job-queue/tests/test_real_redis_integration.py (1 test) ....... SKIPPED (Docker Redis daemon offline)
```

### Running Specific Test Suites

```bash
# 1. Run the 500-Job Flash Sale Stress Test
pytest section-2-job-queue/tests/test_stress_500_jobs.py -v

# 2. Run the Atomic Sliding Window Rate Limiter Unit Tests
pytest section-2-job-queue/tests/test_rate_limiter.py -v

# 3. Run Real Redis Integration Tests (when Redis is running on localhost:6379 or Docker)
pytest section-2-job-queue/tests/test_real_redis_integration.py -v
```

---

## Inspecting Redis Rate Limit & Queue State

When running in Docker Compose or against a live Redis daemon:
```bash
# Inspect active reservations in the rolling 60-second window
redis-cli zcard email:rate_limit:provider

# View timestamps of the oldest and newest reservations
redis-cli zrange email:rate_limit:provider 0 -1 WITHSCORES

# Inspect key TTL (auto-cleans up after inactivity)
redis-cli pttl email:rate_limit:provider

# Inspect Dead-Letter Queue stream in Redis
redis-cli lrange dlq:transactional_emails 0 -1
```

Run the complete test suite across Section 1 and Section 2:
```bash
pytest
```
*Current test suite:* **24 passed tests in ~2.8 seconds**.

```
collected 24 items

section-1-django-debugging/orders/tests/test_orders_summary.py (7 tests) ... PASSED
section-2-job-queue/tests/test_services.py (5 tests) .................... PASSED
section-2-job-queue/tests/test_idempotency.py (2 tests) ................. PASSED
section-2-job-queue/tests/test_dead_letter.py (1 test) ................... PASSED
section-2-job-queue/tests/test_worker_reliability.py (3 tests) .......... PASSED
section-2-job-queue/tests/test_email_tasks.py (6 tests) ................. PASSED
```

---

## Architectural Documentation
- Section 1 Investigation & Post-Mortem: [`section-1-django-debugging/INVESTIGATION_LOG.md`](./section-1-django-debugging/INVESTIGATION_LOG.md)
- Section 2 Architecture & Queue Design: [`section-2-job-queue/README.md`](./section-2-job-queue/README.md)
- System Architecture Design: [`DESIGN.md`](./DESIGN.md)
- Written Assessment Answers: [`ANSWERS.md`](./ANSWERS.md)

