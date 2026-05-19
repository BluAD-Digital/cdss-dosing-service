import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.response import DosingResponse, DosingRow

API_KEY = "test-api-key"
HEADERS = {"X-API-Key": API_KEY}
URL = "/api/v1/dosing"

SAMPLE_RESPONSE = DosingResponse(
    drug_id_1mg="457491",
    brand_name="Crocin",
    salt_composition="Paracetamol 500mg",
    generic_name="Paracetamol",
    age_group="adult",
    dosing=[
        DosingRow(
            frequency="twice daily",
            route="oral",
            dose_amount="500",
            dose_unit="mg",
            duration="5 days",
            indication="pain relief",
            instructions="take with food",
        )
    ],
    cached=False,
    query_time_ms=14.2,
)


def test_dosing_success(app_client):
    with patch("app.api.v1.routers.dosing.dosing_service.get_dosing", new=AsyncMock(return_value=SAMPLE_RESPONSE)):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35}, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["drug_id_1mg"] == "457491"
    assert data["age_group"] == "adult"
    assert len(data["dosing"]) == 1
    assert data["cached"] is False


def test_dosing_not_found(app_client):
    from fastapi import HTTPException

    with patch(
        "app.api.v1.routers.dosing.dosing_service.get_dosing",
        new=AsyncMock(side_effect=HTTPException(status_code=404, detail={"error": "not_found", "message": "No data"})),
    ):
        resp = app_client.post(URL, json={"drug_id_1mg": "000000", "age": 35}, headers=HEADERS)
    assert resp.status_code == 404


def test_dosing_missing_api_key(app_client):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35})
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_dosing_invalid_api_key(app_client):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35}, headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401


def test_dosing_invalid_age(app_client):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": -1}, headers=HEADERS)
    assert resp.status_code == 422


def test_dosing_invalid_age_too_high(app_client):
    resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 121}, headers=HEADERS)
    assert resp.status_code == 422


def test_dosing_cache_hit(app_client):
    cached_response = SAMPLE_RESPONSE.model_copy(update={"cached": True, "query_time_ms": 0.0})
    with patch("app.api.v1.routers.dosing.dosing_service.get_dosing", new=AsyncMock(return_value=cached_response)):
        resp = app_client.post(URL, json={"drug_id_1mg": "457491", "age": 35}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["cached"] is True
    assert resp.json()["query_time_ms"] == 0.0


def test_health_endpoint(app_client):
    resp = app_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "db" in data
    assert "cache" in data
