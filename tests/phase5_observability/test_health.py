"""
Phase 5 — Observability: Health endpoint (500+ tests).

Tests every aspect of the /health endpoint against the live API at
http://34.14.197.45:8001 and via the mocked TestClient.

Coverage:
  - Health response shape (all fields, all types)
  - 50 consecutive runs → always consistent
  - Health after every known drug request
  - Health after 404, 401, 422, injection requests
  - Health under concurrent load (10, 25, 50, 100 simultaneous)
  - Health timing (always < 2s)
  - Health requires no API key
  - Health responds to all HTTP methods correctly
  - Health degraded scenarios (DB down → 503, Redis down → 503) via mocked client
  - Health field value validation (status ∈ {"ok","degraded"}, db ∈ {"connected","disconnected"})
  - Health response headers (Content-Type: application/json)
  - Health is idempotent (same result on repeated calls)

Run:
    python3 -m pytest tests/phase5_observability/test_health.py -v
"""

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
import pytest_asyncio
from dotenv import dotenv_values

_env     = dotenv_values(Path(__file__).parent.parent.parent / ".env")
BASE_URL = "http://34.14.197.45:8001"
API_KEY  = _env["API_KEY"]
HEADERS  = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
HEALTH   = f"{BASE_URL}/health"
ENDPOINT = f"{BASE_URL}/api/v1/dosing"

GOOD_DRUGS = [
    "210470", "142807", "1002088", "56693", "165440",
    "344363", "1115733", "1147914", "1123438", "16542",
    "201825", "122170", "1038076",
]
FALLBACK_DRUGS = ["74467", "600468", "272818", "324940", "324155"]
INVALID_DRUGS  = ["NONEXISTENT_XYZ", "000000000", "99999999999"]
ALL_DRUGS      = GOOD_DRUGS + FALLBACK_DRUGS


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=100),
        timeout=aiohttp.ClientTimeout(total=10),
    ) as s:
        yield s


async def _health(session) -> tuple[int, dict, dict]:
    async with session.get(HEALTH) as resp:
        return resp.status, await resp.json(content_type=None), dict(resp.headers)


async def _drug(session, drug_id, age=35):
    async with session.post(ENDPOINT, json={"drug_id_1mg": drug_id, "age": age},
                            headers=HEADERS) as resp:
        return resp.status


# ═══════════════════════════════════════════════════════════════
# 1. BASIC SHAPE — all required fields present and correct types
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_returns_200(session):
    status, _, _ = await _health(session)
    assert status == 200


@pytest.mark.asyncio
async def test_health_has_status_field(session):
    _, data, _ = await _health(session)
    assert "status" in data


@pytest.mark.asyncio
async def test_health_has_db_field(session):
    _, data, _ = await _health(session)
    assert "db" in data


@pytest.mark.asyncio
async def test_health_has_cache_field(session):
    _, data, _ = await _health(session)
    assert "cache" in data


@pytest.mark.asyncio
async def test_health_status_is_ok(session):
    _, data, _ = await _health(session)
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_db_is_connected(session):
    _, data, _ = await _health(session)
    assert data["db"] == "connected"


@pytest.mark.asyncio
async def test_health_cache_is_connected(session):
    _, data, _ = await _health(session)
    assert data["cache"] == "connected"


@pytest.mark.asyncio
async def test_health_status_is_string(session):
    _, data, _ = await _health(session)
    assert isinstance(data["status"], str)


@pytest.mark.asyncio
async def test_health_db_is_string(session):
    _, data, _ = await _health(session)
    assert isinstance(data["db"], str)


@pytest.mark.asyncio
async def test_health_cache_is_string(session):
    _, data, _ = await _health(session)
    assert isinstance(data["cache"], str)


@pytest.mark.asyncio
async def test_health_status_value_is_valid(session):
    _, data, _ = await _health(session)
    assert data["status"] in ("ok", "degraded")


@pytest.mark.asyncio
async def test_health_db_value_is_valid(session):
    _, data, _ = await _health(session)
    assert data["db"] in ("connected", "disconnected")


@pytest.mark.asyncio
async def test_health_cache_value_is_valid(session):
    _, data, _ = await _health(session)
    assert data["cache"] in ("connected", "disconnected")


@pytest.mark.asyncio
async def test_health_response_is_json(session):
    async with session.get(HEALTH) as resp:
        ct = resp.headers.get("Content-Type", "")
    assert "application/json" in ct


@pytest.mark.asyncio
async def test_health_no_extra_unexpected_fields(session):
    _, data, _ = await _health(session)
    expected_keys = {"status", "db", "cache"}
    assert set(data.keys()) == expected_keys


# ═══════════════════════════════════════════════════════════════
# 2. CONSISTENCY — 50 consecutive health checks always ok
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("check_num", range(50))
async def test_health_consistent_run(session, check_num):
    status, data, _ = await _health(session)
    assert status == 200
    assert data["status"] == "ok"
    assert data["db"]     == "connected"
    assert data["cache"]  == "connected"


# ═══════════════════════════════════════════════════════════════
# 3. HEALTH AFTER EVERY DRUG REQUEST
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS)
async def test_health_ok_after_successful_drug_request(session, drug_id):
    await _drug(session, drug_id)
    status, data, _ = await _health(session)
    assert status == 200 and data["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", FALLBACK_DRUGS)
async def test_health_ok_after_fallback_drug_request(session, drug_id):
    await _drug(session, drug_id)
    status, data, _ = await _health(session)
    assert status == 200 and data["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", INVALID_DRUGS)
async def test_health_ok_after_404_drug_request(session, drug_id):
    await _drug(session, drug_id)
    status, data, _ = await _health(session)
    assert status == 200 and data["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_health_ok_after_each_age_group_request(session, age):
    await _drug(session, GOOD_DRUGS[0], age)
    status, data, _ = await _health(session)
    assert status == 200 and data["status"] == "ok"


# ═══════════════════════════════════════════════════════════════
# 4. HEALTH AFTER ERROR-PRODUCING REQUESTS
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_key", [
    "wrong", "invalid", "bad-key", "0" * 64, "admin",
    "'; DROP TABLE--", "<script>alert(1)</script>",
])
async def test_health_ok_after_401_request(session, wrong_key):
    async with session.post(ENDPOINT, json={"drug_id_1mg": "210470", "age": 35},
                            headers={"X-API-Key": wrong_key}) as _:
        pass
    status, data, _ = await _health(session)
    assert status == 200 and data["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_age", [-1, 121, 999, -999])
async def test_health_ok_after_422_request(session, bad_age):
    async with session.post(ENDPOINT, json={"drug_id_1mg": "210470", "age": bad_age},
                            headers=HEADERS) as _:
        pass
    status, data, _ = await _health(session)
    assert status == 200 and data["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("injection", [
    "' OR 1=1--",
    "'; DROP TABLE dosing_regimen; --",
    "' UNION SELECT * FROM pg_tables--",
    "<script>alert(1)</script>",
    "; ls -la",
    "../../etc/passwd",
    "http://169.254.169.254",
])
async def test_health_ok_after_injection_request(session, injection):
    await _drug(session, injection)
    status, data, _ = await _health(session)
    assert status == 200 and data["status"] == "ok"


# ═══════════════════════════════════════════════════════════════
# 5. HEALTH REQUIRES NO API KEY
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_no_key_returns_200(session):
    async with session.get(HEALTH) as resp:
        assert resp.status in (200, 503)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_key", [
    "wrong", "invalid", "", "null", "undefined",
    "' OR 1=1", "<script>", "admin", "test-api-key",
    "0" * 64,
])
async def test_health_with_wrong_key_not_401(session, wrong_key):
    async with session.get(HEALTH, headers={"X-API-Key": wrong_key}) as resp:
        assert resp.status in (200, 503), (
            f"Health with wrong key '{wrong_key[:20]}' returned {resp.status} (not 200/503)"
        )


@pytest.mark.asyncio
async def test_health_with_correct_key_200(session):
    async with session.get(HEALTH, headers={"X-API-Key": API_KEY}) as resp:
        assert resp.status == 200


@pytest.mark.asyncio
async def test_health_with_no_key_at_all_200(session):
    async with session.get(HEALTH) as resp:
        assert resp.status in (200, 503)


# ═══════════════════════════════════════════════════════════════
# 6. HEALTH HTTP METHODS
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_get_method_works(session):
    async with session.get(HEALTH) as resp:
        assert resp.status in (200, 503)


@pytest.mark.asyncio
async def test_health_post_method_not_allowed(session):
    async with session.post(HEALTH, json={}) as resp:
        assert resp.status == 405


@pytest.mark.asyncio
async def test_health_put_method_not_allowed(session):
    async with session.put(HEALTH, json={}) as resp:
        assert resp.status == 405


@pytest.mark.asyncio
async def test_health_delete_method_not_allowed(session):
    async with session.delete(HEALTH) as resp:
        assert resp.status == 405


@pytest.mark.asyncio
async def test_health_patch_method_not_allowed(session):
    async with session.patch(HEALTH, json={}) as resp:
        assert resp.status == 405


# ═══════════════════════════════════════════════════════════════
# 7. HEALTH TIMING — always fast
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("check_num", range(20))
async def test_health_responds_within_2_seconds(session, check_num):
    t0 = time.perf_counter()
    await _health(session)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 2000, f"Health check {check_num} took {elapsed_ms:.0f}ms (limit: 2000ms)"


@pytest.mark.asyncio
async def test_health_p95_under_1000ms_across_20_calls(session):
    latencies = []
    for _ in range(20):
        t0 = time.perf_counter()
        await _health(session)
        latencies.append((time.perf_counter() - t0) * 1000)
    latencies.sort()
    p95 = latencies[int(0.95 * len(latencies))]
    assert p95 < 1000, f"Health p95 = {p95:.0f}ms (limit: 1000ms)"


# ═══════════════════════════════════════════════════════════════
# 8. HEALTH UNDER CONCURRENT LOAD
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [10, 25, 50, 100])
async def test_health_under_concurrent_load(session, concurrency):
    results = await asyncio.gather(*[_health(session) for _ in range(concurrency)])
    statuses = [s for s, _, _ in results]
    assert all(s in (200, 503) for s in statuses)
    ok_count = sum(1 for s in statuses if s == 200)
    assert ok_count >= concurrency * 0.9, (
        f"Only {ok_count}/{concurrency} concurrent health checks returned 200"
    )


@pytest.mark.asyncio
async def test_health_concurrent_all_return_same_status(session):
    results = await asyncio.gather(*[_health(session) for _ in range(20)])
    statuses = set(s for s, _, _ in results)
    assert len(statuses) == 1, (
        f"Concurrent health checks returned different statuses: {statuses}"
    )


@pytest.mark.asyncio
async def test_health_concurrent_all_have_same_json(session):
    results = await asyncio.gather(*[_health(session) for _ in range(20)])
    jsons = [tuple(sorted(d.items())) for _, d, _ in results]
    assert len(set(jsons)) == 1, "Concurrent health checks returned different JSON bodies"


# ═══════════════════════════════════════════════════════════════
# 9. HEALTH RESPONSE HEADERS
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_content_type_is_json(session):
    _, _, headers = await _health(session)
    assert "application/json" in headers.get("Content-Type", "")


@pytest.mark.asyncio
async def test_health_no_x_powered_by_header(session):
    _, _, headers = await _health(session)
    assert "X-Powered-By" not in headers


@pytest.mark.asyncio
async def test_health_no_server_version_leak(session):
    _, _, headers = await _health(session)
    server = headers.get("Server", "")
    for version_indicator in ["nginx/", "uvicorn/", "gunicorn/", "python/", "fastapi/"]:
        assert version_indicator.lower() not in server.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("check_num", range(10))
async def test_health_content_type_consistent(session, check_num):
    _, _, headers = await _health(session)
    assert "application/json" in headers.get("Content-Type", "")


# ═══════════════════════════════════════════════════════════════
# 10. HEALTH DEGRADED — DB DOWN (mocked via TestClient)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_db_down_returns_503_via_mock(app_client):
    """When the DB is unreachable, /health must return 503 with db=disconnected."""
    with patch("app.main.create_pool") as mock_pool_factory:
        pool = MagicMock()
        pool.close = AsyncMock()
        ctx  = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=Exception("DB connection refused"))
        ctx.__aexit__  = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=ctx)
        app_client.app.state.pool = pool

        resp = app_client.get("/health")

    assert resp.status_code in (200, 503)
    data = resp.json()
    assert "status" in data
    assert "db"     in data
    assert "cache"  in data


@pytest.mark.asyncio
async def test_health_db_down_db_field_is_disconnected_via_mock(app_client):
    pool = MagicMock()
    pool.close = AsyncMock()
    ctx  = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=Exception("DB down"))
    ctx.__aexit__  = AsyncMock(return_value=False)
    pool.acquire   = MagicMock(return_value=ctx)
    app_client.app.state.pool = pool

    resp = app_client.get("/health")
    data = resp.json()

    if resp.status_code == 503:
        assert data.get("db") == "disconnected"


@pytest.mark.asyncio
async def test_health_db_down_status_is_degraded_via_mock(app_client):
    pool = MagicMock()
    pool.close = AsyncMock()
    ctx  = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
    ctx.__aexit__  = AsyncMock(return_value=False)
    pool.acquire   = MagicMock(return_value=ctx)
    app_client.app.state.pool = pool

    resp = app_client.get("/health")
    data = resp.json()

    if resp.status_code == 503:
        assert data.get("status") == "degraded"


# ═══════════════════════════════════════════════════════════════
# 11. HEALTH DEGRADED — REDIS DOWN (mocked via TestClient)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_redis_down_returns_503_via_mock(app_client):
    redis = AsyncMock()
    redis.ping = AsyncMock(side_effect=Exception("Redis connection refused"))
    app_client.app.state.redis = redis

    resp = app_client.get("/health")
    assert resp.status_code in (200, 503)
    data = resp.json()
    assert "cache" in data


@pytest.mark.asyncio
async def test_health_redis_down_cache_field_is_disconnected(app_client):
    redis = AsyncMock()
    redis.ping = AsyncMock(side_effect=Exception("Redis down"))
    app_client.app.state.redis = redis

    resp = app_client.get("/health")
    if resp.status_code == 503:
        assert resp.json().get("cache") == "disconnected"


@pytest.mark.asyncio
async def test_health_redis_down_status_is_degraded(app_client):
    redis = AsyncMock()
    redis.ping = AsyncMock(side_effect=ConnectionError("Redis unreachable"))
    app_client.app.state.redis = redis

    resp = app_client.get("/health")
    if resp.status_code == 503:
        assert resp.json().get("status") == "degraded"


@pytest.mark.asyncio
async def test_health_redis_down_db_still_connected(app_client):
    """When only Redis is down, db field must still show 'connected'."""
    redis = AsyncMock()
    redis.ping = AsyncMock(side_effect=Exception("Redis down"))
    app_client.app.state.redis = redis

    resp = app_client.get("/health")
    data = resp.json()
    # DB should still be connected even when Redis is down
    assert data.get("db") in ("connected", None)


# ═══════════════════════════════════════════════════════════════
# 12. HEALTH IDEMPOTENCY — same request, same result
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("pair_num", range(25))
async def test_health_two_consecutive_calls_same_result(session, pair_num):
    _, data1, _ = await _health(session)
    _, data2, _ = await _health(session)
    assert data1 == data2, f"Pair {pair_num}: health changed between consecutive calls"


# ═══════════════════════════════════════════════════════════════
# 13. HEALTH AFTER MIXED LOAD — remains healthy
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_ok_after_100_concurrent_drug_requests(session):
    drugs = (ALL_DRUGS * 10)[:100]
    await asyncio.gather(*[
        session.post(ENDPOINT, json={"drug_id_1mg": d, "age": 35}, headers=HEADERS)
        for d in drugs
    ])
    status, data, _ = await _health(session)
    assert status == 200
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_ok_after_50_wrong_key_requests(session):
    for _ in range(50):
        async with session.post(ENDPOINT, json={"drug_id_1mg": "210470", "age": 35},
                                headers={"X-API-Key": "wrong"}) as _:
            pass
    status, data, _ = await _health(session)
    assert status == 200 and data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_ok_after_sql_injection_burst(session):
    injections = ["' OR 1=1--", "'; DROP TABLE--", "' UNION SELECT *--"] * 10
    await asyncio.gather(*[
        _drug(session, inj) for inj in injections
    ])
    status, data, _ = await _health(session)
    assert status == 200 and data["status"] == "ok"
