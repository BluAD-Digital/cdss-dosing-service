# CDSS Dosing Service — Production Readiness Test Plan

## How Claude should use this file

> At the start of every session, read this file first.
> Find the **first item whose status is `[ ]` (todo) or `[!]` (failed)**.
> Build that test, run it, record the result in the Results section at the bottom,
> update the checkbox, then ask the user if they want to continue to the next item.
> Never skip items. Never mark an item `[x]` unless the test actually ran and passed.

---

## Environment

| Thing | Value |
|---|---|
| Working directory | `/home/nathanivikas890_gmail_com/cdss-dosing-service` |
| Python | `python3` |
| Run tests with | `python3 -m pytest <file> -v` |
| Docker service | `cdss-dosing-service-dosing-service-1` |
| API base URL | `http://34.14.197.45:8001` |
| API key | read from `.env` → `API_KEY` |
| DB URL | read from `.env` → `DATABASE_URL` |
| Live logs | `docker logs cdss-dosing-service-dosing-service-1 -f` |

---

## Status Legend

| Symbol | Meaning |
|---|---|
| `[ ]` | Not started yet |
| `[~]` | In progress |
| `[x]` | Done and passing |
| `[!]` | Done but failing — needs fixing |
| `[-]` | Skipped (with reason) |

---

## PHASE 1 — Fix What Is Broken

### 1.1 Fix `test_dosing_router.py` (collection error)
- **Status:** `[x]`
- **Problem:** `DosingRow` is constructed without `frequency_meaning` (required field) and `DosingResponse` without `formulation_id`. This crashes pytest at collection time, blocking ALL tests from running together.
- **File to fix:** `tests/test_dosing_router.py`
- **Fix:** Add `frequency_meaning=None` to every `DosingRow(...)` call and `formulation_id="1001"` to `DosingResponse(...)`.
- **Run:** `python3 -m pytest tests/test_dosing_router.py -v`
- **Pass condition:** 0 errors, all tests collected and passing.

---

## PHASE 2 — Functional Tests

### 2.1 Unit tests — full suite runs clean together
- **Status:** `[x]`
- **Goal:** All mocked unit tests pass in one `pytest` invocation with no errors.
- **Run:**
  ```bash
  python3 -m pytest tests/test_age_mapper.py \
                    tests/test_dosing_service.py \
                    tests/test_dosing_router.py \
                    tests/test_age_group_coverage.py \
                    tests/test_concurrency.py -v
  ```
- **Pass condition:** 0 failures, 0 errors.

### 2.2 Fix `test_dosing_service.py` (4 pre-existing failures)
- **Status:** `[x]`
- **Problem:** 4 tests fail because they don't patch `drug_exists` and use a wrong pool mock. Also `test_cache_hit_skips_repo` uses a cached payload missing `formulation_id`.
- **File to fix:** `tests/test_dosing_service.py`
- **Fix:** Add `patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True))` to each failing test; add `formulation_id` to the cached payload dict.
- **Run:** `python3 -m pytest tests/test_dosing_service.py -v`
- **Pass condition:** All 4 previously failing tests now pass.

### 2.3 Integration tests — real DB + real Redis, no mocks
- **Status:** `[x]`
- **Goal:** Tests that hit the actual PostgreSQL database and Redis. Verify the full path from Python → SQL → DB → Redis works end to end.
- **Create file:** `tests/test_integration.py`
- **What to test:**
  - `fetch_dosing` returns rows for a known drug (e.g. `drug_id_1mg="210470"`, age=35)
  - `fetch_dosing` returns empty for an unknown drug id
  - `fetch_dosing_fallback` works for a drug only reachable via the fallback path
  - `drug_exists` returns True for known drug, False for unknown
  - Cache is written to Redis after a DB hit and read back correctly on second call
  - Response for adult (age=35) has `age_group="adult"` in the cache key
  - Response for pediatric (age=10) has `age_group="pediatric"` in the cache key
- **Run:** `python3 -m pytest tests/test_integration.py -v`
- **Pass condition:** All integration tests pass against the real DB.

### 2.4 Input validation tests
- **Status:** `[x]`
- **Goal:** Verify the API rejects bad inputs cleanly.
- **Create file:** `tests/test_input_validation.py`
- **What to test:**
  - `age = -1` → 422
  - `age = 121` → 422
  - `age = 0` → 200 or 404 (valid input, neonate)
  - `age = 120` → 200 or 404 (valid input, geriatric)
  - Missing `drug_id_1mg` field → 422
  - Missing `age` field → 422
  - `drug_id_1mg = ""` (empty string) → 422 or 404
  - `drug_id_1mg = "   "` (whitespace only) → 422 or 404
  - `drug_id_1mg` with 1000 characters → 422 or handled gracefully
  - `age = "abc"` (string not int) → 422
  - Extra unknown fields in payload → ignored (200/404)
- **Run:** `python3 -m pytest tests/test_input_validation.py -v`
- **Pass condition:** All validation edge cases return the correct HTTP status.

### 2.5 SQL injection test
- **Status:** `[x]`
- **Goal:** Confirm parameterized queries protect against injection.
- **Add to:** `tests/test_input_validation.py` (new section)
- **What to test — send these as `drug_id_1mg`:**
  - `"1'; DROP TABLE drugdb.dosing_regimen; --"`
  - `"1 OR 1=1"`
  - `"' UNION SELECT * FROM pg_tables --"`
  - `"../../etc/passwd"`
  - `"<script>alert(1)</script>"`
- **Expected:** Returns 200 or 404 (no data) — never a 500, never leaks DB error text in the response body.
- **Run:** `python3 -m pytest tests/test_input_validation.py::test_sql_injection -v`
- **Pass condition:** All injection strings return 200 or 404, never 500.

---

## PHASE 3 — Reliability / Fault Tolerance

### 3.1 Redis down — service falls through to DB
- **Status:** `[x]`
- **Goal:** When Redis is unreachable, the service still returns dosing data (from DB) instead of crashing.
- **Create file:** `tests/test_fault_tolerance.py`
- **How to test:**
  - Mock `redis.get` to raise `ConnectionError`
  - Call `get_dosing` — expect a valid `DosingResponse` (not a 500)
  - Verify the response has correct data (came from DB, not cache)
- **Run:** `python3 -m pytest tests/test_fault_tolerance.py::test_redis_down_falls_through_to_db -v`
- **Pass condition:** Valid response returned even when Redis raises `ConnectionError`.

### 3.2 Redis down — cache write failure is silent
- **Status:** `[x]`
- **Add to:** `tests/test_fault_tolerance.py`
- **How to test:**
  - Mock `redis.set` to raise `ConnectionError`
  - Call `get_dosing` — expect a valid `DosingResponse`
  - Confirm the error is logged as a warning, not raised
- **Pass condition:** Response is returned successfully; no exception propagates.

### 3.3 DB down — returns 500 with correct error shape
- **Status:** `[x]`
- **Add to:** `tests/test_fault_tolerance.py`
- **How to test:**
  - Mock `dosing_repo.fetch_dosing` to raise `asyncpg.PostgresError`
  - Call `get_dosing` — expect `HTTPException` with `status_code=500`
  - Confirm response body has `{"error": "internal_error", "message": "..."}`
- **Pass condition:** 500 returned; no raw DB error text leaks in the response.

### 3.4 Slow DB — timeout is respected
- **Status:** `[x]`
- **Add to:** `tests/test_fault_tolerance.py`
- **How to test:**
  - Mock `dosing_repo.fetch_dosing` to `await asyncio.sleep(35)` (longer than `POOL_COMMAND_TIMEOUT=10`)
  - Verify the request eventually returns (500 or timeout error) within a reasonable wall-clock time
- **Pass condition:** Service does not hang indefinitely.

### 3.5 DB down — verify in Docker (live test)
- **Status:** `[x]`
- **Goal:** Stop the real DB container and hit the API — confirm it returns 500, not a crash.
- **How to test (manual):**
  ```bash
  # Stop DB access (block the DB port temporarily)
  docker exec cdss-dosing-service-dosing-service-1 \
    curl -s -X POST http://localhost:8001/api/v1/dosing \
    -H "X-API-Key: <key>" \
    -d '{"drug_id_1mg":"210470","age":35}'
  # Then restore
  ```
- **Pass condition:** Returns `{"error": "internal_error", ...}` with status 500. Service process does not crash.

### 3.6 Connection pool exhaustion
- **Status:** `[x]`
- **Add to:** `tests/test_fault_tolerance.py`
- **How to test:**
  - Send 100 simultaneous concurrent requests using `perf_load_test.py --concurrency 100 --total 100`
  - Verify no requests hang indefinitely
  - Verify no 500 errors caused by pool exhaustion
- **Run:** `python3 tests/perf_load_test.py --url http://localhost:8001 --api-key <key> --concurrency 100 --total 100`
- **Pass condition:** All requests complete (200 or 404), none timeout, no 500s.

---

## PHASE 4 — Security

### 4.1 Authentication edge cases
- **Status:** `[x]`
- **Add to:** `tests/test_dosing_router.py` (already partial)
- **What to test:**
  - No `X-API-Key` header → 401 with `{"error": "unauthorized"}`
  - Wrong key → 401
  - Empty string key → 401
  - Key with extra whitespace → 401
  - Correct key → passes through to 200/404
  - Health endpoint needs no key → 200
- **Run:** `python3 -m pytest tests/test_dosing_router.py -k "auth or api_key" -v`
- **Pass condition:** All auth edge cases return the correct status.

### 4.2 Sensitive data not leaked in logs
- **Status:** `[x]`
- **Goal:** Confirm the API key, raw SQL errors, and DB connection strings never appear in logs.
- **How to test:**
  - Send a request with a bad API key
  - Send a request that triggers a DB error (mock)
  - Check `docker logs` output for the API key string and DB URL
- **Run:** `docker logs cdss-dosing-service-dosing-service-1 2>&1 | grep -i "api_key\|DATABASE_URL\|password"`
- **Pass condition:** Zero matches.

### 4.3 Rate limiting (if implemented)
- **Status:** `[-]`
- **Note:** Rate limiting is not currently implemented in this service. Skip until it is added. When added, test: 1000 requests in 1 second from same IP → 429 responses after limit.

---

## PHASE 5 — Observability

### 5.1 Health endpoint is accurate
- **Status:** `[x]`
- **Add to:** `tests/test_dosing_router.py`
- **What to test:**
  - `/health` with DB and Redis both up → `{"status":"ok","db":"connected","cache":"connected"}`
  - `/health` with Redis mocked as down → `{"status":"degraded","cache":"disconnected"}` with HTTP 503
  - `/health` with DB mocked as down → `{"status":"degraded","db":"disconnected"}` with HTTP 503
- **Pass condition:** All three scenarios return the correct shape and status code.

### 5.2 Logs contain required fields per request
- **Status:** `[x]`
- **How to test:**
  - Make one real HTTP request
  - Parse `docker logs` output
  - Verify each log line for that request contains: `request_id`, `timestamp`, `level`, `event`
  - Verify the HTTP log line contains: `method`, `path`, `status_code`, `latency_ms`
- **Run:** Bash script to parse docker logs JSON output
- **Pass condition:** All required fields present in every log line.

---

## PHASE 6 — Operational

### 6.1 Post-deploy smoke test script
- **Status:** `[x]` ✓ verified 2026-05-28
- **Goal:** A single script that can be run after every deployment to confirm the service is alive and returning correct data.
- **Create file:** `tests/smoke_test.py`
- **What to check:**
  - `/health` returns `{"status":"ok"}`
  - A known drug (e.g. `210470`, age=35) returns 200 with correct fields
  - A known non-existent drug returns 404 with `{"error":"not_found"}`
  - Response schema has all required fields: `drug_id_1mg`, `age_group`, `dosing`, `brand_name`, `cached`, `query_time_ms`
  - `query_time_ms > 0`
  - At least 1 dosing row in `dosing` array
- **Run:** `python3 tests/smoke_test.py --url http://localhost:8001 --api-key <key>`
- **Pass condition:** All checks pass; script exits 0.

### 6.2 Graceful shutdown test
- **Status:** `[x]`
- **Goal:** In-flight requests complete before the container stops.
- **How to test:**
  - Send a slow request (mocked 2s DB latency)
  - While it is in flight, send `docker stop` to the container
  - Verify the in-flight request still completes (not aborted mid-response)
- **Pass condition:** In-flight request returns a complete response before the container exits.

---

## PHASE 7 — Code Quality

### 7.1 Code coverage report
- **Status:** `[x]`
- **Goal:** Measure what percentage of app code is exercised by the test suite. Target: ≥ 80%.
- **Install:** `pip install pytest-cov`
- **Run:**
  ```bash
  python3 -m pytest tests/test_age_mapper.py \
                    tests/test_dosing_service.py \
                    tests/test_dosing_router.py \
                    tests/test_age_group_coverage.py \
                    tests/test_concurrency.py \
                    --cov=app --cov-report=term-missing -v
  ```
- **Pass condition:** Overall coverage ≥ 80%. Any module below 60% flagged for improvement.

### 7.2 Soak test — sustained load for 10 minutes
- **Status:** `[x]`
- **Goal:** No memory leak, no connection pool degradation, no error rate increase over time.
- **Run:**
  ```bash
  python3 tests/perf_load_test.py \
    --url http://localhost:8001 \
    --api-key <key> \
    --concurrency 10 \
    --total 500
  ```
- **Watch during run:** `docker stats cdss-dosing-service-dosing-service-1`
- **Pass condition:**
  - Memory stays flat (no steady upward trend)
  - p95 latency does not increase more than 2x from start to end
  - Error rate stays at same level throughout (no degradation over time)

---

## PHASE 8 — Age Group Coverage (Data Quality)

### 8.1 Pediatric coverage (age=10)
- **Status:** `[x]`
- **Run:** `python3 tests/test_top500_drugs.py --age 10`
- **Result:** 338/500 have dosing (67.6%) — 245 primary, 93 fallback, 103 not found, 59 not in DB.

### 8.2 Adult coverage (age=35)
- **Status:** `[x]`
- **Run:** `python3 tests/test_top500_drugs.py --age 35`
- **Pass condition:** Run completes for all 500 drugs. Record summary.

### 8.3 Geriatric coverage (age=70)
- **Status:** `[x]`
- **Run:** `python3 tests/test_top500_drugs.py --age 70`
- **Pass condition:** Run completes for all 500 drugs. Record summary.

### 8.4 Neonate coverage (age=0)
- **Status:** `[x]`
- **Run:** `python3 tests/test_top500_drugs.py --age 0`
- **Pass condition:** Run completes for all 500 drugs. Record summary. (Expected: very low % due to no `any` fallback group.)

### 8.5 Infant coverage (age=1)
- **Status:** `[x]`
- **Run:** `python3 tests/test_top500_drugs.py --age 1`
- **Pass condition:** Run completes for all 500 drugs. Record summary.

---

## Results Log

> Claude: append results here as each test is completed.

| # | Test | Status | Date | Result summary |
|---|---|---|---|---|
| 2.1 (partial) | Unit tests — age_mapper, age_group_coverage, concurrency | `[x]` | 2026-05-28 | 84 tests passing |
| 2.1 (partial) | test_dosing_service | `[!]` | 2026-05-28 | 4 failures — missing drug_exists patch + formulation_id in cache payload |
| 2.1 (partial) | test_dosing_router | `[!]` | 2026-05-28 | Collection error — missing frequency_meaning + formulation_id in SAMPLE_RESPONSE |
| 8.1 | Pediatric coverage age=10 | `[x]` | 2026-05-28 | 338/500 have dosing (67.6%) — 245 primary, 93 fallback |
| Perf | HTTP load test — 20 req, concurrency=10 | `[x]` | 2026-05-28 | Sequential p50=3.5ms, Concurrent cold p50=36.7ms, Warm p50=35.3ms, Isolation PASS |
| Concurrency | test_concurrency.py | `[x]` | 2026-05-28 | 17/17 passing — isolation, cache keys, round-trip, thundering-herd, error isolation |

---

## Known Gaps (not yet tests)

These are confirmed logic bugs found during testing — fix before production:

| Gap | Location | Impact |
|---|---|---|
| `dose_basis = 'fixed'` excludes weight-based dosing | `queries/dosing.sql:75`, `queries/dosing_fallback.sql:93` | Pediatric / neonate dosing often weight-based — silently returns empty |
| `administration_notes NOT ILIKE '%pediatric%'` excludes pediatric rows | `queries/dosing.sql:78`, `queries/dosing_fallback.sql:96` | Discards valid pediatric dosing notes |
| `age_to_groups(0)` returns `["neonate"]` — no `"any"` fallback | `app/utils/age_mapper.py:2` | Neonates always get 404 if DB has no neonate-specific rows |
| `age_to_groups(1)` returns `["infant","neonate"]` — no `"any"` fallback | `app/utils/age_mapper.py:4` | Same problem for infants |
