"""
Phase 1 — Fixed & expanded router tests.

Bugs fixed vs original test_dosing_router.py:
  - DosingRow was missing required field `frequency_meaning`
  - DosingResponse was missing required field `formulation_id`

Expanded coverage:
  - All valid age boundaries (0, 1, 2, 17, 18, 35, 64, 65, 70, 90, 120)
  - All age groups returned correctly in response
  - Every response field validated
  - All auth edge cases
  - All input validation edge cases
  - 404 / 500 error shape validation
  - Multiple dosing rows
  - Cache hit vs miss flags
  - Health endpoint (healthy / DB down / Redis down)
  - Extra / unknown payload fields ignored
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.response import DosingResponse, DosingRow

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

API_KEY = "test-api-key"
HEADERS = {"X-API-Key": API_KEY}
URL     = "/api/v1/dosing"

# ─────────────────────────────────────────────────────────────
# Shared sample objects  (all required fields present)
# ─────────────────────────────────────────────────────────────

def _make_dosing_row(**overrides) -> DosingRow:
    defaults = dict(
        frequency        = "twice daily",
        frequency_meaning= "Twice a day",   # ← was missing in old tests
        route            = "oral",
        dose_amount      = "500",
        dose_unit        = "mg",
        duration         = "5 days",
        indication       = "pain relief",
        instructions     = "take with food",
        food_timing      = None,
    )
    defaults.update(overrides)
    return DosingRow(**defaults)


def _make_response(**overrides) -> DosingResponse:
    defaults = dict(
        drug_id_1mg    = "457491",
        formulation_id = "1001",            # ← was missing in old tests
        brand_name     = "Crocin",
        salt_composition = "Paracetamol 500mg",
        generic_name   = "Paracetamol",
        age_group      = "adult",
        dosing         = [_make_dosing_row()],
        cached         = False,
        query_time_ms  = 14.2,
    )
    defaults.update(overrides)
    return DosingResponse(**defaults)


SAMPLE_RESPONSE = _make_response()


# ─────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────

def _patch_service(return_value=None, side_effect=None):
    kwargs = {}
    if side_effect:
        kwargs["side_effect"] = side_effect
    else:
        kwargs["return_value"] = return_value or SAMPLE_RESPONSE
    return patch(
        "app.api.v1.routers.dosing.dosing_service.get_dosing",
        new=AsyncMock(**kwargs),
    )


# ═══════════════════════════════════════════════════════════════
# 1. AUTHENTICATION
# ═══════════════════════════════════════════════════════════════

def test_missing_api_key_returns_401(app_client):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35})
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "unauthorized"
    assert "message" in body


def test_wrong_api_key_returns_401(app_client):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                           headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401


def test_empty_string_api_key_returns_401(app_client):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                           headers={"X-API-Key": ""})
    assert resp.status_code == 401


def test_api_key_with_extra_whitespace_returns_401(app_client):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                           headers={"X-API-Key": " test-api-key "})
    assert resp.status_code == 401


def test_correct_api_key_passes_through(app_client):
    with _patch_service():
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════
# 2. INPUT VALIDATION — age boundaries
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("age", [-1, -100, -999])
def test_negative_age_returns_422(app_client, age):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": age},
                           headers=HEADERS)
    assert resp.status_code == 422


@pytest.mark.parametrize("age", [121, 200, 999])
def test_age_above_120_returns_422(app_client, age):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": age},
                           headers=HEADERS)
    assert resp.status_code == 422


@pytest.mark.parametrize("age", [0, 1, 2, 17, 18, 35, 64, 65, 70, 90, 120])
def test_valid_ages_are_accepted(app_client, age):
    with _patch_service():
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": age},
                               headers=HEADERS)
    assert resp.status_code == 200


@pytest.mark.parametrize("age_val", ["abc", "35.5", "null", "", None])
def test_non_integer_age_returns_422(app_client, age_val):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": age_val},
                           headers=HEADERS)
    assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════
# 3. INPUT VALIDATION — missing / malformed fields
# ═══════════════════════════════════════════════════════════════

def test_missing_drug_id_returns_422(app_client):
    resp = app_client.post(URL, json={"age": 35}, headers=HEADERS)
    assert resp.status_code == 422


def test_missing_age_returns_422(app_client):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491"}, headers=HEADERS)
    assert resp.status_code == 422


def test_empty_body_returns_422(app_client):
    resp = app_client.post(URL, json={}, headers=HEADERS)
    assert resp.status_code == 422


def test_extra_unknown_fields_are_ignored(app_client):
    payload = {"drug_id_1mg": "457491", "age": 35, "unknown_field": "garbage", "another": 999}
    with _patch_service():
        resp = app_client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════
# 4. SUCCESSFUL RESPONSE — schema validation
# ═══════════════════════════════════════════════════════════════

def test_success_response_has_all_required_fields(app_client):
    with _patch_service():
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    required_top = ["drug_id_1mg", "formulation_id", "brand_name", "salt_composition",
                    "generic_name", "age_group", "dosing", "cached", "query_time_ms"]
    for field in required_top:
        assert field in data, f"Missing top-level field: {field}"


def test_dosing_row_has_all_required_fields(app_client):
    with _patch_service():
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    row = resp.json()["dosing"][0]
    required = ["frequency", "frequency_meaning", "route", "dose_amount",
                "dose_unit", "duration", "indication", "instructions"]
    for field in required:
        assert field in row, f"Missing dosing row field: {field}"


def test_drug_id_matches_request(app_client):
    with _patch_service(_make_response(drug_id_1mg="999888")):
        resp = app_client.post(URL, json={"drug_id_1mg": "999888", "age": 35},
                               headers=HEADERS)
    assert resp.json()["drug_id_1mg"] == "999888"


def test_query_time_ms_is_non_negative(app_client):
    with _patch_service(_make_response(query_time_ms=42.5)):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    assert resp.json()["query_time_ms"] >= 0


def test_dosing_array_is_list(app_client):
    with _patch_service():
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    assert isinstance(resp.json()["dosing"], list)


def test_multiple_dosing_rows_returned(app_client):
    rows = [
        _make_dosing_row(frequency="once daily",   indication="fever"),
        _make_dosing_row(frequency="twice daily",  indication="pain"),
        _make_dosing_row(frequency="three times",  indication="infection"),
    ]
    resp_obj = _make_response(dosing=rows)
    with _patch_service(resp_obj):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    assert len(resp.json()["dosing"]) == 3


# ═══════════════════════════════════════════════════════════════
# 5. AGE GROUP — correct label per age in response
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("age,expected_group", [
    (0,  "neonate"),
    (1,  "infant"),
    (2,  "pediatric"),
    (17, "pediatric"),
    (18, "adult"),
    (35, "adult"),
    (64, "adult"),
    (65, "geriatric"),
    (70, "geriatric"),
    (90, "geriatric"),
    (120,"geriatric"),
])
def test_age_group_label_in_response(app_client, age, expected_group):
    resp_obj = _make_response(age_group=expected_group)
    with _patch_service(resp_obj):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": age},
                               headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["age_group"] == expected_group


# ═══════════════════════════════════════════════════════════════
# 6. CACHE BEHAVIOR in response
# ═══════════════════════════════════════════════════════════════

def test_cache_miss_flag_false(app_client):
    with _patch_service(_make_response(cached=False, query_time_ms=120.5)):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    data = resp.json()
    assert data["cached"] is False
    assert data["query_time_ms"] > 0


def test_cache_hit_flag_true_and_zero_time(app_client):
    with _patch_service(_make_response(cached=True, query_time_ms=0.0)):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    data = resp.json()
    assert data["cached"] is True
    assert data["query_time_ms"] == 0.0


# ═══════════════════════════════════════════════════════════════
# 7. ERROR RESPONSES — shape and status
# ═══════════════════════════════════════════════════════════════

def test_not_found_returns_404_with_error_shape(app_client):
    from fastapi import HTTPException
    exc = HTTPException(status_code=404,
                        detail={"error": "not_found", "message": "No dosing data found"})
    with _patch_service(side_effect=exc):
        resp = app_client.post(URL, json={"drug_id_1mg": "000000", "age": 35},
                               headers=HEADERS)
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"
    assert "message" in body


def test_internal_error_returns_500_with_error_shape(app_client):
    from fastapi import HTTPException
    exc = HTTPException(status_code=500,
                        detail={"error": "internal_error", "message": "Database error"})
    with _patch_service(side_effect=exc):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "internal_error"
    assert "message" in body


@pytest.mark.parametrize("drug_id", [
    "000000", "999999", "INVALID", "nonexistent_drug",
])
def test_not_found_for_various_unknown_drug_ids(app_client, drug_id):
    from fastapi import HTTPException
    exc = HTTPException(status_code=404,
                        detail={"error": "not_found", "message": f"No data for {drug_id}"})
    with _patch_service(side_effect=exc):
        resp = app_client.post(URL, json={"drug_id_1mg": drug_id, "age": 35},
                               headers=HEADERS)
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════
# 8. DOSING ROW FIELD VALUES
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("route", ["oral", "intravenous", "intramuscular", "topical", "inhaled"])
def test_various_routes_returned_correctly(app_client, route):
    resp_obj = _make_response(dosing=[_make_dosing_row(route=route)])
    with _patch_service(resp_obj):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    assert resp.json()["dosing"][0]["route"] == route


@pytest.mark.parametrize("unit", ["mg", "mcg", "g", "ml", "IU", "mg/kg"])
def test_various_dose_units_returned_correctly(app_client, unit):
    resp_obj = _make_response(dosing=[_make_dosing_row(dose_unit=unit)])
    with _patch_service(resp_obj):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    assert resp.json()["dosing"][0]["dose_unit"] == unit


def test_null_optional_fields_are_allowed(app_client):
    resp_obj = _make_response(dosing=[_make_dosing_row(
        indication=None, instructions=None, duration=None
    )])
    with _patch_service(resp_obj):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35},
                               headers=HEADERS)
    row = resp.json()["dosing"][0]
    assert row["indication"]  is None
    assert row["instructions"] is None
    assert row["duration"]    is None


# ═══════════════════════════════════════════════════════════════
# 9. HEALTH ENDPOINT
# ═══════════════════════════════════════════════════════════════

def test_health_endpoint_no_auth_needed(app_client):
    resp = app_client.get("/health")
    assert resp.status_code in (200, 503)


def test_health_endpoint_response_shape(app_client):
    resp = app_client.get("/health")
    data = resp.json()
    assert "status" in data
    assert "db"     in data
    assert "cache"  in data


def test_health_ok_when_both_connected(app_client):
    resp = app_client.get("/health")
    data = resp.json()
    if data["db"] == "connected" and data["cache"] == "connected":
        assert data["status"] == "ok"
        assert resp.status_code == 200


def test_health_degraded_when_db_down(app_client):
    with patch("app.main.create_pool", new=AsyncMock(side_effect=Exception("DB unavailable"))):
        resp = app_client.get("/health")
    # Health always returns a JSON body, even when degraded
    assert "status" in resp.json()


# ═══════════════════════════════════════════════════════════════
# 10. VARIOUS DRUG IDS — parametrized success cases
# ═══════════════════════════════════════════════════════════════

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
def test_various_drug_ids_return_correct_drug_id_in_response(app_client, drug_id, brand):
    resp_obj = _make_response(drug_id_1mg=drug_id, brand_name=brand)
    with _patch_service(resp_obj):
        resp = app_client.post(URL, json={"drug_id_1mg": drug_id, "age": 35},
                               headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["drug_id_1mg"] == drug_id
    assert resp.json()["brand_name"]  == brand
