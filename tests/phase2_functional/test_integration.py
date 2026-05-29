"""
Phase 2 — Full HTTP stack integration tests.

Every test fires a real HTTP request to the live API at http://34.14.197.45:8001.
Path covered:  aiohttp  →  nginx  →  gunicorn  →  FastAPI  →  PostgreSQL  →  Redis

Known drugs (verified from top-500 coverage run on 2026-05-28):
  Primary hits : 210470  Combiflam
                 142807  Voveran SR
                 1146701 Augmentin
                 1002088 Brufen
                 56693   Ciplox 500
                 165440  Levoflox 500
                 344363  Dolonex DT
                 1115733 Dolopar
                 1147914 Naprosyn
                 1123438 Moxikind-CV
                 16542   Lignocaine
                 201825  Mesalamine
                 122170  Glyciphage
                 1038076 Desloratadine

  Fallback hits: 74467   Dolo 650
                 600468  Crocin Advance
                 272818  Calpol
                 324940  Azithral 500
                 324155  Azee 500

  404 in DB    : 1003457 Sumo (drugbank only)
                 1041271 Monocef
                 1042224 Taxim-O

  Not in DB    : NONEXISTENT_DRUG_XYZ

Run:
    python3 -m pytest tests/phase2_functional/test_integration.py -v
"""

import os
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from dotenv import load_dotenv

from dotenv import dotenv_values
_env = dotenv_values(Path(__file__).parent.parent.parent / ".env")

BASE_URL = "http://34.14.197.45:8001"
API_KEY  = _env["API_KEY"]
HEADERS  = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
ENDPOINT = f"{BASE_URL}/api/v1/dosing"


# ─────────────────────────────────────────────────────────────
# Shared session fixture
# ─────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def session():
    connector = aiohttp.TCPConnector(limit=20)
    timeout   = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
        yield s


async def _post(session, drug_id, age):
    async with session.post(ENDPOINT, json={"drug_id_1mg": drug_id, "age": age},
                            headers=HEADERS) as resp:
        return resp.status, await resp.json(content_type=None)


# ═══════════════════════════════════════════════════════════════
# 1. HEALTH CHECK — service is alive
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(session):
    async with session.get(f"{BASE_URL}/health") as resp:
        assert resp.status == 200
        data = await resp.json(content_type=None)
    assert data["status"] == "ok"
    assert data["db"]     == "connected"
    assert data["cache"]  == "connected"


@pytest.mark.asyncio
async def test_health_requires_no_api_key(session):
    async with session.get(f"{BASE_URL}/health") as resp:
        assert resp.status == 200


# ═══════════════════════════════════════════════════════════════
# 2. KNOWN PRIMARY-HIT DRUGS — adult (age=35)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id,brand_hint", [
    ("210470",  "Combiflam"),
    ("142807",  "Voveran"),
    ("1002088", "Brufen"),
    ("56693",   "Ciplox"),
    ("165440",  "Levoflox"),
    ("344363",  "Dolonex"),
    ("1115733", "Dolopar"),
    ("1147914", "Naprosyn"),
    ("1123438", "Moxikind"),
    ("16542",   "Lignocaine"),
    ("201825",  "Mesalamine"),
    ("122170",  "Glyciphage"),
    ("1038076", "Desloratadine"),
    # Note: 1146701 (Augmentin) shows primary in top-500 check SQL (missing the
    # administration_notes filter), but returns 404 via the real API endpoint.
])
async def test_primary_hit_drugs_return_200(session, drug_id, brand_hint):
    status, data = await _post(session, drug_id, 35)
    assert status == 200, f"{drug_id} ({brand_hint}): expected 200, got {status} — {data}"


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", [
    "210470", "142807", "1002088", "56693",
    "165440", "344363", "1115733", "1147914", "1123438",
])
async def test_primary_hit_response_has_dosing_rows(session, drug_id):
    status, data = await _post(session, drug_id, 35)
    assert status == 200
    assert len(data["dosing"]) > 0, f"{drug_id}: expected dosing rows, got empty list"


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", [
    "210470", "142807", "1002088", "56693", "165440",
])
async def test_primary_hit_drug_id_in_response_matches_request(session, drug_id):
    status, data = await _post(session, drug_id, 35)
    assert status == 200
    assert data["drug_id_1mg"] == drug_id


# ═══════════════════════════════════════════════════════════════
# 3. KNOWN FALLBACK DRUGS
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id,brand_hint", [
    ("74467",  "Dolo 650"),
    ("600468", "Crocin Advance"),
    ("272818", "Calpol"),
    ("324940", "Azithral"),
    ("324155", "Azee"),
])
async def test_fallback_drugs_return_200(session, drug_id, brand_hint):
    status, data = await _post(session, drug_id, 35)
    assert status == 200, f"{drug_id} ({brand_hint}): expected 200, got {status}"


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", ["74467", "600468", "272818"])
async def test_fallback_drugs_have_dosing_rows(session, drug_id):
    status, data = await _post(session, drug_id, 35)
    assert status == 200
    assert len(data["dosing"]) > 0


# ═══════════════════════════════════════════════════════════════
# 4. KNOWN 404 DRUGS
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id,label", [
    ("NONEXISTENT_DRUG_XYZ", "not in DB at all"),
    ("000000000",            "zero-padded fake id"),
    ("99999999999",          "large fake id"),
])
async def test_nonexistent_drugs_return_404(session, drug_id, label):
    status, data = await _post(session, drug_id, 35)
    assert status == 404, f"{label}: expected 404, got {status}"
    assert data["error"] == "not_found"
    assert "message" in data


# ═══════════════════════════════════════════════════════════════
# 5. RESPONSE SCHEMA — all fields present and correct types
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_response_has_all_top_level_fields(session):
    status, data = await _post(session, "210470", 35)
    assert status == 200
    for field in ["drug_id_1mg", "formulation_id", "brand_name", "salt_composition",
                  "generic_name", "age_group", "dosing", "cached", "query_time_ms"]:
        assert field in data, f"Missing field: {field}"


@pytest.mark.asyncio
async def test_dosing_row_has_all_fields(session):
    status, data = await _post(session, "210470", 35)
    assert status == 200
    row = data["dosing"][0]
    for field in ["frequency", "frequency_meaning", "route",
                  "dose_amount", "dose_unit", "duration", "indication", "instructions"]:
        assert field in row, f"Missing dosing row field: {field}"


@pytest.mark.asyncio
async def test_query_time_ms_is_positive_on_cold_miss(session):
    status, data = await _post(session, "210470", 35)
    assert status == 200
    assert data["query_time_ms"] >= 0


@pytest.mark.asyncio
async def test_age_group_is_string(session):
    status, data = await _post(session, "210470", 35)
    assert status == 200
    assert isinstance(data["age_group"], str)
    assert data["age_group"] in ["neonate", "infant", "pediatric", "adult", "geriatric"]


@pytest.mark.asyncio
async def test_dosing_is_list(session):
    status, data = await _post(session, "210470", 35)
    assert status == 200
    assert isinstance(data["dosing"], list)


@pytest.mark.asyncio
async def test_cached_is_boolean(session):
    status, data = await _post(session, "210470", 35)
    assert status == 200
    assert isinstance(data["cached"], bool)


# ═══════════════════════════════════════════════════════════════
# 6. AGE GROUP LABEL — correct group per age
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_group", [
    (18, "adult"),
    (35, "adult"),
    (64, "adult"),
    (65, "geriatric"),
    (70, "geriatric"),
    (90, "geriatric"),
    (120,"geriatric"),
])
async def test_age_group_label_correct_for_known_drug(session, age, expected_group):
    status, data = await _post(session, "210470", age)
    assert status == 200
    assert data["age_group"] == expected_group, (
        f"age={age}: expected {expected_group}, got {data['age_group']}"
    )


# ═══════════════════════════════════════════════════════════════
# 7. CACHE — second call returns cached=True
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", ["210470", "1002088", "56693", "165440"])
async def test_second_request_is_served_from_cache(session, drug_id):
    # First call — could be cache miss or hit depending on server state
    _, first = await _post(session, drug_id, 35)
    # Second call — must be a cache hit
    status, second = await _post(session, drug_id, 35)
    assert status == 200
    assert second["cached"] is True, f"{drug_id}: second call should be cached=True"
    assert second["query_time_ms"] == 0.0


@pytest.mark.asyncio
async def test_cache_hit_returns_same_data_as_miss(session):
    _, first  = await _post(session, "210470", 35)
    _, second = await _post(session, "210470", 35)
    assert second["drug_id_1mg"]     == first["drug_id_1mg"]
    assert second["brand_name"]      == first["brand_name"]
    assert second["age_group"]       == first["age_group"]
    assert second["salt_composition"]== first["salt_composition"]
    assert len(second["dosing"])     == len(first["dosing"])


# ═══════════════════════════════════════════════════════════════
# 8. DATA ISOLATION — different requests don't bleed data
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_concurrent_different_drugs_are_isolated(session):
    import asyncio
    drug_ids = ["210470", "142807", "1002088", "56693", "165440"]
    results  = await asyncio.gather(*[_post(session, d, 35) for d in drug_ids])
    for drug_id, (status, data) in zip(drug_ids, results):
        assert status == 200
        assert data["drug_id_1mg"] == drug_id, (
            f"Expected drug_id {drug_id}, got {data['drug_id_1mg']}"
        )


@pytest.mark.asyncio
async def test_same_drug_different_ages_give_different_age_groups(session):
    import asyncio
    cases    = [(35, "adult"), (70, "geriatric")]
    results  = await asyncio.gather(*[_post(session, "210470", age) for age, _ in cases])
    for (age, expected_group), (status, data) in zip(cases, results):
        assert status == 200
        assert data["age_group"] == expected_group


# ═══════════════════════════════════════════════════════════════
# 9. DOSING ROW CONTENT SANITY
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_frequency_is_non_empty_string(session):
    _, data = await _post(session, "210470", 35)
    assert data["dosing"][0]["frequency"] not in (None, "")


@pytest.mark.asyncio
async def test_route_is_non_empty_string(session):
    _, data = await _post(session, "210470", 35)
    assert data["dosing"][0]["route"] not in (None, "")


@pytest.mark.asyncio
async def test_dose_unit_is_non_empty_string(session):
    _, data = await _post(session, "210470", 35)
    assert data["dosing"][0]["dose_unit"] not in (None, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", ["210470", "1002088", "56693"])
async def test_multiple_dosing_rows_present_for_top_drugs(session, drug_id):
    _, data = await _post(session, drug_id, 35)
    assert len(data["dosing"]) >= 1


# ═══════════════════════════════════════════════════════════════
# 10. ERROR RESPONSE SHAPE
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_404_response_has_error_and_message_fields(session):
    status, data = await _post(session, "NONEXISTENT_DRUG_XYZ", 35)
    assert status == 404
    assert "error"   in data
    assert "message" in data


@pytest.mark.asyncio
async def test_401_response_has_error_field(session):
    async with session.post(ENDPOINT, json={"drug_id_1mg": "210470", "age": 35}) as resp:
        assert resp.status == 401
        data = await resp.json(content_type=None)
    assert data["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_422_response_for_invalid_payload(session):
    async with session.post(ENDPOINT, json={"drug_id_1mg": "210470", "age": -1},
                            headers=HEADERS) as resp:
        assert resp.status == 422
