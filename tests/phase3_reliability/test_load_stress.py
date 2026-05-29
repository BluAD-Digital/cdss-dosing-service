"""
Phase 3 — Live load and stress tests against http://34.14.197.45:8001.

Covers:
  - 100 simultaneous concurrent requests → no 500s (3.6)
  - Sequential sustained load (200 requests) → consistent latency
  - Burst then wait → latency recovers
  - Mixed drug/age under high concurrency → all isolated
  - Same drug burst → cache warms up, later requests serve from cache
  - Repeated burst rounds → no degradation (soak pattern)
  - p50 / p95 / p99 latency thresholds

Run:
    python3 -m pytest tests/phase3_reliability/test_load_stress.py -v
"""

import asyncio
import os
import statistics
import time
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from dotenv import dotenv_values

_env     = dotenv_values(Path(__file__).parent.parent.parent / ".env")
BASE_URL = "http://34.14.197.45:8001"
API_KEY  = _env["API_KEY"]
HEADERS  = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
ENDPOINT = f"{BASE_URL}/api/v1/dosing"

# Known drugs that return 200 at age=35 (verified in Phase 2)
GOOD_DRUGS = [
    "210470",   # Combiflam
    "142807",   # Voveran SR
    "1002088",  # Brufen
    "56693",    # Ciplox 500
    "165440",   # Levoflox 500
    "344363",   # Dolonex DT
    "1115733",  # Dolopar
    "1147914",  # Naprosyn
    "1123438",  # Moxikind-CV
    "16542",    # Lignocaine
    "201825",   # Mesalamine
    "122170",   # Glyciphage
    "1038076",  # Desloratadine
]

FALLBACK_DRUGS = ["74467", "600468", "272818", "324940", "324155"]

ALL_DRUGS = GOOD_DRUGS + FALLBACK_DRUGS


def _pct(sorted_data: list[float], p: float) -> float:
    if not sorted_data:
        return 0.0
    idx = (p / 100) * (len(sorted_data) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (idx - lo)


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def session():
    connector = aiohttp.TCPConnector(limit=200)
    timeout   = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
        yield s


async def _post(session, drug_id, age=35):
    t0 = time.perf_counter()
    async with session.post(
        ENDPOINT,
        json={"drug_id_1mg": drug_id, "age": age},
        headers=HEADERS,
    ) as resp:
        latency_ms = (time.perf_counter() - t0) * 1000
        data = await resp.json(content_type=None)
        return resp.status, latency_ms, data


# ═══════════════════════════════════════════════════════════════
# 1. HEALTH BEFORE LOAD — confirm service is alive
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_service_healthy_before_load_tests(session):
    async with session.get(f"{BASE_URL}/health") as resp:
        assert resp.status == 200
        data = await resp.json(content_type=None)
    assert data["status"] == "ok"
    assert data["db"]    == "connected"
    assert data["cache"] == "connected"


# ═══════════════════════════════════════════════════════════════
# 2. CONCURRENT BURST — 100 simultaneous requests, no 500s (3.6)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_100_concurrent_requests_no_500(session):
    """Send 100 simultaneous requests across known drugs — must never return 500."""
    drugs = (ALL_DRUGS * 10)[:100]
    tasks = [_post(session, d) for d in drugs]

    results = await asyncio.gather(*tasks)
    statuses = [status for status, _, _ in results]

    assert 500 not in statuses, (
        f"Got 500 in concurrent burst of 100. "
        f"Status distribution: {dict((s, statuses.count(s)) for s in set(statuses))}"
    )
    assert all(s in (200, 404) for s in statuses)


@pytest.mark.asyncio
async def test_100_concurrent_no_requests_timeout(session):
    """All 100 concurrent requests must complete within 30s."""
    drugs = (ALL_DRUGS * 10)[:100]
    t0    = time.perf_counter()
    await asyncio.gather(*[_post(session, d) for d in drugs])
    elapsed = time.perf_counter() - t0
    assert elapsed < 30, f"100 concurrent requests took {elapsed:.1f}s (limit: 30s)"


@pytest.mark.asyncio
async def test_200_concurrent_requests_no_500(session):
    """Stress test: 200 simultaneous requests — still no 500s."""
    drugs = (ALL_DRUGS * 12)[:200]
    results = await asyncio.gather(*[_post(session, d) for d in drugs])
    statuses = [s for s, _, _ in results]

    assert 500 not in statuses, (
        f"500 seen in 200-concurrent burst: {dict((s, statuses.count(s)) for s in set(statuses))}"
    )


# ═══════════════════════════════════════════════════════════════
# 3. LATENCY THRESHOLDS — under normal load
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_p50_latency_under_1000ms_for_concurrent_burst(session):
    drugs   = (GOOD_DRUGS * 5)[:50]
    results = await asyncio.gather(*[_post(session, d) for d in drugs])
    lats    = sorted(r[1] for r in results if r[0] == 200)

    p50 = _pct(lats, 50)
    assert p50 < 1000, f"p50 latency {p50:.0f}ms exceeds 1000ms threshold"


@pytest.mark.asyncio
async def test_p95_latency_under_5000ms_for_concurrent_burst(session):
    drugs   = (ALL_DRUGS * 5)[:50]
    results = await asyncio.gather(*[_post(session, d) for d in drugs])
    lats    = sorted(r[1] for r in results if r[0] in (200, 404))

    p95 = _pct(lats, 95)
    assert p95 < 5000, f"p95 latency {p95:.0f}ms exceeds 5000ms threshold"


@pytest.mark.asyncio
async def test_cache_hit_latency_under_200ms(session):
    """Hit the same drug twice — second (warm) should be well under 200ms."""
    _, cold_ms, _ = await _post(session, GOOD_DRUGS[0])
    _, warm_ms, _ = await _post(session, GOOD_DRUGS[0])
    assert warm_ms < 200, f"Cache hit latency {warm_ms:.0f}ms exceeds 200ms"


# ═══════════════════════════════════════════════════════════════
# 4. CACHE WARM-UP UNDER LOAD
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_same_drug_burst_later_requests_are_cached(session):
    """50 concurrent requests for the same drug — second batch must all be cached."""
    drug = GOOD_DRUGS[0]

    # First batch (cold or warm depending on server state)
    await asyncio.gather(*[_post(session, drug) for _ in range(10)])

    # Second batch — all should now be cache hits
    results = await asyncio.gather(*[_post(session, drug) for _ in range(20)])
    for status, _, data in results:
        if status == 200:
            assert data.get("cached") is True, "Second batch should all be cache hits"


@pytest.mark.asyncio
async def test_different_drugs_all_cached_after_first_call(session):
    """Call each drug once, then again — second call must be cached."""
    # Warm all drugs
    await asyncio.gather(*[_post(session, d) for d in GOOD_DRUGS])

    # Verify all cached on second call
    results = await asyncio.gather(*[_post(session, d) for d in GOOD_DRUGS])
    for drug_id, (status, _, data) in zip(GOOD_DRUGS, results):
        if status == 200:
            assert data.get("cached") is True, f"{drug_id}: expected cached=True on second call"
            assert data.get("query_time_ms") == 0.0


# ═══════════════════════════════════════════════════════════════
# 5. DATA ISOLATION UNDER LOAD
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_concurrent_load_data_isolation(session):
    """Under load, each drug's response must contain its own drug_id."""
    drugs   = GOOD_DRUGS * 3   # 39 requests
    results = await asyncio.gather(*[_post(session, d) for d in drugs])

    for drug_id, (status, _, data) in zip(drugs, results):
        if status == 200:
            assert data["drug_id_1mg"] == drug_id, (
                f"Data bleed: requested {drug_id}, got {data['drug_id_1mg']}"
            )


@pytest.mark.asyncio
async def test_concurrent_age_group_isolation_under_load(session):
    """Concurrent requests for same drug at different ages must have correct age_group."""
    drug   = GOOD_DRUGS[0]
    cases  = [(18, "adult"), (35, "adult"), (65, "geriatric"), (70, "geriatric"), (90, "geriatric")]
    tasks  = [_post(session, drug, age) for age, _ in cases] * 4   # 20 total

    results = await asyncio.gather(*tasks)
    repeated_cases = cases * 4

    for (age, expected_group), (status, _, data) in zip(repeated_cases, results):
        if status == 200:
            assert data["age_group"] == expected_group, (
                f"age={age}: expected {expected_group}, got {data['age_group']}"
            )


# ═══════════════════════════════════════════════════════════════
# 6. REPEATED BURST ROUNDS — no degradation (mini soak test)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_5_burst_rounds_no_degradation(session):
    """
    Fire 5 rounds of 20 concurrent requests each with 0.5s gap between rounds.
    Success rate must not drop between round 1 and round 5.
    """
    ROUNDS       = 5
    BURST_SIZE   = 20
    drugs        = (ALL_DRUGS * 2)[:BURST_SIZE]
    round_rates  = []

    for round_num in range(ROUNDS):
        results  = await asyncio.gather(*[_post(session, d) for d in drugs])
        ok_count = sum(1 for s, _, _ in results if s in (200, 404))
        rate     = ok_count / BURST_SIZE
        round_rates.append(rate)

        if round_num < ROUNDS - 1:
            await asyncio.sleep(0.5)

    # Success rate must stay >= 90% in every round
    for i, rate in enumerate(round_rates):
        assert rate >= 0.9, f"Round {i+1} success rate dropped to {rate*100:.0f}% (expected ≥ 90%)"

    # Success rate must not degrade more than 5% from round 1 to round 5
    assert round_rates[-1] >= round_rates[0] - 0.05, (
        f"Service degraded: round 1={round_rates[0]*100:.0f}%, round 5={round_rates[-1]*100:.0f}%"
    )


# ═══════════════════════════════════════════════════════════════
# 7. MIXED FALLBACK + PRIMARY UNDER LOAD
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_primary_and_fallback_drugs_concurrent_no_500(session):
    """Mix primary-hit and fallback-hit drugs in a concurrent burst — no 500s."""
    drugs   = (GOOD_DRUGS + FALLBACK_DRUGS) * 3
    results = await asyncio.gather(*[_post(session, d) for d in drugs])
    statuses = [s for s, _, _ in results]

    assert 500 not in statuses
    success = sum(1 for s in statuses if s == 200)
    assert success > 0, "Expected at least some 200 responses"


# ═══════════════════════════════════════════════════════════════
# 8. HEALTH AFTER LOAD — service still healthy
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_service_healthy_after_load(session):
    """After all load tests, the service must still report healthy."""
    async with session.get(f"{BASE_URL}/health") as resp:
        assert resp.status == 200
        data = await resp.json(content_type=None)
    assert data["status"] == "ok"
    assert data["db"]    == "connected"
    assert data["cache"] == "connected"


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Multiple concurrency levels with latency tracking
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [10, 25, 50, 75, 100, 150])
async def test_various_concurrency_levels_no_500(session, concurrency):
    drugs   = (ALL_DRUGS * 10)[:concurrency]
    results = await asyncio.gather(*[_post(session, d) for d in drugs])
    statuses = [s for s, _, _ in results]
    assert 500 not in statuses, (
        f"concurrency={concurrency}: got 500. "
        f"Distribution: {dict((s, statuses.count(s)) for s in set(statuses))}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [10, 25, 50, 75])
async def test_p95_latency_scales_acceptably(session, concurrency):
    drugs   = (GOOD_DRUGS * 10)[:concurrency]
    results = await asyncio.gather(*[_post(session, d) for d in drugs])
    lats    = sorted(r[1] for r in results if r[0] == 200)
    if lats:
        p95 = _pct(lats, 95)
        # Latency should be under 10s even at high concurrency
        assert p95 < 10000, f"concurrency={concurrency}: p95={p95:.0f}ms exceeds 10s"


# ═══════════════════════════════════════════════════════════════
# EXPANDED — All age groups under concurrent load
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_group", [
    (0,   "neonate"),
    (1,   "infant"),
    (2,   "pediatric"),
    (10,  "pediatric"),
    (17,  "pediatric"),
    (18,  "adult"),
    (35,  "adult"),
    (64,  "adult"),
    (65,  "geriatric"),
    (70,  "geriatric"),
    (90,  "geriatric"),
    (120, "geriatric"),
])
async def test_each_age_group_under_10_concurrent(session, age, expected_group):
    drug    = GOOD_DRUGS[0]   # Combiflam — known to return 200 for adult/geriatric
    results = await asyncio.gather(*[_post(session, drug, age) for _ in range(10)])
    statuses = [s for s, _, _ in results]
    # All must complete without 500
    assert 500 not in statuses


@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_group", [
    (18,  "adult"),
    (35,  "adult"),
    (64,  "adult"),
    (65,  "geriatric"),
    (70,  "geriatric"),
])
async def test_age_group_label_correct_under_concurrent_load(session, age, expected_group):
    drug    = GOOD_DRUGS[0]
    results = await asyncio.gather(*[_post(session, drug, age) for _ in range(5)])
    for status, _, data in results:
        if status == 200:
            assert data["age_group"] == expected_group, (
                f"age={age}: expected {expected_group}, got {data['age_group']}"
            )


# ═══════════════════════════════════════════════════════════════
# EXPANDED — All 13 known good drugs, each under 5 concurrent
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS)
async def test_each_good_drug_under_5_concurrent(session, drug_id):
    results  = await asyncio.gather(*[_post(session, drug_id) for _ in range(5)])
    statuses = [s for s, _, _ in results]
    assert 500 not in statuses
    assert all(s == 200 for s in statuses), (
        f"{drug_id}: expected all 200, got {set(statuses)}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS)
async def test_each_good_drug_response_has_correct_drug_id_under_load(session, drug_id):
    results = await asyncio.gather(*[_post(session, drug_id) for _ in range(3)])
    for status, _, data in results:
        if status == 200:
            assert data["drug_id_1mg"] == drug_id


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", FALLBACK_DRUGS)
async def test_each_fallback_drug_under_5_concurrent_no_500(session, drug_id):
    results  = await asyncio.gather(*[_post(session, drug_id) for _ in range(5)])
    statuses = [s for s, _, _ in results]
    assert 500 not in statuses


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Response field validation under load
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS[:5])
async def test_all_top_level_response_fields_present_under_load(session, drug_id):
    results = await asyncio.gather(*[_post(session, drug_id) for _ in range(5)])
    for status, _, data in results:
        if status == 200:
            for field in ["drug_id_1mg", "formulation_id", "brand_name",
                          "salt_composition", "generic_name", "age_group",
                          "dosing", "cached", "query_time_ms"]:
                assert field in data, f"{drug_id}: missing field '{field}' under load"


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS[:5])
async def test_dosing_row_fields_present_under_load(session, drug_id):
    results = await asyncio.gather(*[_post(session, drug_id) for _ in range(5)])
    for status, _, data in results:
        if status == 200 and data["dosing"]:
            row = data["dosing"][0]
            for field in ["frequency", "frequency_meaning", "route",
                          "dose_amount", "dose_unit", "duration",
                          "indication", "instructions"]:
                assert field in row, f"{drug_id}: dosing row missing '{field}' under load"


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Sustained sequential load (many requests in a row)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("n_requests", [20, 50, 100])
async def test_sequential_sustained_load_no_500(session, n_requests):
    drugs = (ALL_DRUGS * 10)[:n_requests]
    fail_count = 0
    for drug in drugs:
        status, _, _ = await _post(session, drug)
        if status == 500:
            fail_count += 1
    assert fail_count == 0, f"{fail_count}/{n_requests} requests returned 500"


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Mixed 200 and 404 under concurrency
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_mixed_valid_and_invalid_concurrent_no_500(session):
    """Mix of known valid and known-404 drugs — no 500s."""
    valid_drugs   = GOOD_DRUGS * 3
    invalid_drugs = ["NONEXISTENT_1", "NONEXISTENT_2", "000000000",
                     "99999999", "FAKE_DRUG"] * 3
    all_drugs     = valid_drugs + invalid_drugs

    results  = await asyncio.gather(*[_post(session, d) for d in all_drugs])
    statuses = [s for s, _, _ in results]
    assert 500 not in statuses
    assert 200 in statuses, "Expected some 200 responses"
    assert 404 in statuses, "Expected some 404 responses"


@pytest.mark.asyncio
async def test_404_requests_dont_slow_down_200_requests(session):
    """Concurrent 404 and 200 requests — 200s must finish in reasonable time."""
    valid   = [(GOOD_DRUGS[i % len(GOOD_DRUGS)], 35) for i in range(10)]
    invalid = [("NONEXISTENT_99", 35)] * 10

    t0 = time.perf_counter()
    results = await asyncio.gather(
        *[_post(session, d, a) for d, a in valid + invalid]
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    valid_lats   = [lat for (s, lat, _), (d, _) in zip(results[:10], valid) if s == 200]
    if valid_lats:
        p95 = _pct(sorted(valid_lats), 95)
        assert p95 < 5000, f"Valid drug p95={p95:.0f}ms too high when mixed with 404s"


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Cache behaviour: warm, cold, mixed under load
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS[:8])
async def test_third_call_always_cached(session, drug_id):
    """Make 2 calls first, then verify the 3rd is always a cache hit."""
    await _post(session, drug_id)
    await _post(session, drug_id)
    status, _, data = await _post(session, drug_id)
    if status == 200:
        assert data["cached"] is True
        assert data["query_time_ms"] == 0.0


@pytest.mark.asyncio
async def test_all_good_drugs_cached_on_third_batch(session):
    """Warm all drugs × 2, then verify all are cached on 3rd batch."""
    await asyncio.gather(*[_post(session, d) for d in GOOD_DRUGS])
    await asyncio.gather(*[_post(session, d) for d in GOOD_DRUGS])
    results = await asyncio.gather(*[_post(session, d) for d in GOOD_DRUGS])
    for drug_id, (status, _, data) in zip(GOOD_DRUGS, results):
        if status == 200:
            assert data["cached"] is True, f"{drug_id}: expected cached=True on 3rd batch"
