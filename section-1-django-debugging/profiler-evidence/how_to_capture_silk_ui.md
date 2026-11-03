# How to View Genuine Django Silk Profiler Telemetry in Browser

The Django Silk profiler is fully integrated, migrated, and active in this repository.

### Step 1: Start Django Development Server
```bash
python manage.py runserver
```

### Step 2: Open Django Silk UI
Navigate to:
```
http://127.0.0.1:8000/silk/
```

### Step 3: Trigger the API Endpoints
Open a new browser tab or run curl:
```bash
# 1. Trigger the optimized production endpoint:
curl http://127.0.0.1:8000/api/orders/summary/

# 2. Trigger the unoptimized reproduction endpoint:
curl http://127.0.0.1:8000/api/orders/summary/unoptimized/

# 3. View live side-by-side benchmark diagnostics:
curl http://127.0.0.1:8000/api/orders/benchmark/
```

### Step 4: Inspect Requests in Silk
In `http://127.0.0.1:8000/silk/`:
1. You will see both requests listed with their exact execution times and query counts.
2. Click on `/api/orders/summary/unoptimized/` to inspect the massive waterfall of 1,000+ individual queries.
3. Click on `/api/orders/summary/` to see exactly 2-3 queries executed in constant O(1) time.
