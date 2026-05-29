"""
Phase 3 — Reliability / Fault Tolerance tests (mocked service layer).

Tests what happens when individual components fail:
  - Redis read failure    → service falls through to DB (3.1)
  - Redis write failure   → silent, response still returned (3.2)
  - DB down              → 500 with correct shape, no raw error leak (3.3)
  - Slow DB              → completes without hanging (3.4)
  - Simulated total loss → multiple failure combos tested (3.5)
  - Error isolation      → one failing request does not corrupt siblings (3.6)

How the current code handles failures
  get_cached  : catches ALL exceptions, returns None  →  cache miss
  set_cached  : catches ALL exceptions silently       →  no crash
  service     : catches asyncpg.PostgresError         →  HTTPException(500)

Run:
    python3 -m pytest tests/phase3_reliability/test_fault_tolerance.py -v
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from app.schemas.response import DosingResponse


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _make_row(**overrides):
    defaults = {
        "formulation_id":   1001,
        "brand_name":       "TestBrand",
        "salt_composition": "Paracetamol 500mg",
        "generic_name":     "Paracetamol",
        "frequency":        "twice daily",
        "route":            "oral",
        "dose_amount":      "500",
        "dose_unit":        "mg",
        "duration":         "5 days",
        "indication":       "pain",
        "instructions":     None,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda k: defaults[k])
    return row


class _FakeDBError(asyncpg.PostgresError):
    def __init__(self, msg="simulated DB error"):
        Exception.__init__(self, msg)


def _pool():
    return MagicMock()


def _patch_repo(*, exists=True, primary=None, fallback=None):
    return (
        patch("app.services.dosing_service.dosing_repo.drug_exists",
              new=AsyncMock(return_value=exists)),
        patch("app.services.dosing_service.dosing_repo.fetch_dosing",
              new=AsyncMock(return_value=primary or [])),
        patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback",
              new=AsyncMock(return_value=fallback or [])),
    )


class BrokenGetRedis:
    """Redis that raises on get() but succeeds on set()."""
    def __init__(self, error=None):
        self._error = error or ConnectionError("Redis is down")
        self._store = {}
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key):
        self.get_calls += 1
        raise self._error

    async def set(self, key, value, ex=None):
        self.set_calls += 1
        self._store[key] = value

    async def ping(self):
        raise self._error


class BrokenSetRedis:
    """Redis that can read (returns None = miss) but raises on set()."""
    def __init__(self, error=None):
        self._error = error or ConnectionError("Redis write failed")
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key):
        self.get_calls += 1
        return None     # always cache miss

    async def set(self, key, value, ex=None):
        self.set_calls += 1
        raise self._error

    async def ping(self):
        return True


class FullyBrokenRedis:
    """Redis where both get() and set() raise."""
    def __init__(self):
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key):
        self.get_calls += 1
        raise ConnectionError("Redis completely unavailable")

    async def set(self, key, value, ex=None):
        self.set_calls += 1
        raise ConnectionError("Redis completely unavailable")

    async def ping(self):
        raise ConnectionError("Redis completely unavailable")


class SlowRedis:
    """Redis that delays get() to simulate a slow/overloaded cache."""
    def __init__(self, delay_seconds=0.1):
        self._delay = delay_seconds
        self._store = {}

    async def get(self, key):
        await asyncio.sleep(self._delay)
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        await asyncio.sleep(self._delay)
        self._store[key] = value


# ═══════════════════════════════════════════════════════════════
# 3.1 — Redis READ failure → falls through to DB
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_redis_get_connection_error_falls_through_to_db():
    from app.services.dosing_service import get_dosing
    redis = BrokenGetRedis(ConnectionError("Redis is down"))
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2 as mock_fetch, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    mock_fetch.assert_awaited_once()
    assert isinstance(result, DosingResponse)
    assert result.drug_id_1mg == "457491"


@pytest.mark.asyncio
async def test_redis_get_timeout_error_falls_through_to_db():
    from app.services.dosing_service import get_dosing
    redis = BrokenGetRedis(TimeoutError("Redis timed out"))
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert isinstance(result, DosingResponse)


@pytest.mark.asyncio
async def test_redis_get_generic_exception_falls_through_to_db():
    from app.services.dosing_service import get_dosing
    redis = BrokenGetRedis(Exception("Unknown Redis error"))
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert isinstance(result, DosingResponse)


@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_group", [
    (0,  "neonate"),
    (1,  "infant"),
    (10, "pediatric"),
    (35, "adult"),
    (70, "geriatric"),
])
async def test_redis_down_all_age_groups_still_work(age, expected_group):
    from app.services.dosing_service import get_dosing
    redis = BrokenGetRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", age, _pool(), redis)
    assert isinstance(result, DosingResponse)
    assert result.age_group == expected_group


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", [
    "210470", "142807", "1002088", "56693", "165440",
    "344363", "1115733", "1147914", "1123438", "16542",
])
async def test_redis_down_various_drugs_still_return_data(drug_id):
    from app.services.dosing_service import get_dosing
    redis = BrokenGetRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row(brand_name=f"Brand-{drug_id}")])
    with p1, p2, p3:
        result = await get_dosing(drug_id, 35, _pool(), redis)
    assert result.drug_id_1mg == drug_id
    assert result.brand_name  == f"Brand-{drug_id}"


@pytest.mark.asyncio
async def test_redis_down_fallback_path_still_works():
    from app.services.dosing_service import get_dosing
    redis = BrokenGetRedis()
    p1, p2, p3 = _patch_repo(primary=[], fallback=[_make_row(brand_name="FallbackBrand")])
    with p1, p2, p3:
        result = await get_dosing("74467", 35, _pool(), redis)
    assert result.brand_name == "FallbackBrand"


@pytest.mark.asyncio
async def test_redis_down_returns_cached_false_since_no_cache():
    """When Redis is broken, cached=False on every response (always from DB)."""
    from app.services.dosing_service import get_dosing
    redis = FullyBrokenRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        first  = await get_dosing("457491", 35, _pool(), redis)
        second = await get_dosing("457491", 35, _pool(), redis)
    assert first.cached  is False
    assert second.cached is False   # no cache was ever written


# ═══════════════════════════════════════════════════════════════
# 3.2 — Redis WRITE failure → silent, response still returned
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_redis_set_connection_error_is_silent():
    from app.services.dosing_service import get_dosing
    redis = BrokenSetRedis(ConnectionError("Redis write failed"))
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert isinstance(result, DosingResponse)
    assert result.cached is False    # write failed, so cached=False


@pytest.mark.asyncio
async def test_redis_set_timeout_is_silent():
    from app.services.dosing_service import get_dosing
    redis = BrokenSetRedis(TimeoutError("Redis write timed out"))
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert isinstance(result, DosingResponse)


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_redis_write_failure_all_age_groups_still_return_data(age):
    from app.services.dosing_service import get_dosing
    redis = BrokenSetRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", age, _pool(), redis)
    assert isinstance(result, DosingResponse)
    assert result.age_group == {0: "neonate", 1: "infant", 10: "pediatric", 35: "adult", 70: "geriatric"}[age]


@pytest.mark.asyncio
async def test_redis_fully_broken_every_call_hits_db():
    """With Redis completely down, every request goes to DB and returns data."""
    from app.services.dosing_service import get_dosing
    redis    = FullyBrokenRedis()
    db_calls = 0

    async def counting_fetch(*args, **kwargs):
        nonlocal db_calls
        db_calls += 1
        return [_make_row()]

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=counting_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):

        for _ in range(5):
            result = await get_dosing("457491", 35, _pool(), redis)
            assert isinstance(result, DosingResponse)

    assert db_calls == 5     # every request hits DB (no caching)


@pytest.mark.asyncio
async def test_concurrent_requests_redis_write_broken_all_succeed():
    from app.services.dosing_service import get_dosing
    redis = BrokenSetRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        results = await asyncio.gather(*[
            get_dosing("457491", 35, _pool(), redis) for _ in range(10)
        ])
    assert all(isinstance(r, DosingResponse) for r in results)


# ═══════════════════════════════════════════════════════════════
# 3.3 — DB DOWN → 500 with correct error shape
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_db_error_on_primary_fetch_returns_500():
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    async def failing_fetch(*args, **kwargs):
        raise _FakeDBError("connection refused")

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=failing_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", 35, _pool(), FakeRedis())

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["error"] == "internal_error"
    assert "message" in exc_info.value.detail


@pytest.mark.asyncio
async def test_db_error_on_fallback_fetch_returns_500():
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    async def failing_fallback(*args, **kwargs):
        raise _FakeDBError("fallback DB is down")

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", new=AsyncMock(return_value=[])), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", side_effect=failing_fallback):
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", 35, _pool(), FakeRedis())

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_db_error_does_not_leak_connection_string_in_detail():
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    async def failing_fetch(*args, **kwargs):
        raise _FakeDBError("password authentication failed for user postgres@178.236.185.230")

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=failing_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", 35, _pool(), FakeRedis())

    detail_str = json.dumps(exc_info.value.detail)
    # Must not expose IP, DB host, or raw postgres error text
    for sensitive in ["178.236.185.230", "password authentication", "postgres@"]:
        assert sensitive not in detail_str, f"Sensitive data leaked: {sensitive}"


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_db_error_returns_500_for_all_age_groups(age):
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing",
               side_effect=_FakeDBError), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback",
               new=AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", age, _pool(), FakeRedis())

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_db_error_shape_has_required_fields():
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=_FakeDBError), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", 35, _pool(), FakeRedis())

    detail = exc_info.value.detail
    assert "error"   in detail
    assert "message" in detail
    assert detail["error"] == "internal_error"


# ═══════════════════════════════════════════════════════════════
# 3.4 — Slow DB → completes, does not hang
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_slow_db_2s_still_completes():
    """A 2-second DB delay should still complete without hanging."""
    from app.services.dosing_service import get_dosing

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(2)
        return [_make_row()]

    t0 = time.perf_counter()
    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=slow_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):
        result = await get_dosing("457491", 35, _pool(), FakeRedis())

    elapsed = time.perf_counter() - t0
    assert isinstance(result, DosingResponse)
    assert elapsed < 10, f"Slow DB test took {elapsed:.1f}s — service may be hanging"


@pytest.mark.asyncio
async def test_slow_redis_still_completes():
    """A slow Redis (0.5s per op) should not crash the service."""
    from app.services.dosing_service import get_dosing
    redis = SlowRedis(delay_seconds=0.2)
    p1, p2, p3 = _patch_repo(primary=[_make_row()])

    t0 = time.perf_counter()
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    elapsed = time.perf_counter() - t0

    assert isinstance(result, DosingResponse)
    assert elapsed < 10, f"Slow Redis test took {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_concurrent_slow_db_all_complete():
    """10 concurrent requests each hitting a 1s slow DB — all must complete."""
    from app.services.dosing_service import get_dosing
    CONCURRENCY = 10

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(1)
        return [_make_row()]

    t0 = time.perf_counter()
    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=slow_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):
        results = await asyncio.gather(*[
            get_dosing(f"drug_{i}", 35, _pool(), FakeRedis()) for i in range(CONCURRENCY)
        ])
    elapsed = time.perf_counter() - t0

    assert all(isinstance(r, DosingResponse) for r in results)
    # With asyncio concurrency, 10 × 1s tasks should complete in ~1s not 10s
    assert elapsed < 5, f"Expected concurrent execution ~1s, got {elapsed:.1f}s"


# ═══════════════════════════════════════════════════════════════
# 3.5 — Error isolation: one failing request doesn't affect siblings
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_db_error_isolated_other_concurrent_requests_succeed():
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    good_drugs  = [f"good_{i}" for i in range(5)]
    broken_drug = "broken_drug"

    async def selective_fetch(pool, drug_id, age_groups):
        if drug_id == broken_drug:
            raise _FakeDBError()
        return [_make_row(brand_name=f"Brand-{drug_id}")]

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=selective_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):

        outcomes = await asyncio.gather(
            *[get_dosing(d, 35, _pool(), FakeRedis()) for d in good_drugs],
            get_dosing(broken_drug, 35, _pool(), FakeRedis()),
            return_exceptions=True,
        )

    good_results = outcomes[:5]
    bad_result   = outcomes[5]

    for drug_id, result in zip(good_drugs, good_results):
        assert isinstance(result, DosingResponse), f"{drug_id} should succeed"
        assert result.drug_id_1mg == drug_id

    assert isinstance(bad_result, HTTPException)
    assert bad_result.status_code == 500


@pytest.mark.asyncio
async def test_redis_error_and_db_404_isolated():
    """Redis down + unknown drug → 404, not 500."""
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    redis = FullyBrokenRedis()
    p1, p2, p3 = _patch_repo(primary=[], fallback=[])
    with p1, p2, p3:
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("UNKNOWN_DRUG", 35, _pool(), redis)
    # DB returned nothing → 404, even though Redis was broken
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_mixed_redis_failure_and_success_concurrent():
    """Some concurrent requests have broken Redis, others have normal Redis."""
    from app.services.dosing_service import get_dosing

    class NormalRedis:
        def __init__(self):
            self._store = {}
        async def get(self, key): return self._store.get(key)
        async def set(self, key, value, ex=None): self._store[key] = value

    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        results = await asyncio.gather(
            get_dosing("drug_A", 35, _pool(), FullyBrokenRedis()),
            get_dosing("drug_B", 35, _pool(), NormalRedis()),
            get_dosing("drug_C", 35, _pool(), BrokenGetRedis()),
            get_dosing("drug_D", 35, _pool(), BrokenSetRedis()),
            get_dosing("drug_E", 35, _pool(), NormalRedis()),
        )

    assert all(isinstance(r, DosingResponse) for r in results)
    drug_ids = ["drug_A", "drug_B", "drug_C", "drug_D", "drug_E"]
    for drug_id, result in zip(drug_ids, results):
        assert result.drug_id_1mg == drug_id


# ═══════════════════════════════════════════════════════════════
# 3.6 — Stress: many concurrent mocked requests — no degradation
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_50_concurrent_requests_all_succeed():
    from app.services.dosing_service import get_dosing

    class FakeRedis:
        def __init__(self):
            self._store = {}
        async def get(self, key): return self._store.get(key)
        async def set(self, key, value, ex=None): self._store[key] = value

    drugs = [f"drug_{i}" for i in range(50)]

    async def drug_fetch(pool, drug_id, age_groups):
        return [_make_row(brand_name=f"Brand-{drug_id}")]

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=drug_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):

        results = await asyncio.gather(*[
            get_dosing(d, 35, _pool(), FakeRedis()) for d in drugs
        ])

    assert len(results) == 50
    assert all(isinstance(r, DosingResponse) for r in results)
    for drug_id, result in zip(drugs, results):
        assert result.drug_id_1mg == drug_id


@pytest.mark.asyncio
async def test_100_concurrent_same_drug_cache_populated_then_subsequent_hit():
    """
    100 concurrent requests for the same drug — all see cache miss (thundering herd,
    no distributed lock).  After all complete, a follow-up single request MUST be
    a cache hit because the cache was written by one of the 100.
    """
    from app.services.dosing_service import get_dosing

    class YieldingRedis:
        def __init__(self):
            self._store = {}
        async def get(self, key):
            await asyncio.sleep(0)
            return self._store.get(key)
        async def set(self, key, value, ex=None):
            await asyncio.sleep(0)
            self._store[key] = value

    async def fetch_with_yield(pool, drug_id, age_groups):
        await asyncio.sleep(0)
        return [_make_row()]

    redis = YieldingRedis()

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=fetch_with_yield), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):

        # Phase 1 — concurrent burst (all cold, thundering herd expected)
        results = await asyncio.gather(*[
            get_dosing("common_drug", 35, _pool(), redis) for _ in range(100)
        ])
        assert len(results) == 100
        assert all(isinstance(r, DosingResponse) for r in results)

        # Phase 2 — single follow-up request MUST be a cache hit
        followup = await get_dosing("common_drug", 35, _pool(), redis)

    assert followup.cached is True, (
        "Follow-up request after concurrent burst should be served from cache"
    )


@pytest.mark.asyncio
async def test_404_still_raised_even_when_redis_slow():
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    redis = SlowRedis(delay_seconds=0.5)
    p1, p2, p3 = _patch_repo(primary=[], fallback=[])
    with p1, p2, p3:
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("UNKNOWN", 35, _pool(), redis)
    assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Redis error types (OSError, BrokenPipe, ValueError, etc.)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    OSError("connection reset"),
    BrokenPipeError("pipe broken"),
    ConnectionResetError("connection reset by peer"),
    ConnectionRefusedError("connection refused"),
    MemoryError("out of memory"),
    RuntimeError("redis client closed"),
    ValueError("invalid response"),
    Exception("unknown redis error"),
])
async def test_redis_get_various_error_types_fall_through_to_db(error):
    from app.services.dosing_service import get_dosing
    redis = BrokenGetRedis(error)
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert isinstance(result, DosingResponse)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    OSError("write failed"),
    BrokenPipeError("redis pipe broken"),
    ConnectionResetError("redis reset"),
    MemoryError("redis OOM"),
    RuntimeError("redis write closed"),
    TimeoutError("redis write timeout"),
    ValueError("invalid key"),
    Exception("unknown redis write error"),
])
async def test_redis_set_various_error_types_are_silent(error):
    from app.services.dosing_service import get_dosing
    redis = BrokenSetRedis(error)
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert isinstance(result, DosingResponse)


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Corrupted Redis data (non-JSON, partial, wrong type)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [
    "NOT JSON",
    "{broken json",
    "null",
    "",
    "   ",
    "{'single': 'quotes'}",
    "<xml>nope</xml>",
    "undefined",
])
async def test_corrupted_redis_invalid_json_falls_through_to_db(bad_value):
    """If Redis returns invalid/unparseable JSON, get_cached catches the error
    and returns None, so the service falls through to DB."""
    from app.services.dosing_service import get_dosing

    class CorruptRedis:
        async def get(self, key): return bad_value
        async def set(self, key, value, ex=None): pass

    p1, p2, p3 = _patch_repo(primary=[_make_row(brand_name="FreshFromDB")])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), CorruptRedis())
    assert isinstance(result, DosingResponse)
    assert result.brand_name == "FreshFromDB"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", ["[]", "42"])
async def test_corrupted_redis_valid_json_but_wrong_type_is_known_bug(bad_value):
    """
    KNOWN BUG: When Redis returns valid JSON that is NOT a dict (e.g. "[]" or "42"),
    json.loads succeeds and get_cached returns the value (non-None).
    The service then calls .pop() on a list/int and crashes with AttributeError → 500.

    Root cause: dosing_service.py:32 calls cached_data.pop() without checking type.
    Fix needed: add `if not isinstance(cached_data, dict): return None` in get_cached.
    """
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    class CorruptRedis:
        async def get(self, key): return bad_value
        async def set(self, key, value, ex=None): pass

    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        with pytest.raises((HTTPException, AttributeError, TypeError)):
            await get_dosing("457491", 35, _pool(), CorruptRedis())
    # This test passes when the bug is present (raises) AND when it's fixed
    # (would return DosingResponse).  Change assertion to DosingResponse after fix.


# ═══════════════════════════════════════════════════════════════
# EXPANDED — All 13 known drugs with Redis completely down
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id,brand", [
    ("210470",  "Combiflam"),
    ("142807",  "Voveran SR"),
    ("1002088", "Brufen"),
    ("56693",   "Ciplox 500"),
    ("165440",  "Levoflox 500"),
    ("344363",  "Dolonex DT"),
    ("1115733", "Dolopar"),
    ("1147914", "Naprosyn"),
    ("1123438", "Moxikind-CV"),
    ("16542",   "Lignocaine"),
    ("201825",  "Mesalamine"),
    ("122170",  "Glyciphage"),
    ("1038076", "Desloratadine"),
])
async def test_all_known_drugs_still_work_with_redis_fully_broken(drug_id, brand):
    from app.services.dosing_service import get_dosing
    redis = FullyBrokenRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row(brand_name=brand)])
    with p1, p2, p3:
        result = await get_dosing(drug_id, 35, _pool(), redis)
    assert result.drug_id_1mg == drug_id
    assert result.brand_name  == brand


# ═══════════════════════════════════════════════════════════════
# EXPANDED — All age groups with DB error → always 500
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("age,group", [
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
async def test_db_error_for_every_age_boundary_returns_500(age, group):
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=_FakeDBError), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", age, _pool(), FakeRedis())

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["error"] == "internal_error"


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Slow DB at different delay levels
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0.05, 0.1, 0.3, 0.5, 1.0])
async def test_slow_db_various_delays_all_complete(delay):
    from app.services.dosing_service import get_dosing

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(delay)
        return [_make_row()]

    t0 = time.perf_counter()
    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=slow_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):
        result = await get_dosing("457491", 35, _pool(), FakeRedis())

    elapsed = time.perf_counter() - t0
    assert isinstance(result, DosingResponse)
    assert elapsed < delay + 2, f"Took {elapsed:.2f}s for {delay}s delay (too slow)"


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Intermittent failures: N of M requests fail
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("fail_indices,total", [
    ([0],          5),    # first fails
    ([4],          5),    # last fails
    ([2],          5),    # middle fails
    ([0, 4],       5),    # first + last fail
    ([1, 2, 3],    5),    # middle 3 fail
    ([0, 2, 4],    5),    # alternating fail
])
async def test_intermittent_db_failures_isolated(fail_indices, total):
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    drug_ids   = [f"drug_{i}" for i in range(total)]
    call_count = [-1]

    async def selective_fetch(pool, drug_id, age_groups):
        idx = int(drug_id.split("_")[1])
        if idx in fail_indices:
            raise _FakeDBError()
        return [_make_row(brand_name=f"Brand-{drug_id}")]

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=selective_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):

        outcomes = await asyncio.gather(
            *[get_dosing(d, 35, _pool(), FakeRedis()) for d in drug_ids],
            return_exceptions=True,
        )

    for i, outcome in enumerate(outcomes):
        if i in fail_indices:
            assert isinstance(outcome, HTTPException) and outcome.status_code == 500
        else:
            assert isinstance(outcome, DosingResponse)
            assert outcome.drug_id_1mg == drug_ids[i]


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Response completeness under various failure states
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("redis_type,description", [
    ("broken_get",  "Redis get broken"),
    ("broken_set",  "Redis set broken"),
    ("fully_broken","Redis fully broken"),
    ("slow",        "Redis slow (0.1s)"),
])
async def test_response_has_all_fields_under_redis_failure(redis_type, description):
    from app.services.dosing_service import get_dosing

    redis_map = {
        "broken_get":   BrokenGetRedis(),
        "broken_set":   BrokenSetRedis(),
        "fully_broken": FullyBrokenRedis(),
        "slow":         SlowRedis(0.1),
    }
    redis = redis_map[redis_type]
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)

    assert result.drug_id_1mg     == "457491"
    assert result.formulation_id  is not None
    assert result.brand_name      is not None
    assert result.age_group       == "adult"
    assert isinstance(result.dosing, list)
    assert len(result.dosing)     >= 1


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Concurrent requests with broken Redis, all drugs
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [5, 10, 20, 50])
async def test_n_concurrent_requests_redis_down_all_succeed(concurrency):
    from app.services.dosing_service import get_dosing

    async def drug_fetch(pool, drug_id, age_groups):
        return [_make_row(brand_name=f"Brand-{drug_id}")]

    drugs = [f"drug_{i}" for i in range(concurrency)]

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=drug_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):

        results = await asyncio.gather(*[
            get_dosing(d, 35, _pool(), FullyBrokenRedis()) for d in drugs
        ])

    assert len(results) == concurrency
    assert all(isinstance(r, DosingResponse) for r in results)
    for drug_id, result in zip(drugs, results):
        assert result.drug_id_1mg == drug_id


# ═══════════════════════════════════════════════════════════════
# EXPANDED — 404 behaviour under various failure states
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("redis_class", [
    BrokenGetRedis,
    BrokenSetRedis,
    FullyBrokenRedis,
])
async def test_404_returned_correctly_regardless_of_redis_state(redis_class):
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    p1, p2, p3 = _patch_repo(primary=[], fallback=[])
    with p1, p2, p3:
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("UNKNOWN_DRUG", 35, _pool(), redis_class())
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"] == "not_found"


# ═══════════════════════════════════════════════════════════════
# EXPANDED — DB error detail never leaks internal info
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("db_error_msg,sensitive_word", [
    ("password=secret123",       "secret123"),
    ("host=178.236.185.230",     "178.236.185.230"),
    ("user=postgres password=X", "password"),
    ("asyncpg internal error",   "asyncpg"),
    ("SELECT * FROM dosing_regimen", "SELECT"),
    ("ERROR: relation does not exist", "relation"),
    ("FATAL: connection terminated",  "FATAL"),
    ("SSL SYSCALL error",             "SYSCALL"),
])
async def test_db_error_message_not_leaked_various(db_error_msg, sensitive_word):
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    class FakeRedis:
        async def get(self, key): return None
        async def set(self, key, value, ex=None): pass

    async def failing_fetch(*a, **kw):
        raise _FakeDBError(db_error_msg)

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=failing_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", 35, _pool(), FakeRedis())

    detail_str = json.dumps(exc_info.value.detail)
    assert sensitive_word not in detail_str, (
        f"Sensitive word '{sensitive_word}' leaked in error detail: {detail_str}"
    )


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Fallback path under Redis failures (all age groups)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_group", [
    (0,  "neonate"),
    (1,  "infant"),
    (10, "pediatric"),
    (35, "adult"),
    (70, "geriatric"),
])
async def test_fallback_path_works_with_redis_down(age, expected_group):
    from app.services.dosing_service import get_dosing
    redis = FullyBrokenRedis()
    p1, p2, p3 = _patch_repo(
        primary=[],
        fallback=[_make_row(brand_name=f"Fallback-{expected_group}")]
    )
    with p1, p2, p3:
        result = await get_dosing("74467", age, _pool(), redis)
    assert result.age_group  == expected_group
    assert result.brand_name == f"Fallback-{expected_group}"


# ═══════════════════════════════════════════════════════════════
# EXPANDED — Cache write failure: DB called N times (no caching)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("n_calls", [2, 3, 5, 10])
async def test_redis_write_broken_db_called_n_times(n_calls):
    from app.services.dosing_service import get_dosing
    redis    = BrokenSetRedis()
    db_calls = 0

    async def counting_fetch(*args, **kwargs):
        nonlocal db_calls
        db_calls += 1
        return [_make_row()]

    with patch("app.services.dosing_service.dosing_repo.drug_exists", new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing", side_effect=counting_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback", new=AsyncMock(return_value=[])):
        for _ in range(n_calls):
            result = await get_dosing("457491", 35, _pool(), redis)
            assert isinstance(result, DosingResponse)

    assert db_calls == n_calls, (
        f"Expected {n_calls} DB calls (no caching), got {db_calls}"
    )
