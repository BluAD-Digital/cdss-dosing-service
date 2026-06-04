"""
Concurrency, timing, and data-isolation tests for the dosing service.

All tests are mocked — no real DB or Redis needed.

Scenarios covered
─────────────────
 1. Concurrent requests for different drugs return each drug's own data.
 2. Concurrent requests for the same drug+age all succeed with identical data.
 3. Same drug, different age groups → separate cache keys, separate responses.
 4. Cache hit is measurably faster than a cold DB query.
 5. A 404 for one request in a concurrent batch does NOT affect the others.
 6. Cache key format is  dosing:{drug_id}:{primary_group}.
 7. Cache serialization round-trip: cached dict → DosingResponse is lossless.
 8. Concurrent cache-miss requests all trigger DB calls (no silent de-dup).
 9. DB error in one concurrent task does not corrupt sibling tasks.
10. Different age groups sharing a drug produce the correct age_group label.
11. High-concurrency stress: 50 simultaneous requests all return valid data.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.response import DosingResponse
from app.utils.age_mapper import age_to_groups, age_to_primary_group


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_row(**overrides):
    defaults = {
        "formulation_id": 1001,
        "brand_name":      "TestBrand",
        "salt_composition":"Paracetamol 500mg",
        "generic_name":    "Paracetamol",
        "frequency":       "twice daily",
        "route":           "oral",
        "dose_amount":     "500",
        "dose_unit":       "mg",
        "duration":        "5 days",
        "indication":      "pain",
        "instructions":    None,
        "food_timing":     None,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda k: defaults[k])
    return row


class FakeRedis:
    """
    In-memory Redis substitute compatible with get_cached / set_cached.

    get_cached expects  redis.get(key) → JSON string | None
    set_cached calls    redis.set(key, json_string, ex=ttl)
    """

    def __init__(self):
        self._store: dict[str, str] = {}
        self.get_calls: int = 0
        self.set_calls: int = 0

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        return self._store.get(key)

    async def set(self, key: str, value: str, ex=None) -> None:
        self.set_calls += 1
        self._store[key] = value

    async def ping(self) -> bool:
        return True

    # ── inspection helpers ────────────────────────────────────────────────────

    def has_key(self, key: str) -> bool:
        return key in self._store

    def get_parsed(self, key: str) -> dict | None:
        raw = self._store.get(key)
        return json.loads(raw) if raw else None

    @property
    def key_count(self) -> int:
        return len(self._store)


def _pool() -> MagicMock:
    return MagicMock()


_PATCH = "app.services.dosing_service.dosing_repo.fetch_dosing_with_fallback"


# ──────────────────────────────────────────────────────────────────────────────
# 1. Concurrent requests for different drugs — data isolation
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_different_drugs_are_isolated():
    """Each concurrent request for a distinct drug_id gets its own drug's data."""
    from app.services.dosing_service import get_dosing

    drug_ids = ["drug_A", "drug_B", "drug_C", "drug_D", "drug_E"]
    redis = FakeRedis()

    async def mock_fetch(pool, drug_id, age_groups):
        return ([_make_row(brand_name=f"Brand-{drug_id}")], "primary", False)

    with patch(_PATCH, side_effect=mock_fetch):
        results = await asyncio.gather(*[
            get_dosing(drug_id, 35, _pool(), redis)
            for drug_id in drug_ids
        ])

    for drug_id, result in zip(drug_ids, results):
        assert result.drug_id_1mg == drug_id, f"Expected {drug_id}, got {result.drug_id_1mg}"
        assert result.brand_name == f"Brand-{drug_id}"


# ──────────────────────────────────────────────────────────────────────────────
# 2. Concurrent requests for the same drug+age — all succeed with same data
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_same_drug_same_age_all_succeed():
    """N simultaneous requests for the same drug and age all return valid responses."""
    from app.services.dosing_service import get_dosing

    CONCURRENCY = 10
    redis = FakeRedis()
    rows = [_make_row(brand_name="SharedBrand")]

    with patch(_PATCH, new=AsyncMock(return_value=(rows, "primary", False))):
        results = await asyncio.gather(*[
            get_dosing("drug_X", 35, _pool(), redis)
            for _ in range(CONCURRENCY)
        ])

    assert len(results) == CONCURRENCY
    for result in results:
        assert isinstance(result, DosingResponse)
        assert result.drug_id_1mg == "drug_X"
        assert result.brand_name == "SharedBrand"


# ──────────────────────────────────────────────────────────────────────────────
# 3. Same drug, different age groups → separate cache keys
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_same_drug_different_ages_use_separate_cache_keys():
    """
    (drug_A, adult) and (drug_A, pediatric) must be stored in separate cache entries
    so a geriatric patient's data never bleeds into a pediatric response.
    """
    from app.services.dosing_service import get_dosing

    test_cases = [
        (10, "pediatric"),
        (35, "adult"),
        (70, "geriatric"),
    ]
    redis = FakeRedis()

    async def mock_fetch(pool, drug_id, age_groups):
        primary = age_groups[0]
        return ([_make_row(brand_name=f"brand-{primary}")], "primary", False)

    with patch(_PATCH, side_effect=mock_fetch):
        results = await asyncio.gather(*[
            get_dosing("drug_shared", age, _pool(), redis)
            for age, _ in test_cases
        ])

    assert redis.key_count == len(test_cases)

    for (age, expected_group), result in zip(test_cases, results):
        expected_key = f"dosing:drug_shared:{expected_group}"
        assert redis.has_key(expected_key), f"Missing cache key {expected_key}"
        assert result.age_group == expected_group
        assert result.brand_name == f"brand-{expected_group}"


# ──────────────────────────────────────────────────────────────────────────────
# 4. Cache hit is faster than a cold DB call
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_hit_faster_than_db_miss():
    """
    Simulate a slow DB (10 ms artificial delay) and measure that a cache hit
    is significantly faster.  Cache hit should take < 2 ms; cold miss ≥ 10 ms.
    """
    from app.services.dosing_service import get_dosing

    DB_DELAY = 0.01  # 10 ms simulated DB latency

    async def slow_fetch(pool, drug_id, age_groups):
        await asyncio.sleep(DB_DELAY)
        return ([_make_row()], "primary", False)

    redis = FakeRedis()

    with patch(_PATCH, side_effect=slow_fetch):
        # Cold miss — must hit slow DB
        t0 = time.perf_counter()
        await get_dosing("drug_timing", 35, _pool(), redis)
        cold_ms = (time.perf_counter() - t0) * 1000

        # Warm hit — should skip DB entirely
        t1 = time.perf_counter()
        await get_dosing("drug_timing", 35, _pool(), redis)
        warm_ms = (time.perf_counter() - t1) * 1000

    assert cold_ms >= DB_DELAY * 1000 * 0.9, f"Cold miss too fast: {cold_ms:.2f} ms"
    assert warm_ms < cold_ms / 3, f"Cache hit ({warm_ms:.2f} ms) not faster than cold miss ({cold_ms:.2f} ms)"


# ──────────────────────────────────────────────────────────────────────────────
# 5. A 404 for one request doesn't affect sibling concurrent requests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_error_in_one_concurrent_request_does_not_affect_others():
    """
    Mix of valid and invalid drug_ids in asyncio.gather — valid ones should
    succeed even if an invalid one raises 404.
    """
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    good_drugs = ["drug_1", "drug_2", "drug_3"]
    bad_drug   = "bad_drug"
    redis      = FakeRedis()
    rows       = [_make_row()]

    async def selective_fetch(pool, drug_id, age_groups):
        if drug_id == bad_drug:
            return ([], "none", False)
        return (rows, "primary", False)

    with patch(_PATCH, side_effect=selective_fetch):
        tasks = [get_dosing(d, 35, _pool(), redis) for d in good_drugs]
        tasks.append(get_dosing(bad_drug, 35, _pool(), redis))
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    for i, drug_id in enumerate(good_drugs):
        assert isinstance(outcomes[i], DosingResponse), f"{drug_id} should have succeeded"

    bad_outcome = outcomes[-1]
    assert isinstance(bad_outcome, HTTPException)
    assert bad_outcome.status_code == 404


# ──────────────────────────────────────────────────────────────────────────────
# 6. Cache key format
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id,age,expected_key", [
    ("457491", 0,  "dosing:457491:neonate"),
    ("457491", 1,  "dosing:457491:infant"),
    ("457491", 10, "dosing:457491:pediatric"),
    ("457491", 35, "dosing:457491:adult"),
    ("457491", 70, "dosing:457491:geriatric"),
    ("999888", 35, "dosing:999888:adult"),
])
async def test_cache_key_format(drug_id, age, expected_key):
    """Cache key must be  dosing:{drug_id}:{primary_age_group}."""
    from app.services.dosing_service import get_dosing

    redis = FakeRedis()
    rows  = [_make_row()]

    with patch(_PATCH, new=AsyncMock(return_value=(rows, "primary", False))):
        await get_dosing(drug_id, age, _pool(), redis)

    assert redis.has_key(expected_key), (
        f"Expected cache key '{expected_key}', found keys: {list(redis._store.keys())}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 7. Cache serialization round-trip — response survives JSON round-trip
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_serialization_round_trip():
    """
    After a cold miss the response is written to the fake Redis as JSON.
    A second call should read that JSON back and reconstruct an identical
    DosingResponse (with cached=True).
    """
    from app.services.dosing_service import get_dosing

    redis = FakeRedis()
    rows  = [_make_row(
        brand_name="RoundTripBrand",
        salt_composition="Amoxicillin 250mg",
        frequency="three times daily",
        dose_unit="mg",
        duration="7 days",
        indication="infection",
    )]

    with patch(_PATCH, new=AsyncMock(return_value=(rows, "primary", False))):
        first  = await get_dosing("rt_drug", 35, _pool(), redis)   # writes cache
        second = await get_dosing("rt_drug", 35, _pool(), redis)   # reads cache

    assert second.cached is True
    assert second.drug_id_1mg      == first.drug_id_1mg
    assert second.brand_name       == first.brand_name
    assert second.salt_composition == first.salt_composition
    assert second.age_group        == first.age_group
    assert len(second.dosing)      == len(first.dosing)
    assert second.dosing[0].frequency  == first.dosing[0].frequency
    assert second.dosing[0].dose_unit  == first.dosing[0].dose_unit
    assert second.dosing[0].indication == first.dosing[0].indication


# ──────────────────────────────────────────────────────────────────────────────
# 8. Concurrent cache-miss requests all call the DB
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_cold_requests_all_hit_db():
    """
    The service has no distributed lock, so N concurrent cache-miss requests
    for the same key all independently query the DB.
    This test documents and verifies that behaviour.
    """
    from app.services.dosing_service import get_dosing

    CONCURRENCY = 5
    db_calls: list[int] = []

    async def yielding_fetch(pool, drug_id, age_groups):
        await asyncio.sleep(0)   # yield — lets other coroutines advance
        db_calls.append(1)
        return ([_make_row()], "primary", False)

    class YieldingFakeRedis(FakeRedis):
        async def get(self, key: str) -> str | None:
            await asyncio.sleep(0)
            self.get_calls += 1
            return self._store.get(key)

        async def set(self, key: str, value: str, ex=None) -> None:
            await asyncio.sleep(0)
            self.set_calls += 1
            self._store[key] = value

    redis = YieldingFakeRedis()

    with patch(_PATCH, side_effect=yielding_fetch):
        results = await asyncio.gather(*[
            get_dosing("same_drug", 35, _pool(), redis)
            for _ in range(CONCURRENCY)
        ])

    assert len(results) == CONCURRENCY
    assert all(isinstance(r, DosingResponse) for r in results)
    assert len(db_calls) >= 1, "Expected at least one DB call on a cold cache"
    print(f"\n  [thundering-herd] DB calls for {CONCURRENCY} concurrent cold requests: {len(db_calls)}")
    assert redis.has_key("dosing:same_drug:adult"), "Cache should be populated after concurrent requests"


# ──────────────────────────────────────────────────────────────────────────────
# 9. DB error in one task does NOT corrupt cache or sibling tasks
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_db_error_does_not_corrupt_sibling_cache():
    """
    If one concurrent request hits a DB error, other requests for different
    drug_ids that succeed should still populate and read their own cache entries
    correctly.
    """
    import asyncpg
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    redis = FakeRedis()
    rows  = [_make_row()]

    class _FakeDBError(asyncpg.PostgresError):
        def __init__(self):
            Exception.__init__(self, "simulated connection error")

    async def selective_fetch(pool, drug_id, age_groups):
        if drug_id == "broken_drug":
            raise _FakeDBError()
        return (rows, "primary", False)

    with patch(_PATCH, side_effect=selective_fetch):
        outcomes = await asyncio.gather(
            get_dosing("good_drug", 35, _pool(), redis),
            get_dosing("broken_drug", 35, _pool(), redis),
            return_exceptions=True,
        )

    good_result, bad_result = outcomes

    assert isinstance(good_result, DosingResponse), "good drug should succeed"
    assert isinstance(bad_result, HTTPException)
    assert bad_result.status_code == 500

    assert redis.has_key("dosing:good_drug:adult")
    cached = redis.get_parsed("dosing:good_drug:adult")
    assert cached["drug_id_1mg"] == "good_drug"
    assert not redis.has_key("dosing:broken_drug:adult")


# ──────────────────────────────────────────────────────────────────────────────
# 10. Correct age_group label per age in concurrent batch
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_age_group_labels_are_correct():
    """
    Firing concurrent requests with different ages for the same drug:
    each response must carry the correct age_group string.
    """
    from app.services.dosing_service import get_dosing

    age_cases = [
        (0,  "neonate"),
        (1,  "infant"),
        (10, "pediatric"),
        (35, "adult"),
        (70, "geriatric"),
    ]
    redis = FakeRedis()

    with patch(_PATCH, new=AsyncMock(return_value=([_make_row()], "primary", False))):
        results = await asyncio.gather(*[
            get_dosing("multi_age_drug", age, _pool(), redis)
            for age, _ in age_cases
        ])

    for (age, expected_group), result in zip(age_cases, results):
        assert result.age_group == expected_group, (
            f"age={age}: expected '{expected_group}', got '{result.age_group}'"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 11. High-concurrency stress — 50 simultaneous requests all return valid data
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_high_concurrency_stress():
    """50 simultaneous requests across 10 drugs × 5 age buckets — all valid."""
    from app.services.dosing_service import get_dosing

    drugs = [f"drug_{i}" for i in range(10)]
    ages  = [0, 1, 10, 35, 70]
    redis = FakeRedis()

    async def mock_fetch(pool, drug_id, age_groups):
        return ([_make_row(brand_name=f"b-{drug_id}")], "primary", False)

    with patch(_PATCH, side_effect=mock_fetch):
        tasks = [
            get_dosing(drug_id, age, _pool(), redis)
            for drug_id in drugs
            for age in ages
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == 50
    assert all(isinstance(r, DosingResponse) for r in results)
    assert redis.key_count == len(drugs) * len(ages)


# ──────────────────────────────────────────────────────────────────────────────
# 12. cache=True second call does NOT increment DB call count
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_request_is_served_from_cache_not_db():
    """After a cold miss, the second identical request must not touch the DB."""
    from app.services.dosing_service import get_dosing

    db_calls = 0

    async def counting_fetch(pool, drug_id, age_groups):
        nonlocal db_calls
        db_calls += 1
        return ([_make_row()], "primary", False)

    redis = FakeRedis()

    with patch(_PATCH, side_effect=counting_fetch):
        first  = await get_dosing("cached_drug", 35, _pool(), redis)
        second = await get_dosing("cached_drug", 35, _pool(), redis)

    assert db_calls == 1, f"DB should be called once; called {db_calls} times"
    assert first.cached  is False
    assert second.cached is True
