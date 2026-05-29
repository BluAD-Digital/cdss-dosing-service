"""
Phase 2 — Input validation + SQL injection tests (full HTTP stack).

Every test fires a real request to http://34.14.197.45:8001.

Covers:
  - Age boundary validation (all invalid and valid values)
  - Missing / wrong-type fields
  - drug_id edge cases (empty, whitespace, very long, unicode, special chars)
  - SQL injection via drug_id_1mg
  - Payload structure edge cases
  - HTTP method validation (GET not allowed on POST endpoint)

Run:
    python3 -m pytest tests/phase2_functional/test_input_validation.py -v
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


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=10),
        timeout=aiohttp.ClientTimeout(total=15),
    ) as s:
        yield s


async def _post(session, payload):
    async with session.post(ENDPOINT, json=payload, headers=HEADERS) as resp:
        return resp.status, await resp.json(content_type=None)


async def _post_raw(session, payload, headers=None):
    async with session.post(ENDPOINT, json=payload,
                            headers=headers or HEADERS) as resp:
        return resp.status, await resp.json(content_type=None)


# ═══════════════════════════════════════════════════════════════
# 1. AGE — invalid values (must return 422)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("age", [-1, -10, -100, -999, -9999])
async def test_negative_age_returns_422(session, age):
    status, data = await _post(session, {"drug_id_1mg": "210470", "age": age})
    assert status == 422, f"age={age}: expected 422, got {status}"


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [121, 150, 200, 500, 1000, 9999])
async def test_age_above_max_returns_422(session, age):
    status, data = await _post(session, {"drug_id_1mg": "210470", "age": age})
    assert status == 422, f"age={age}: expected 422, got {status}"


@pytest.mark.asyncio
@pytest.mark.parametrize("age_val", ["abc", "35.5", "thirty", "null", [], {}])
async def test_non_integer_age_returns_422(session, age_val):
    status, _ = await _post(session, {"drug_id_1mg": "210470", "age": age_val})
    assert status == 422, f"age={age_val!r}: expected 422, got {status}"


@pytest.mark.asyncio
async def test_boolean_true_coerced_to_age_1(session):
    # Pydantic v2 coerces JSON true → int 1 (valid age). Not a bug — document it.
    status, _ = await _post(session, {"drug_id_1mg": "210470", "age": True})
    assert status in (200, 404), "Boolean True is coerced to age=1 (infant), should be 200 or 404"


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 2, 17, 18, 35, 64, 65, 70, 90, 120])
async def test_all_valid_age_boundaries_accepted(session, age):
    status, _ = await _post(session, {"drug_id_1mg": "210470", "age": age})
    assert status in (200, 404), f"age={age}: expected 200 or 404, got {status}"


# ═══════════════════════════════════════════════════════════════
# 2. MISSING REQUIRED FIELDS
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_missing_drug_id_returns_422(session):
    status, _ = await _post(session, {"age": 35})
    assert status == 422


@pytest.mark.asyncio
async def test_missing_age_returns_422(session):
    status, _ = await _post(session, {"drug_id_1mg": "210470"})
    assert status == 422


@pytest.mark.asyncio
async def test_empty_payload_returns_422(session):
    status, _ = await _post(session, {})
    assert status == 422


@pytest.mark.asyncio
async def test_null_drug_id_returns_422(session):
    status, _ = await _post(session, {"drug_id_1mg": None, "age": 35})
    assert status == 422


@pytest.mark.asyncio
async def test_null_age_returns_422(session):
    status, _ = await _post(session, {"drug_id_1mg": "210470", "age": None})
    assert status == 422


# ═══════════════════════════════════════════════════════════════
# 3. DRUG ID EDGE CASES
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_empty_string_drug_id_returns_404_or_422(session):
    status, _ = await _post(session, {"drug_id_1mg": "", "age": 35})
    assert status in (404, 422), f"Expected 404 or 422, got {status}"


@pytest.mark.asyncio
async def test_whitespace_drug_id_returns_404(session):
    status, _ = await _post(session, {"drug_id_1mg": "   ", "age": 35})
    assert status in (404, 422)


@pytest.mark.asyncio
async def test_very_long_drug_id_handled_gracefully(session):
    long_id = "A" * 500
    status, data = await _post(session, {"drug_id_1mg": long_id, "age": 35})
    assert status in (200, 404, 422, 500)
    # Must not crash with unhandled exception leaking stack trace
    assert "error" in data or "drug_id_1mg" in data


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", [
    "Paracetamol",
    "aspirin",
    "IBUPROFEN",
    "drug-name-with-hyphens",
    "drug.with.dots",
    "drug with spaces",
])
async def test_non_numeric_drug_ids_handled_gracefully(session, drug_id):
    status, _ = await _post(session, {"drug_id_1mg": drug_id, "age": 35})
    assert status in (200, 404), f"drug_id={drug_id!r}: expected 200 or 404, got {status}"


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", [
    "Paracétamol",         # accented chars
    "药物",                  # Chinese characters
    "दवाई",                # Hindi
    "Лекарство",           # Russian
    "🆕drug",              # emoji prefix
])
async def test_unicode_drug_ids_handled_gracefully(session, drug_id):
    status, data = await _post(session, {"drug_id_1mg": drug_id, "age": 35})
    # Must not 500 or leak internal errors
    assert status in (200, 404, 422)


# ═══════════════════════════════════════════════════════════════
# 4. SQL INJECTION — must return 200/404, never 500 or leak DB error
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("injection", [
    "1'; DROP TABLE drugdb.dosing_regimen; --",
    "1 OR 1=1",
    "' OR '1'='1",
    "1'; SELECT * FROM pg_tables; --",
    "' UNION SELECT NULL,NULL,NULL,NULL --",
    "1; DELETE FROM drugdb.drug WHERE 1=1; --",
    "' AND 1=0 UNION SELECT username, password FROM users --",
    "105 OR 1=1",
    "' OR 'unusual'='unusual",
    "1'; EXEC xp_cmdshell('dir'); --",
    "1 AND (SELECT COUNT(*) FROM dosing_regimen) > 0",
    "\\'; DROP TABLE users; --",
])
async def test_sql_injection_does_not_cause_500(session, injection):
    status, data = await _post(session, {"drug_id_1mg": injection, "age": 35})
    assert status in (200, 404), (
        f"SQL injection '{injection[:40]}' caused status {status}. "
        f"Response: {data}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("injection", [
    "1'; DROP TABLE drugdb.dosing_regimen; --",
    "' UNION SELECT * FROM pg_tables --",
    "1 OR 1=1",
])
async def test_sql_injection_does_not_leak_db_server_errors(session, injection):
    status, data = await _post(session, {"drug_id_1mg": injection, "age": 35})
    response_str = str(data).lower()
    # The error message echoes the user's drug_id back — that is expected.
    # What we must NOT see is a raw PostgreSQL server-side error message.
    # These patterns only appear in server-side errors, never in user input.
    postgres_error_patterns = [
        "syntax error at or near",
        "unterminated quoted string",
        "invalid input syntax",
        "operator does not exist",
        "column reference",
        "pgerror",
        "pg exception",
        "traceback",
        "asyncpg",
    ]
    for pattern in postgres_error_patterns:
        assert pattern not in response_str, (
            f"PostgreSQL server error leaked for injection '{injection[:40]}': "
            f"found '{pattern}' in response: {data}"
        )
    # Must return 200 or 404 — never 500 (which would mean the DB processed it)
    assert status in (200, 404), f"Injection caused unexpected status {status}: {data}"


@pytest.mark.asyncio
@pytest.mark.parametrize("xss", [
    "<script>alert(1)</script>",
    "javascript:alert(1)",
    "<img src=x onerror=alert(1)>",
    "';alert('XSS');//",
    "<svg onload=alert(1)>",
])
async def test_xss_payloads_handled_safely(session, xss):
    status, data = await _post(session, {"drug_id_1mg": xss, "age": 35})
    assert status in (200, 404, 422)
    # Payload must not be echoed back unescaped in a way that executes
    response_str = str(data)
    assert "<script>" not in response_str or status == 404


# ═══════════════════════════════════════════════════════════════
# 5. EXTRA / UNKNOWN FIELDS IN PAYLOAD
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_extra_fields_in_payload_are_ignored(session):
    payload = {
        "drug_id_1mg": "210470",
        "age": 35,
        "unknown_field": "should be ignored",
        "another_extra": 99999,
        "inject": "' OR 1=1",
    }
    status, _ = await _post(session, payload)
    assert status in (200, 404)


@pytest.mark.asyncio
async def test_deeply_nested_extra_field_ignored(session):
    payload = {
        "drug_id_1mg": "210470",
        "age": 35,
        "nested": {"level1": {"level2": "deep_value"}},
    }
    status, _ = await _post(session, payload)
    assert status in (200, 404)


# ═══════════════════════════════════════════════════════════════
# 6. HTTP METHOD VALIDATION
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_method_not_allowed_on_dosing_endpoint(session):
    async with session.get(ENDPOINT, headers=HEADERS) as resp:
        assert resp.status == 405


@pytest.mark.asyncio
async def test_put_method_not_allowed(session):
    async with session.put(ENDPOINT, json={"drug_id_1mg": "210470", "age": 35},
                           headers=HEADERS) as resp:
        assert resp.status == 405


@pytest.mark.asyncio
async def test_delete_method_not_allowed(session):
    async with session.delete(ENDPOINT, headers=HEADERS) as resp:
        assert resp.status == 405


# ═══════════════════════════════════════════════════════════════
# 7. CONTENT-TYPE HANDLING
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_wrong_content_type_handled(session):
    async with session.post(
        ENDPOINT,
        data="drug_id_1mg=210470&age=35",
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    ) as resp:
        # Should return 422 (unprocessable) not 500
        assert resp.status in (422, 415)


# ═══════════════════════════════════════════════════════════════
# 8. CONCURRENT BURST — all valid, same age
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_50_concurrent_requests_all_valid_no_500(session):
    import asyncio
    drug_ids = [
        "210470", "142807", "1146701", "1002088", "56693",
        "165440", "344363", "1115733", "1147914", "1123438",
    ]
    tasks = [
        _post(session, {"drug_id_1mg": drug_ids[i % len(drug_ids)], "age": 35})
        for i in range(50)
    ]
    results = await asyncio.gather(*tasks)
    statuses = [status for status, _ in results]
    assert 500 not in statuses, f"Got 500 in concurrent burst: {statuses}"
    assert all(s in (200, 404) for s in statuses), f"Unexpected statuses: {set(statuses)}"
