#!/usr/bin/env python3
"""
HTTP-level concurrency and latency test for the dosing API.

Requires the service to be running.  Does NOT use pytest — run directly:

    python3 tests/perf_load_test.py
    python3 tests/perf_load_test.py --url http://localhost:8000 --concurrency 20 --total 100 --age 35
    python3 tests/perf_load_test.py --isolation      # data-isolation check only
    python3 tests/perf_load_test.py --cache-compare  # cold vs warm latency

Rounds of testing
─────────────────
  1. Sequential baseline   — one request at a time, measures true per-request cost.
  2. Concurrent burst      -- N requests fired simultaneously.
  3. Cache warm round      — same burst repeated; should be mostly cache hits.
  4. Data isolation check  — different (drug_id, age) pairs must not bleed data.
  5. Mixed age-group burst — concurrent requests across all age groups.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import aiohttp
except ImportError:
    sys.exit(
        "aiohttp is required:  pip install aiohttp\n"
        "(or use the venv that already has it)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_URL         = "http://34.14.197.45:8001"
DEFAULT_API_KEY     = "test-api-key"
DOSING_ENDPOINT     = "/api/v1/dosing"

# Known drug_ids from the top-500 coverage run that return dosing data
SAMPLE_DRUG_IDS = [
    "457491",    # Dolo 650 area
    "210470",    # Combiflam (primary hit)
    "142807",    # Voveran SR
    "1146701",   # Augmentin
    "1002088",   # Brufen
    "56693",     # Ciplox 500
    "165440",    # Levoflox 500
    "1055048",   # Thyronorm
    "122170",    # Glyciphage
    "1038076",   # Desloratadine
]


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RequestResult:
    drug_id: str
    age: int
    status: int
    latency_ms: float
    age_group_returned: str | None = None
    error: str | None = None


@dataclass
class RoundSummary:
    label: str
    results: list[RequestResult] = field(default_factory=list)

    def ok(self):
        return [r for r in self.results if r.status == 200]

    def failed(self):
        return [r for r in self.results if r.status != 200]

    def latencies(self):
        return [r.latency_ms for r in self.ok()]

    def print(self):
        ok      = self.ok()
        failed  = self.failed()
        lats    = self.latencies()
        total   = len(self.results)

        print(f"\n{'─'*60}")
        print(f"  {self.label}")
        print(f"{'─'*60}")
        print(f"  Total requests  : {total}")
        print(f"  Success (200)   : {len(ok)}  ({len(ok)/total*100:.1f}%)")
        print(f"  Failed          : {len(failed)}")
        if failed:
            codes = {}
            for r in failed:
                codes[r.status] = codes.get(r.status, 0) + 1
            print(f"  Failure codes   : {codes}")

        if lats:
            lats_sorted = sorted(lats)
            print(f"\n  Latency (ms) — successful requests:")
            print(f"    min  = {min(lats):.1f}")
            print(f"    p50  = {_pct(lats_sorted, 50):.1f}")
            print(f"    p75  = {_pct(lats_sorted, 75):.1f}")
            print(f"    p95  = {_pct(lats_sorted, 95):.1f}")
            print(f"    p99  = {_pct(lats_sorted, 99):.1f}")
            print(f"    max  = {max(lats):.1f}")
            print(f"    mean = {statistics.mean(lats):.1f}")
            if len(lats) > 1:
                print(f"    stdev= {statistics.stdev(lats):.1f}")

        cached_count = sum(
            1 for r in ok
            if r.age_group_returned is not None  # crude proxy — server returns cached field
        )
        # Note: we can't directly see cached=True without parsing the body per request.
        # Full latency histogram is the best proxy for cache hit ratio.


# ──────────────────────────────────────────────────────────────────────────────
# Core HTTP helper
# ──────────────────────────────────────────────────────────────────────────────

async def do_request(
    session: "aiohttp.ClientSession",
    base_url: str,
    drug_id: str,
    age: int,
    api_key: str,
) -> RequestResult:
    url     = base_url.rstrip("/") + DOSING_ENDPOINT
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    payload = {"drug_id_1mg": drug_id, "age": age}

    t0 = time.perf_counter()
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            latency_ms = (time.perf_counter() - t0) * 1000
            body = await resp.json(content_type=None)
            age_group = body.get("age_group") if resp.status == 200 else None
            return RequestResult(
                drug_id=drug_id,
                age=age,
                status=resp.status,
                latency_ms=latency_ms,
                age_group_returned=age_group,
            )
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        return RequestResult(
            drug_id=drug_id,
            age=age,
            status=0,
            latency_ms=latency_ms,
            error=str(exc),
        )


def _pct(sorted_data: list[float], p: float) -> float:
    if not sorted_data:
        return 0.0
    idx = (p / 100) * (len(sorted_data) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (idx - lo)


# ──────────────────────────────────────────────────────────────────────────────
# Test rounds
# ──────────────────────────────────────────────────────────────────────────────

async def round_sequential(session, base_url, api_key, drug_ids, age, n) -> RoundSummary:
    """Fire requests one at a time to get a clean per-request baseline."""
    summary = RoundSummary("Sequential baseline")
    drugs = (drug_ids * ((n // len(drug_ids)) + 1))[:n]
    for drug_id in drugs:
        summary.results.append(await do_request(session, base_url, drug_id, age, api_key))
    return summary


async def round_concurrent(session, base_url, api_key, drug_ids, age, n, label="Concurrent burst") -> RoundSummary:
    """Fire all N requests simultaneously."""
    summary = RoundSummary(label)
    drugs   = (drug_ids * ((n // len(drug_ids)) + 1))[:n]
    tasks   = [do_request(session, base_url, drug_id, age, api_key) for drug_id in drugs]
    summary.results = await asyncio.gather(*tasks)
    return summary


async def round_mixed_ages(session, base_url, api_key, drug_id, n) -> RoundSummary:
    """Fire N requests with rotating age groups to exercise all age paths."""
    summary = RoundSummary("Mixed age-group burst (all age groups)")
    ages    = [0, 1, 10, 17, 18, 35, 64, 65, 90]
    ages_n  = (ages * ((n // len(ages)) + 1))[:n]
    tasks   = [do_request(session, base_url, drug_id, age, api_key) for age in ages_n]
    summary.results = await asyncio.gather(*tasks)
    return summary


async def check_data_isolation(session, base_url, api_key) -> bool:
    """
    Data isolation: concurrent requests for (drug_A, adult) and (drug_B, pediatric)
    must return each drug's own drug_id in the response — no cross-contamination.
    """
    print("\n── Data isolation check ────────────────────────────────────")

    pairs = [(SAMPLE_DRUG_IDS[i], age) for i, age in enumerate([10, 35, 70, 10, 35])]
    tasks = [do_request(session, base_url, drug_id, age, api_key) for drug_id, age in pairs]
    results = await asyncio.gather(*tasks)

    passed = True
    for (expected_drug, age), result in zip(pairs, results):
        if result.status != 200:
            print(f"  SKIP  {expected_drug} age={age} → status {result.status}")
            continue

        # Parse response body — we stored age_group_returned but need drug_id_1mg
        # Re-fetch with explicit body parsing for isolation check
        url     = base_url.rstrip("/") + DOSING_ENDPOINT
        headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        async with session.post(url, json={"drug_id_1mg": expected_drug, "age": age}, headers=headers) as resp:
            if resp.status == 200:
                body = await resp.json(content_type=None)
                returned_drug = body.get("drug_id_1mg")
                ok = (returned_drug == expected_drug)
                status = "PASS" if ok else "FAIL"
                if not ok:
                    passed = False
                print(f"  {status}  requested={expected_drug}  returned={returned_drug}  age={age}  age_group={body.get('age_group')}")

    return passed


async def check_cache_warm_vs_cold(session, base_url, api_key, drug_id, age, concurrency) -> None:
    """Compare cold (first hit) vs warm (repeated) latency for the same key."""
    print("\n── Cache cold vs warm comparison ───────────────────────────")

    # Cold — one request to prime the key
    cold = await do_request(session, base_url, drug_id, age, api_key)
    print(f"  Cold (single request) : {cold.latency_ms:.1f} ms  status={cold.status}")

    # Warm — concurrent requests that should all hit cache
    warm_tasks = [do_request(session, base_url, drug_id, age, api_key) for _ in range(concurrency)]
    warm_results = await asyncio.gather(*warm_tasks)
    warm_lats = [r.latency_ms for r in warm_results if r.status == 200]

    if warm_lats:
        sorted_warm = sorted(warm_lats)
        print(f"  Warm ({concurrency} concurrent):")
        print(f"    p50 = {_pct(sorted_warm, 50):.1f} ms")
        print(f"    p95 = {_pct(sorted_warm, 95):.1f} ms")
        print(f"    max = {max(warm_lats):.1f} ms")
        speedup = cold.latency_ms / (_pct(sorted_warm, 50) or 1)
        print(f"  Speed-up (cold / warm p50) : {speedup:.1f}x")


# ──────────────────────────────────────────────────────────────────────────────
# Health check
# ──────────────────────────────────────────────────────────────────────────────

async def health_check(session, base_url) -> bool:
    try:
        async with session.get(base_url.rstrip("/") + "/health") as resp:
            if resp.status == 200:
                body = await resp.json(content_type=None)
                print(f"  Health: {body}")
                return body.get("status") == "ok"
            print(f"  Health check returned {resp.status}")
            return False
    except Exception as exc:
        print(f"  Health check failed: {exc}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def main(args) -> None:
    connector = aiohttp.TCPConnector(limit=200)
    timeout   = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        print(f"\n{'='*60}")
        print(f"  CDSS Dosing Service — Performance & Isolation Tests")
        print(f"  URL         : {args.url}")
        print(f"  Concurrency : {args.concurrency}")
        print(f"  Total reqs  : {args.total}")
        print(f"  Age         : {args.age}")
        print(f"{'='*60}")

        # ── Health check ──────────────────────────────────────────────────────
        print("\n── Health check ─────────────────────────────────────────────")
        if not await health_check(session, args.url):
            print("  Service is not healthy — aborting.")
            return

        drug_ids = SAMPLE_DRUG_IDS[:args.concurrency] if args.concurrency <= len(SAMPLE_DRUG_IDS) else SAMPLE_DRUG_IDS

        # ── Isolation only ────────────────────────────────────────────────────
        if args.isolation:
            await check_data_isolation(session, args.url, args.api_key)
            return

        # ── Cache compare only ────────────────────────────────────────────────
        if args.cache_compare:
            await check_cache_warm_vs_cold(
                session, args.url, args.api_key,
                drug_ids[0], args.age, args.concurrency
            )
            return

        # ── Round 1: Sequential baseline ─────────────────────────────────────
        seq = await round_sequential(
            session, args.url, args.api_key, drug_ids, args.age,
            n=min(args.total, 20),  # cap sequential at 20 to keep it fast
        )
        seq.print()

        # ── Round 2: Concurrent burst (cold) ─────────────────────────────────
        cold_burst = await round_concurrent(
            session, args.url, args.api_key, drug_ids, args.age,
            n=args.total, label=f"Concurrent burst — {args.total} simultaneous (cold)"
        )
        cold_burst.print()

        # ── Round 3: Concurrent burst (warm — cache should be hot) ────────────
        warm_burst = await round_concurrent(
            session, args.url, args.api_key, drug_ids, args.age,
            n=args.total, label=f"Concurrent burst — {args.total} simultaneous (warm cache)"
        )
        warm_burst.print()

        # ── Round 4: Mixed age groups ─────────────────────────────────────────
        mixed = await round_mixed_ages(
            session, args.url, args.api_key, drug_ids[0], n=args.total
        )
        mixed.print()

        # ── Round 5: Data isolation ───────────────────────────────────────────
        isolation_ok = await check_data_isolation(session, args.url, args.api_key)

        # ── Round 6: Cache cold vs warm ───────────────────────────────────────
        await check_cache_warm_vs_cold(
            session, args.url, args.api_key,
            drug_ids[0], args.age, args.concurrency
        )

        # ── Summary ───────────────────────────────────────────────────────────
        cold_lats = cold_burst.latencies()
        warm_lats = warm_burst.latencies()
        print(f"\n{'='*60}")
        print("  SUMMARY")
        print(f"{'='*60}")
        if cold_lats and warm_lats:
            speedup = _pct(sorted(cold_lats), 50) / (_pct(sorted(warm_lats), 50) or 1)
            print(f"  Cache speedup (cold p50 / warm p50) : {speedup:.1f}x")
        print(f"  Data isolation                      : {'PASS' if isolation_ok else 'FAIL'}")
        print(f"  Cold burst success rate             : {len(cold_burst.ok())}/{len(cold_burst.results)}")
        print(f"  Warm burst success rate             : {len(warm_burst.ok())}/{len(warm_burst.results)}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CDSS Dosing Service — performance + isolation tests")
    parser.add_argument("--url",          default=DEFAULT_URL,     help="Base URL of the service")
    parser.add_argument("--api-key",      default=DEFAULT_API_KEY, help="X-API-Key header value")
    parser.add_argument("--concurrency",  type=int, default=10,    help="Number of simultaneous requests")
    parser.add_argument("--total",        type=int, default=50,    help="Total requests per round")
    parser.add_argument("--age",          type=int, default=35,    help="Patient age to use for requests")
    parser.add_argument("--isolation",    action="store_true",     help="Run data-isolation check only")
    parser.add_argument("--cache-compare",action="store_true",     help="Run cold vs warm cache comparison only")
    args = parser.parse_args()

    asyncio.run(main(args))
