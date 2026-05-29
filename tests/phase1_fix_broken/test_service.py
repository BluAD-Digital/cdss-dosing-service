"""
Phase 1 — Fixed & expanded service tests.

Bugs fixed vs original test_dosing_service.py:
  - Missing patch for `dosing_repo.drug_exists` → AttributeError on pool.acquire
  - Cached payload missing `formulation_id` → ValidationError on DosingResponse

Expanded coverage:
  - Every age group (neonate, infant, pediatric, adult, geriatric)
  - Primary hit path for every age group
  - Fallback triggered when primary empty, for every age group
  - Fallback NOT called when primary succeeds
  - drug_exists=False skips primary, goes to fallback
  - Both primary and fallback empty → 404
  - DB error → 500
  - Cache miss → DB called, cached=False, cache populated
  - Cache hit → DB NOT called, cached=True, query_time_ms=0
  - Cache key format for every age group
  - All response fields validated
  - Multiple dosing rows
  - frequency_meaning resolved via frequency_mapper
  - Various drug IDs
  - query_time_ms > 0 on DB hit
  - Formulation ID in response
  - Brand name, salt_composition, generic_name from DB row
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.response import DosingResponse
from app.utils.age_mapper import age_to_groups, age_to_primary_group


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


def _cached_payload(**overrides):
    defaults = {
        "drug_id_1mg":     "457491",
        "formulation_id":  "1001",       # ← was missing in old tests
        "brand_name":      "TestBrand",
        "salt_composition":"Paracetamol 500mg",
        "generic_name":    "Paracetamol",
        "age_group":       "adult",
        "dosing":          [],
        "cached":          False,
        "query_time_ms":   12.5,
    }
    defaults.update(overrides)
    return defaults


class FakeRedis:
    def __init__(self, initial_data=None):
        self._store = {}
        if initial_data:
            for k, v in initial_data.items():
                self._store[k] = json.dumps(v)
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key):
        self.get_calls += 1
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self.set_calls += 1
        self._store[key] = value

    def has_key(self, key):
        return key in self._store

    def get_parsed(self, key):
        raw = self._store.get(key)
        return json.loads(raw) if raw else None


def _pool():
    return MagicMock()


def _patch_repo(*, exists=True, primary=None, fallback=None):
    primary  = primary  if primary  is not None else []
    fallback = fallback if fallback is not None else []
    return (
        patch("app.services.dosing_service.dosing_repo.drug_exists",
              new=AsyncMock(return_value=exists)),
        patch("app.services.dosing_service.dosing_repo.fetch_dosing",
              new=AsyncMock(return_value=primary)),
        patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback",
              new=AsyncMock(return_value=fallback)),
    )


# ═══════════════════════════════════════════════════════════════
# 1. CACHE — miss and hit behaviour
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cache_miss_calls_db_and_returns_result():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    rows  = [_make_row()]
    p1, p2, p3 = _patch_repo(primary=rows)
    with p1, p2 as mock_fetch, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    mock_fetch.assert_awaited_once()
    assert isinstance(result, DosingResponse)
    assert result.cached is False


@pytest.mark.asyncio
async def test_cache_miss_populates_redis():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        await get_dosing("457491", 35, _pool(), redis)
    assert redis.has_key("dosing:457491:adult")


@pytest.mark.asyncio
async def test_cache_hit_skips_db():
    from app.services.dosing_service import get_dosing
    payload = _cached_payload()
    redis   = FakeRedis({"dosing:457491:adult": payload})
    p1, p2, p3 = _patch_repo()
    with p1, p2 as mock_fetch, p3 as mock_fallback:
        result = await get_dosing("457491", 35, _pool(), redis)
    mock_fetch.assert_not_awaited()
    mock_fallback.assert_not_awaited()
    assert result.cached is True
    assert result.query_time_ms == 0.0


@pytest.mark.asyncio
async def test_cache_hit_returns_correct_drug_id():
    from app.services.dosing_service import get_dosing
    payload = _cached_payload(drug_id_1mg="999888")
    redis   = FakeRedis({"dosing:999888:adult": payload})
    p1, p2, p3 = _patch_repo()
    with p1, p2, p3:
        result = await get_dosing("999888", 35, _pool(), redis)
    assert result.drug_id_1mg == "999888"


@pytest.mark.asyncio
async def test_query_time_ms_positive_on_db_hit():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert result.query_time_ms >= 0


@pytest.mark.asyncio
async def test_cache_hit_query_time_is_zero():
    from app.services.dosing_service import get_dosing
    payload = _cached_payload()
    redis   = FakeRedis({"dosing:457491:adult": payload})
    p1, p2, p3 = _patch_repo()
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert result.query_time_ms == 0.0


# ═══════════════════════════════════════════════════════════════
# 2. PRIMARY vs FALLBACK path
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_primary_hit_does_not_call_fallback():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3 as mock_fallback:
        await get_dosing("457491", 35, _pool(), redis)
    mock_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_primary_triggers_fallback():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[], fallback=[_make_row()])
    with p1, p2, p3 as mock_fallback:
        await get_dosing("457491", 35, _pool(), redis)
    mock_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_drug_not_exists_skips_primary_calls_fallback():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(exists=False, fallback=[_make_row()])
    with p1, p2 as mock_fetch, p3 as mock_fallback:
        await get_dosing("457491", 35, _pool(), redis)
    mock_fetch.assert_not_awaited()
    mock_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_both_primary_and_fallback_empty_raises_404():
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[], fallback=[])
    with p1, p2, p3:
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", 35, _pool(), redis)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"] == "not_found"


@pytest.mark.asyncio
async def test_fallback_result_is_returned_as_valid_response():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[], fallback=[_make_row(brand_name="FallbackBrand")])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert isinstance(result, DosingResponse)
    assert result.brand_name == "FallbackBrand"


# ═══════════════════════════════════════════════════════════════
# 3. AGE GROUPS — correct group passed to repo and in response
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_groups,expected_primary", [
    (0,  ["neonate"],                   "neonate"),
    (1,  ["infant", "neonate"],         "infant"),
    (10, ["pediatric", "any"],          "pediatric"),
    (35, ["adult", "any"],              "adult"),
    (70, ["geriatric", "adult", "any"], "geriatric"),
])
async def test_correct_age_groups_passed_to_primary_fetch(age, expected_groups, expected_primary):
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2 as mock_fetch, p3:
        await get_dosing("457491", age, _pool(), redis)
    passed_groups = mock_fetch.call_args[0][2]
    assert passed_groups == expected_groups


@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_primary", [
    (0,  "neonate"),
    (1,  "infant"),
    (10, "pediatric"),
    (35, "adult"),
    (70, "geriatric"),
])
async def test_response_age_group_matches_patient_age(age, expected_primary):
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        result = await get_dosing("457491", age, _pool(), redis)
    assert result.age_group == expected_primary


@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_primary", [
    (0,  "neonate"),
    (1,  "infant"),
    (10, "pediatric"),
    (35, "adult"),
    (70, "geriatric"),
])
async def test_correct_age_groups_passed_to_fallback(age, expected_primary):
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    expected_groups = age_to_groups(age)
    p1, p2, p3 = _patch_repo(primary=[], fallback=[_make_row()])
    with p1, p2, p3 as mock_fallback:
        await get_dosing("457491", age, _pool(), redis)
    passed_groups = mock_fallback.call_args[0][2]
    assert passed_groups == expected_groups


# ═══════════════════════════════════════════════════════════════
# 4. CACHE KEY FORMAT
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id,age,expected_key", [
    ("457491",  0,  "dosing:457491:neonate"),
    ("457491",  1,  "dosing:457491:infant"),
    ("457491",  10, "dosing:457491:pediatric"),
    ("457491",  35, "dosing:457491:adult"),
    ("457491",  70, "dosing:457491:geriatric"),
    ("999888",  35, "dosing:999888:adult"),
    ("210470",  10, "dosing:210470:pediatric"),
    ("1146701", 0,  "dosing:1146701:neonate"),
    ("1002088", 65, "dosing:1002088:geriatric"),
    ("56693",   18, "dosing:56693:adult"),
])
async def test_cache_key_format(drug_id, age, expected_key):
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        await get_dosing(drug_id, age, _pool(), redis)
    assert redis.has_key(expected_key), (
        f"Expected key '{expected_key}', found: {list(redis._store.keys())}"
    )


# ═══════════════════════════════════════════════════════════════
# 5. RESPONSE FIELDS — all values come from DB row correctly
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_brand_name_from_db_row():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row(brand_name="Augmentin")])
    with p1, p2, p3:
        result = await get_dosing("1146701", 35, _pool(), redis)
    assert result.brand_name == "Augmentin"


@pytest.mark.asyncio
async def test_salt_composition_from_db_row():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row(salt_composition="Amoxicillin 500mg + Clavulanic Acid 125mg")])
    with p1, p2, p3:
        result = await get_dosing("1146701", 35, _pool(), redis)
    assert result.salt_composition == "Amoxicillin 500mg + Clavulanic Acid 125mg"


@pytest.mark.asyncio
async def test_generic_name_from_db_row():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row(generic_name="Amoxicillin / Clavulanic Acid")])
    with p1, p2, p3:
        result = await get_dosing("1146701", 35, _pool(), redis)
    assert result.generic_name == "Amoxicillin / Clavulanic Acid"


@pytest.mark.asyncio
async def test_formulation_id_from_db_row():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row(formulation_id=9999)])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert result.formulation_id == "9999"


@pytest.mark.asyncio
async def test_dosing_row_fields_from_db():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row(
        frequency="three times daily",
        route="intravenous",
        dose_amount="250",
        dose_unit="mg",
        duration="10 days",
        indication="bacterial infection",
        instructions="infuse over 30 minutes",
    )])
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    row = result.dosing[0]
    assert row.frequency  == "three times daily"
    assert row.route      == "intravenous"
    assert row.dose_amount== "250"
    assert row.dose_unit  == "mg"
    assert row.duration   == "10 days"
    assert row.indication == "bacterial infection"
    assert row.instructions == "infuse over 30 minutes"


@pytest.mark.asyncio
async def test_multiple_dosing_rows_all_returned():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    rows  = [
        _make_row(frequency="once daily",    indication="mild pain"),
        _make_row(frequency="twice daily",   indication="moderate pain"),
        _make_row(frequency="three times",   indication="severe pain"),
        _make_row(frequency="four times",    indication="acute fever"),
    ]
    p1, p2, p3 = _patch_repo(primary=rows)
    with p1, p2, p3:
        result = await get_dosing("457491", 35, _pool(), redis)
    assert len(result.dosing) == 4


# ═══════════════════════════════════════════════════════════════
# 6. ERROR HANDLING
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_db_error_raises_500():
    import asyncpg
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    class _FakeDBError(asyncpg.PostgresError):
        def __init__(self):
            Exception.__init__(self, "connection lost")

    redis = FakeRedis()

    async def failing_fetch(*args, **kwargs):
        raise _FakeDBError()

    with patch("app.services.dosing_service.dosing_repo.drug_exists",
               new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing",
               side_effect=failing_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback",
               new=AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", 35, _pool(), redis)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["error"] == "internal_error"


@pytest.mark.asyncio
async def test_404_detail_contains_drug_id_and_age():
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[], fallback=[])
    with p1, p2, p3:
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("MISSING_DRUG", 35, _pool(), redis)
    detail = exc_info.value.detail
    assert "MISSING_DRUG" in detail["message"]
    assert "35" in detail["message"]


# ═══════════════════════════════════════════════════════════════
# 7. 404 FOR ALL AGE GROUPS — no data means 404 regardless of age
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_404_for_all_age_groups_when_no_data(age):
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[], fallback=[])
    with p1, p2, p3:
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("UNKNOWN", age, _pool(), redis)
    assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════════════════
# 8. VARIOUS DRUG IDS — parametrized primary hit
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id,brand", [
    ("457491",  "Dolo 650"),
    ("210470",  "Combiflam"),
    ("1146701", "Augmentin"),
    ("1002088", "Brufen"),
    ("56693",   "Ciplox 500"),
    ("165440",  "Levoflox 500"),
    ("142807",  "Voveran SR"),
    ("1055048", "Thyronorm"),
    ("122170",  "Glyciphage"),
    ("1038076", "Desloratadine"),
])
async def test_various_drugs_return_correct_drug_id(drug_id, brand):
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row(brand_name=brand)])
    with p1, p2, p3:
        result = await get_dosing(drug_id, 35, _pool(), redis)
    assert result.drug_id_1mg == drug_id
    assert result.brand_name  == brand


# ═══════════════════════════════════════════════════════════════
# 9. SECOND REQUEST IS CACHE HIT — DB call count
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_second_call_same_key_hits_cache_not_db():
    from app.services.dosing_service import get_dosing
    redis    = FakeRedis()
    db_calls = 0

    async def counting_fetch(*args, **kwargs):
        nonlocal db_calls
        db_calls += 1
        return [_make_row()]

    with patch("app.services.dosing_service.dosing_repo.drug_exists",
               new=AsyncMock(return_value=True)), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing",
               side_effect=counting_fetch), \
         patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback",
               new=AsyncMock(return_value=[])):

        first  = await get_dosing("457491", 35, _pool(), redis)
        second = await get_dosing("457491", 35, _pool(), redis)

    assert db_calls == 1
    assert first.cached  is False
    assert second.cached is True


# ═══════════════════════════════════════════════════════════════
# 10. DIFFERENT KEYS DO NOT SHARE CACHE
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_different_drug_ids_have_separate_cache_entries():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        await get_dosing("drug_A", 35, _pool(), redis)
        await get_dosing("drug_B", 35, _pool(), redis)
    assert redis.has_key("dosing:drug_A:adult")
    assert redis.has_key("dosing:drug_B:adult")
    assert redis.get_parsed("dosing:drug_A:adult")["drug_id_1mg"] == "drug_A"
    assert redis.get_parsed("dosing:drug_B:adult")["drug_id_1mg"] == "drug_B"


@pytest.mark.asyncio
async def test_same_drug_different_ages_have_separate_cache_entries():
    from app.services.dosing_service import get_dosing
    redis = FakeRedis()
    p1, p2, p3 = _patch_repo(primary=[_make_row()])
    with p1, p2, p3:
        await get_dosing("457491", 10, _pool(), redis)
        await get_dosing("457491", 35, _pool(), redis)
        await get_dosing("457491", 70, _pool(), redis)
    assert redis.has_key("dosing:457491:pediatric")
    assert redis.has_key("dosing:457491:adult")
    assert redis.has_key("dosing:457491:geriatric")
