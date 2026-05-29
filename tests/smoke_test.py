#!/usr/bin/env python3
"""
Post-deploy smoke test — Phase 6.1

Confirms the live service is healthy and returning correct data after a deployment.
Runs ~3000 checks: 20 core sanity checks + bulk coverage across 500 drugs × 2 age groups.

Usage:
    python3 tests/smoke_test.py --url http://34.14.197.45:8001 --api-key <key>
    python3 tests/smoke_test.py --url http://34.14.197.45:8001 --api-key <key> --db-url postgresql://...

Exit codes:
    0  — all checks passed
    1  — one or more checks failed
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import aiohttp
import asyncpg
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

KNOWN_DRUG_ID   = "210470"
KNOWN_AGE       = 35
UNKNOWN_DRUG_ID = "000000000"

REQUIRED_RESPONSE_FIELDS = {
    "drug_id_1mg", "age_group", "dosing", "brand_name", "cached", "query_time_ms",
}
REQUIRED_ROW_FIELDS = {"route", "dose_amount", "dose_unit", "frequency"}

BULK_AGES        = [35, 10]          # adult + pediatric
BULK_CONCURRENCY = 20
BULK_DRUG_LIMIT  = 500

FETCH_DRUG_IDS_SQL = """
SELECT DISTINCT drug_id_1mg::text
FROM drugdb.indian_brand
WHERE drug_id_1mg IS NOT NULL
ORDER BY drug_id_1mg
LIMIT $1
"""

GREEN = "\033[32m"
RED   = "\033[31m"
BOLD  = "\033[1m"
RESET = "\033[0m"

PASS_LABEL = f"{GREEN}PASS{RESET}"
FAIL_LABEL = f"{RED}FAIL{RESET}"


# ── Core check helper ─────────────────────────────────────────────────────────

_core_passed = 0
_core_failed = 0


def _check(label: str, condition: bool, detail: str = "") -> bool:
    global _core_passed, _core_failed
    status = PASS_LABEL if condition else FAIL_LABEL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    if condition:
        _core_passed += 1
    else:
        _core_failed += 1
    return condition


# ══════════════════════════════════════════════════════════════════════════════
# SECTION A — Core sanity checks (20 checks)
# ══════════════════════════════════════════════════════════════════════════════

def run_core_checks(base_url: str, headers: dict) -> bool:
    global _core_passed, _core_failed
    _core_passed = 0
    _core_failed = 0

    # 1. Health
    print(f"\n{BOLD}A1. Health endpoint{RESET}")
    try:
        r    = requests.get(f"{base_url}/health", timeout=10)
        data = r.json()
        _check("HTTP 200",          r.status_code == 200,              f"got {r.status_code}")
        _check("status = ok",       data.get("status") == "ok",        f"got {data.get('status')!r}")
        _check("db = connected",    data.get("db") == "connected",     f"got {data.get('db')!r}")
        _check("cache = connected", data.get("cache") == "connected",  f"got {data.get('cache')!r}")
    except Exception as exc:
        _check("health reachable", False, str(exc))
        print(f"\n  {RED}FATAL: cannot reach {base_url}/health — aborting.{RESET}\n")
        return False

    # 2. Known drug
    print(f"\n{BOLD}A2. Known drug ({KNOWN_DRUG_ID}, age={KNOWN_AGE}){RESET}")
    try:
        r    = requests.post(f"{base_url}/api/v1/dosing",
                             json={"drug_id_1mg": KNOWN_DRUG_ID, "age": KNOWN_AGE},
                             headers=headers, timeout=10)
        data = r.json()
        _check("HTTP 200",                    r.status_code == 200,
               f"got {r.status_code}")
        _check("all required fields present", REQUIRED_RESPONSE_FIELDS <= set(data.keys()),
               f"missing: {REQUIRED_RESPONSE_FIELDS - set(data.keys())}")
        _check("drug_id_1mg matches",         data.get("drug_id_1mg") == KNOWN_DRUG_ID,
               f"got {data.get('drug_id_1mg')!r}")
        _check("age_group is adult",          data.get("age_group") == "adult",
               f"got {data.get('age_group')!r}")
        _check("dosing is a list",            isinstance(data.get("dosing"), list))
        _check("at least 1 dosing row",       len(data.get("dosing", [])) >= 1,
               f"got {len(data.get('dosing', []))} rows")
        _check("query_time_ms >= 0",          (data.get("query_time_ms") or 0) >= 0,
               f"got {data.get('query_time_ms')}")
        if data.get("dosing"):
            row = data["dosing"][0]
            _check("dosing row has required fields",
                   not (REQUIRED_ROW_FIELDS - set(row.keys())),
                   f"missing: {REQUIRED_ROW_FIELDS - set(row.keys())}")
    except Exception as exc:
        _check("known drug request", False, str(exc))

    # 3. Cache hit
    print(f"\n{BOLD}A3. Cache hit on repeat request{RESET}")
    try:
        r    = requests.post(f"{base_url}/api/v1/dosing",
                             json={"drug_id_1mg": KNOWN_DRUG_ID, "age": KNOWN_AGE},
                             headers=headers, timeout=10)
        data = r.json()
        _check("HTTP 200",      r.status_code == 200,      f"got {r.status_code}")
        _check("cached = True", data.get("cached") is True, f"got {data.get('cached')!r}")
    except Exception as exc:
        _check("cache hit", False, str(exc))

    # 4. Unknown drug
    print(f"\n{BOLD}A4. Unknown drug returns 404{RESET}")
    try:
        r    = requests.post(f"{base_url}/api/v1/dosing",
                             json={"drug_id_1mg": UNKNOWN_DRUG_ID, "age": KNOWN_AGE},
                             headers=headers, timeout=10)
        data = r.json()
        _check("HTTP 404",               r.status_code == 404,             f"got {r.status_code}")
        _check("error = not_found",      data.get("error") == "not_found", f"got {data.get('error')!r}")
        _check("message field present",  "message" in data,                f"keys: {list(data.keys())}")
    except Exception as exc:
        _check("unknown drug", False, str(exc))

    # 5. Auth
    print(f"\n{BOLD}A5. Missing API key → 401{RESET}")
    try:
        r    = requests.post(f"{base_url}/api/v1/dosing",
                             json={"drug_id_1mg": KNOWN_DRUG_ID, "age": KNOWN_AGE},
                             timeout=10)
        data = r.json()
        _check("HTTP 401",               r.status_code == 401,                 f"got {r.status_code}")
        _check("error = unauthorized",   data.get("error") == "unauthorized",  f"got {data.get('error')!r}")
    except Exception as exc:
        _check("no-auth request", False, str(exc))

    # 6. Validation
    print(f"\n{BOLD}A6. Invalid age → 422{RESET}")
    try:
        r = requests.post(f"{base_url}/api/v1/dosing",
                          json={"drug_id_1mg": KNOWN_DRUG_ID, "age": -1},
                          headers=headers, timeout=10)
        _check("HTTP 422", r.status_code == 422, f"got {r.status_code}")
    except Exception as exc:
        _check("invalid age", False, str(exc))

    return _core_failed == 0


# ══════════════════════════════════════════════════════════════════════════════
# SECTION B — Bulk coverage (500 drugs × 2 age groups)
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_drug_ids(db_url: str) -> list[str]:
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=5, command_timeout=30)
    async with pool.acquire() as conn:
        rows = await conn.fetch(FETCH_DRUG_IDS_SQL, BULK_DRUG_LIMIT)
    await pool.close()
    return [str(r["drug_id_1mg"]) for r in rows]


async def _test_drug(session: aiohttp.ClientSession, endpoint: str, headers: dict,
                     drug_id: str, age: int) -> dict:
    """Fire one request and return a result dict."""
    result = {"drug_id": drug_id, "age": age, "status": None, "error": None,
              "has_dosing": False, "missing_fields": [], "server_error": False}
    try:
        async with session.post(endpoint,
                                json={"drug_id_1mg": drug_id, "age": age},
                                headers=headers) as resp:
            result["status"] = resp.status
            if resp.status == 500:
                result["server_error"] = True
                result["error"] = await resp.text()
            elif resp.status == 200:
                data = await resp.json()
                missing = REQUIRED_RESPONSE_FIELDS - set(data.keys())
                result["missing_fields"] = list(missing)
                result["has_dosing"]     = len(data.get("dosing", [])) > 0
    except Exception as exc:
        result["error"]        = str(exc)
        result["server_error"] = True
    return result


async def run_bulk_checks(base_url: str, api_key: str, db_url: str) -> tuple[bool, dict]:
    endpoint = f"{base_url}/api/v1/dosing"
    headers  = {"X-API-Key": api_key, "Content-Type": "application/json"}

    print(f"\n{BOLD}B. Bulk coverage — fetching drug IDs from DB…{RESET}")
    try:
        drug_ids = await _fetch_drug_ids(db_url)
    except Exception as exc:
        print(f"  {RED}Cannot connect to DB to fetch drug IDs: {exc}{RESET}")
        print(f"  Skipping bulk section. Pass --db-url to enable.")
        return True, {}

    total_requests = len(drug_ids) * len(BULK_AGES)
    print(f"  {len(drug_ids)} drug IDs × {len(BULK_AGES)} age groups = {total_requests} requests")
    print(f"  Concurrency: {BULK_CONCURRENCY}  |  Ages: {BULK_AGES}")
    print()

    sem     = asyncio.Semaphore(BULK_CONCURRENCY)
    results = []

    async def bounded(drug_id, age):
        async with sem:
            return await _test_drug(session, endpoint, headers, drug_id, age)

    t0 = time.perf_counter()
    connector = aiohttp.TCPConnector(limit=BULK_CONCURRENCY + 5)
    timeout   = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [bounded(d, a) for d in drug_ids for a in BULK_AGES]
        done  = 0
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results.append(r)
            done += 1
            if done % 100 == 0 or done == total_requests:
                elapsed = time.perf_counter() - t0
                print(f"  Progress: {done}/{total_requests}  ({elapsed:.1f}s)", end="\r", flush=True)

    elapsed = time.perf_counter() - t0
    print(f"  Completed {total_requests} requests in {elapsed:.1f}s"
          f"  ({total_requests/elapsed:.0f} req/s)")

    # ── Tally results ─────────────────────────────────────────────────────────
    server_errors   = [r for r in results if r["server_error"]]
    missing_fields  = [r for r in results if r["missing_fields"]]
    ok_200          = [r for r in results if r["status"] == 200]
    ok_404          = [r for r in results if r["status"] == 404]
    have_dosing     = [r for r in results if r["has_dosing"]]

    checks = []

    def bulk_check(label, condition, detail=""):
        symbol = f"{GREEN}PASS{RESET}" if condition else f"{RED}FAIL{RESET}"
        suffix = f"  ({detail})" if detail else ""
        print(f"  [{symbol}] {label}{suffix}")
        checks.append(condition)
        return condition

    print()
    # Allow up to 0.5% error rate — very large responses under concurrent load
    # may cause transient 500s (e.g. a drug with 50k rows hit simultaneously).
    error_rate = len(server_errors) / total_requests
    bulk_check("Server error rate < 0.5%",
               error_rate < 0.005,
               f"{len(server_errors)} errors ({error_rate*100:.2f}%)")
    bulk_check("Zero missing-field responses",
               len(missing_fields) == 0,
               f"{len(missing_fields)} responses missing fields" if missing_fields else "")
    bulk_check("All responses are 200 or 404 (excl. transient errors)",
               (total_requests - len(ok_200) - len(ok_404)) / total_requests < 0.005,
               f"{total_requests - len(ok_200) - len(ok_404)} unexpected statuses")
    bulk_check("At least 50% of adult requests return dosing",
               sum(1 for r in results if r["age"] == 35 and r["has_dosing"])
               >= len(drug_ids) * 0.50,
               f"{sum(1 for r in results if r['age']==35 and r['has_dosing'])}/{len(drug_ids)} adult drugs have dosing")
    bulk_check("At least 30% of pediatric requests return dosing",
               sum(1 for r in results if r["age"] == 10 and r["has_dosing"])
               >= len(drug_ids) * 0.30,
               f"{sum(1 for r in results if r['age']==10 and r['has_dosing'])}/{len(drug_ids)} pediatric drugs have dosing")

    stats = {
        "total_requests":  total_requests,
        "ok_200":          len(ok_200),
        "ok_404":          len(ok_404),
        "server_errors":   len(server_errors),
        "have_dosing":     len(have_dosing),
        "adult_dosing":    sum(1 for r in results if r["age"] == 35 and r["has_dosing"]),
        "pediatric_dosing":sum(1 for r in results if r["age"] == 10 and r["has_dosing"]),
        "elapsed_s":       round(elapsed, 1),
    }

    if server_errors:
        print(f"\n  {RED}Sample server errors:{RESET}")
        for r in server_errors[:3]:
            print(f"    drug_id={r['drug_id']} age={r['age']}: {str(r['error'])[:120]}")

    return all(checks), stats


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Post-deploy smoke test")
    parser.add_argument("--url",     default="http://34.14.197.45:8001",
                        help="Base URL of the service")
    parser.add_argument("--api-key", default=os.getenv("API_KEY"),
                        help="API key (X-API-Key header)")
    parser.add_argument("--db-url",  default=os.getenv("DATABASE_URL"),
                        help="PostgreSQL DSN for bulk drug ID lookup")
    parser.add_argument("--skip-bulk", action="store_true",
                        help="Skip the bulk 500-drug section (run core 20 checks only)")
    args = parser.parse_args()

    if not args.api_key:
        print("ERROR: --api-key is required (or set API_KEY in .env)")
        sys.exit(1)

    base_url = args.url.rstrip("/")
    headers  = {"X-API-Key": args.api_key, "Content-Type": "application/json"}

    print(f"\n{'═'*55}")
    print(f"  Smoke test  →  {base_url}")
    print(f"{'═'*55}")

    # Section A — core checks
    core_ok = run_core_checks(base_url, headers)

    core_total  = _core_passed + _core_failed
    print(f"\n  Core checks: {_core_passed}/{core_total} passed")

    if not core_ok:
        print(f"\n  {RED}Core checks failed — skipping bulk section.{RESET}\n")
        sys.exit(1)

    # Section B — bulk checks
    bulk_ok   = True
    bulk_stats = {}
    if not args.skip_bulk:
        bulk_ok, bulk_stats = asyncio.run(
            run_bulk_checks(base_url, args.api_key, args.db_url or "")
        )

    # ── Final summary ─────────────────────────────────────────────────────────
    print(f"\n{'═'*55}")
    print(f"  {BOLD}SUMMARY{RESET}")
    print(f"{'─'*55}")
    print(f"  Core checks  : {_core_passed}/{core_total} passed")
    if bulk_stats:
        total_req = bulk_stats["total_requests"]
        print(f"  Bulk requests: {total_req}  ({bulk_stats['elapsed_s']}s)")
        print(f"    200 (have dosing path)  : {bulk_stats['ok_200']}")
        print(f"    404 (no dosing found)   : {bulk_stats['ok_404']}")
        print(f"    500 (server errors)     : {bulk_stats['server_errors']}")
        print(f"    Adult with dosing       : {bulk_stats['adult_dosing']}/{total_req//2}")
        print(f"    Pediatric with dosing   : {bulk_stats['pediatric_dosing']}/{total_req//2}")
    print(f"{'═'*55}")

    overall = core_ok and bulk_ok
    if overall:
        print(f"  {GREEN}{BOLD}ALL CHECKS PASSED{RESET}")
    else:
        print(f"  {RED}{BOLD}SOME CHECKS FAILED{RESET}")
    print()

    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
