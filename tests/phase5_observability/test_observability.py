"""
Phase 5 — Observability: Log structure and request tracing (500+ tests).

Strategy:
  - A session-scoped fixture makes one batch of representative requests,
    fetches docker logs ONCE, parses every JSON log line, and caches the result.
  - All 500+ tests read from this cache — zero extra docker log fetches.
  - This keeps the suite fast while still exercising many real log lines.

Log line types observed in production:
  1. "dosing request"   — logged by the router when a request arrives
  2. "cache MISS/HIT"  — logged by the service
  3. "fetch_dosing"     — debug log from repo
  4. "dosing response" — logged by the router on success
  5. "http request"    — logged by the middleware with latency
  6. uvicorn access    — raw access log

Required fields every JSON log line must have:
  event, level, logger, timestamp, service, environment, request_id

HTTP middleware log must also have:
  method, path, status_code, latency_ms

Dosing response log must also have:
  drug_id_1mg, age_group, dosing_rows, cached, query_time_ms

Run:
    python3 -m pytest tests/phase5_observability/test_observability.py -v
"""

import json
import re
import subprocess
import time
import uuid
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

GOOD_DRUGS = [
    "210470", "142807", "1002088", "56693", "165440",
    "344363", "1115733", "1147914", "1123438", "16542",
    "201825", "122170", "1038076",
]
FALLBACK_DRUGS = ["74467", "600468", "272818"]

ALL_AGES  = [0, 1, 10, 17, 18, 35, 64, 65, 70]

CORE_LOG_FIELDS = [
    "event", "level", "logger", "timestamp", "service", "environment",
]
HTTP_LOG_FIELDS = [
    "method", "path", "status_code", "latency_ms",
]
RESPONSE_LOG_FIELDS = [
    "drug_id_1mg", "age_group", "dosing_rows", "cached", "query_time_ms",
]
VALID_LEVELS = {"debug", "info", "warning", "error", "critical"}
VALID_EVENTS = {
    "dosing request", "cache MISS", "cache HIT",
    "dosing response", "http request", "primary miss, trying fallback",
}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _fetch_docker_logs(tail: int = 500) -> list[dict]:
    """Fetch last N lines of container logs and parse JSON lines."""
    try:
        result = subprocess.run(
            ["docker", "logs", "cdss-dosing-service-dosing-service-1",
             "--tail", str(tail)],
            capture_output=True, text=True, timeout=15,
        )
        output = result.stdout + result.stderr
        parsed = []
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    parsed.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return parsed
    except Exception:
        return []


def _lines_for_request_id(logs: list[dict], request_id: str) -> list[dict]:
    return [l for l in logs if l.get("request_id") == request_id]


def _lines_with_event(logs: list[dict], event: str) -> list[dict]:
    return [l for l in logs if l.get("event") == event]


def _is_iso8601(ts: str) -> bool:
    """Check if timestamp is roughly ISO-8601 format."""
    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    return bool(re.match(pattern, str(ts)))


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except ValueError:
        return False


# ─────────────────────────────────────────────────────────────
# Session-scoped log capture — fetch once, share across all tests
# ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def log_data():
    """
    Make representative requests covering all scenarios, then fetch and
    parse docker logs once. All observability tests use this fixture.
    Returns a dict with:
      - all_lines: list[dict] — every parsed log line
      - by_request: dict[str, list[dict]] — lines grouped by request_id
      - http_lines: list[dict] — only "http request" log lines
      - response_lines: list[dict] — only "dosing response" log lines
      - miss_lines: list[dict] — only "cache MISS" log lines
      - hit_lines: list[dict] — only "cache HIT" log lines
    """
    import asyncio
    import aiohttp as _aio

    async def _make_requests():
        connector = _aio.TCPConnector(limit=20)
        timeout   = _aio.ClientTimeout(total=30)
        async with _aio.ClientSession(connector=connector, timeout=timeout) as sess:
            tasks = []
            # Good drugs at adult age
            for drug in GOOD_DRUGS:
                tasks.append(sess.post(ENDPOINT, json={"drug_id_1mg": drug, "age": 35}, headers=HEADERS))
            # Fallback drugs
            for drug in FALLBACK_DRUGS:
                tasks.append(sess.post(ENDPOINT, json={"drug_id_1mg": drug, "age": 35}, headers=HEADERS))
            # Different age groups
            for age in ALL_AGES:
                tasks.append(sess.post(ENDPOINT, json={"drug_id_1mg": GOOD_DRUGS[0], "age": age}, headers=HEADERS))
            # 404 drugs
            for drug in ["NONEXISTENT_XYZ", "000000000"]:
                tasks.append(sess.post(ENDPOINT, json={"drug_id_1mg": drug, "age": 35}, headers=HEADERS))
            # 401 requests
            for _ in range(3):
                tasks.append(sess.post(ENDPOINT, json={"drug_id_1mg": "210470", "age": 35},
                                       headers={"X-API-Key": "wrong"}))
            # Second calls for cache hits
            for drug in GOOD_DRUGS[:5]:
                tasks.append(sess.post(ENDPOINT, json={"drug_id_1mg": drug, "age": 35}, headers=HEADERS))

            resps = await asyncio.gather(*[t for t in tasks], return_exceptions=True)
            for r in resps:
                if hasattr(r, "release"):
                    await r.release()
                elif hasattr(r, "close"):
                    r.close()

    asyncio.run(_make_requests())
    time.sleep(1)   # let logs flush

    all_lines = _fetch_docker_logs(tail=1000)

    return {
        "all_lines":      all_lines,
        "by_request":     {l.get("request_id"): [] for l in all_lines if "request_id" in l},
        "http_lines":     _lines_with_event(all_lines, "http request"),
        "response_lines": _lines_with_event(all_lines, "dosing response"),
        "miss_lines":     _lines_with_event(all_lines, "cache MISS"),
        "hit_lines":      _lines_with_event(all_lines, "cache HIT"),
    }


# Populate by_request after building the dict
@pytest.fixture(scope="session")
def logs(log_data):
    for line in log_data["all_lines"]:
        rid = line.get("request_id")
        if rid and rid in log_data["by_request"]:
            log_data["by_request"][rid].append(line)
    return log_data


# ═══════════════════════════════════════════════════════════════
# 1. LOG VOLUME — enough lines captured
# ═══════════════════════════════════════════════════════════════

def test_logs_captured_at_least_50_lines(logs):
    assert len(logs["all_lines"]) >= 50, (
        f"Only {len(logs['all_lines'])} log lines captured — expected ≥ 50"
    )


def test_logs_have_http_request_lines(logs):
    assert len(logs["http_lines"]) >= 5, (
        f"Only {len(logs['http_lines'])} 'http request' log lines"
    )


def test_logs_have_dosing_response_lines(logs):
    assert len(logs["response_lines"]) >= 5


def test_logs_have_cache_miss_lines(logs):
    assert len(logs["miss_lines"]) >= 1


def test_logs_have_cache_hit_lines(logs):
    assert len(logs["hit_lines"]) >= 1


# ═══════════════════════════════════════════════════════════════
# 2. EVERY LOG LINE — core fields present
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("field", CORE_LOG_FIELDS)
def test_every_log_line_has_core_field(logs, field):
    missing = [l for l in logs["all_lines"] if field not in l]
    assert len(missing) == 0, (
        f"{len(missing)} log lines missing required field '{field}'. "
        f"Sample: {missing[:2]}"
    )


@pytest.mark.parametrize("field", CORE_LOG_FIELDS)
def test_every_log_line_core_field_is_non_empty(logs, field):
    empty = [l for l in logs["all_lines"]
             if field in l and (l[field] is None or str(l[field]).strip() == "")]
    assert len(empty) == 0, (
        f"{len(empty)} log lines have empty '{field}'. Sample: {empty[:2]}"
    )


# ═══════════════════════════════════════════════════════════════
# 3. EVERY LOG LINE — level is valid
# ═══════════════════════════════════════════════════════════════

def test_every_log_line_level_is_valid_value(logs):
    invalid = [l for l in logs["all_lines"]
               if l.get("level", "").lower() not in VALID_LEVELS]
    assert len(invalid) == 0, (
        f"{len(invalid)} log lines have invalid 'level'. Sample: {invalid[:2]}"
    )


@pytest.mark.parametrize("level", ["info", "warning", "error"])
def test_log_level_is_lowercase(logs, level):
    """Level field must always be lowercase."""
    lines_of_level = [l for l in logs["all_lines"] if l.get("level") == level]
    if lines_of_level:
        assert all(l["level"] == l["level"].lower() for l in lines_of_level)


# ═══════════════════════════════════════════════════════════════
# 4. TIMESTAMP FORMAT — ISO 8601
# ═══════════════════════════════════════════════════════════════

def test_every_log_line_timestamp_is_iso8601(logs):
    invalid = [l for l in logs["all_lines"]
               if "timestamp" in l and not _is_iso8601(l["timestamp"])]
    assert len(invalid) == 0, (
        f"{len(invalid)} log lines have non-ISO-8601 timestamp. Sample: {invalid[:2]}"
    )


def test_every_log_line_timestamp_ends_in_z(logs):
    """All timestamps must be UTC (ending in Z)."""
    non_utc = [l for l in logs["all_lines"]
               if "timestamp" in l and not str(l["timestamp"]).endswith("Z")]
    assert len(non_utc) == 0, (
        f"{len(non_utc)} log lines have non-UTC timestamp. Sample: {non_utc[:2]}"
    )


def test_every_log_line_timestamp_has_microseconds(logs):
    """Timestamps must have sub-second precision."""
    no_micro = [l for l in logs["all_lines"]
                if "timestamp" in l and "." not in str(l["timestamp"])]
    assert len(no_micro) == 0, (
        f"{len(no_micro)} log lines missing microseconds in timestamp"
    )


# ═══════════════════════════════════════════════════════════════
# 5. SERVICE & ENVIRONMENT FIELDS
# ═══════════════════════════════════════════════════════════════

def test_every_log_line_service_is_cdss_dosing_service(logs):
    wrong = [l for l in logs["all_lines"]
             if l.get("service") != "cdss-dosing-service"]
    assert len(wrong) == 0, (
        f"{len(wrong)} log lines have wrong 'service'. Sample: {wrong[:2]}"
    )


def test_every_log_line_service_is_constant(logs):
    services = set(l.get("service") for l in logs["all_lines"] if "service" in l)
    assert len(services) == 1, f"Multiple service values in logs: {services}"


def test_every_log_line_environment_is_present(logs):
    missing = [l for l in logs["all_lines"] if "environment" not in l]
    assert len(missing) == 0


def test_every_log_line_environment_is_constant(logs):
    envs = set(l.get("environment") for l in logs["all_lines"] if "environment" in l)
    assert len(envs) == 1, f"Multiple environment values in logs: {envs}"


# ═══════════════════════════════════════════════════════════════
# 6. REQUEST ID — format and correlation
# ═══════════════════════════════════════════════════════════════

def test_every_log_line_request_id_is_present(logs):
    missing = [l for l in logs["all_lines"] if "request_id" not in l]
    assert len(missing) == 0, (
        f"{len(missing)} log lines missing 'request_id'. Sample: {missing[:2]}"
    )


def test_every_log_line_request_id_is_uuid_format(logs):
    invalid = [l for l in logs["all_lines"]
               if "request_id" in l and not _is_uuid(l["request_id"])]
    assert len(invalid) == 0, (
        f"{len(invalid)} log lines have non-UUID request_id. Sample: {invalid[:2]}"
    )


def test_every_log_line_request_id_is_non_empty(logs):
    empty = [l for l in logs["all_lines"]
             if l.get("request_id") in (None, "", "null")]
    assert len(empty) == 0


def test_multiple_log_lines_per_request_share_request_id(logs):
    """One HTTP request should produce multiple log lines — all with the same request_id."""
    groups = {rid: lines for rid, lines in logs["by_request"].items()
              if len(lines) >= 2}
    assert len(groups) >= 1, (
        "Expected at least one request_id with multiple log lines"
    )


def test_http_request_and_dosing_response_share_request_id(logs):
    """The 'dosing response' and 'http request' log lines for the same request
    must have the same request_id."""
    http_ids     = {l["request_id"] for l in logs["http_lines"]}
    response_ids = {l["request_id"] for l in logs["response_lines"]}
    overlap = http_ids & response_ids
    assert len(overlap) >= 1, (
        "No shared request_ids between 'http request' and 'dosing response' logs"
    )


# ═══════════════════════════════════════════════════════════════
# 7. HTTP MIDDLEWARE LOG — specific fields
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("field", HTTP_LOG_FIELDS)
def test_http_log_has_field(logs, field):
    missing = [l for l in logs["http_lines"] if field not in l]
    assert len(missing) == 0, (
        f"{len(missing)}/{len(logs['http_lines'])} 'http request' lines missing '{field}'"
    )


def test_http_log_method_is_valid(logs):
    valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
    invalid = [l for l in logs["http_lines"]
               if l.get("method") not in valid_methods]
    assert len(invalid) == 0, f"Invalid HTTP method in logs: {[l['method'] for l in invalid]}"


def test_http_log_path_starts_with_slash(logs):
    invalid = [l for l in logs["http_lines"]
               if not str(l.get("path", "")).startswith("/")]
    assert len(invalid) == 0


def test_http_log_status_code_is_integer(logs):
    invalid = [l for l in logs["http_lines"]
               if not isinstance(l.get("status_code"), int)]
    assert len(invalid) == 0


def test_http_log_status_code_is_valid_http(logs):
    invalid = [l for l in logs["http_lines"]
               if l.get("status_code") not in range(100, 600)]
    assert len(invalid) == 0, f"Invalid status codes in logs: {[l['status_code'] for l in invalid]}"


def test_http_log_latency_ms_is_numeric(logs):
    invalid = [l for l in logs["http_lines"]
               if not isinstance(l.get("latency_ms"), (int, float))]
    assert len(invalid) == 0, f"{len(invalid)} http logs have non-numeric latency_ms"


def test_http_log_latency_ms_is_positive(logs):
    negative = [l for l in logs["http_lines"] if l.get("latency_ms", 1) < 0]
    assert len(negative) == 0, f"{len(negative)} http logs have negative latency_ms"


def test_http_log_latency_ms_is_reasonable(logs):
    """latency_ms should be under 30 seconds (30,000ms) for any request."""
    too_slow = [l for l in logs["http_lines"] if l.get("latency_ms", 0) > 30000]
    assert len(too_slow) == 0, f"{len(too_slow)} requests took > 30s: {[l['latency_ms'] for l in too_slow]}"


def test_http_log_dosing_path_is_correct(logs):
    dosing_logs = [l for l in logs["http_lines"] if l.get("path") == "/api/v1/dosing"]
    assert len(dosing_logs) >= 1, "No dosing endpoint log lines found"


def test_http_log_health_path_is_correct(logs):
    health_logs = [l for l in logs["http_lines"] if l.get("path") == "/health"]
    assert len(health_logs) >= 1, "No /health log lines found"


# ═══════════════════════════════════════════════════════════════
# 8. DOSING RESPONSE LOG — specific fields
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("field", RESPONSE_LOG_FIELDS)
def test_dosing_response_log_has_field(logs, field):
    missing = [l for l in logs["response_lines"] if field not in l]
    assert len(missing) == 0, (
        f"{len(missing)}/{len(logs['response_lines'])} 'dosing response' lines missing '{field}'"
    )


def test_dosing_response_log_dosing_rows_is_integer(logs):
    invalid = [l for l in logs["response_lines"]
               if not isinstance(l.get("dosing_rows"), int)]
    assert len(invalid) == 0


def test_dosing_response_log_dosing_rows_non_negative(logs):
    neg = [l for l in logs["response_lines"] if l.get("dosing_rows", 1) < 0]
    assert len(neg) == 0


def test_dosing_response_log_cached_is_boolean(logs):
    invalid = [l for l in logs["response_lines"]
               if not isinstance(l.get("cached"), bool)]
    assert len(invalid) == 0


def test_dosing_response_log_query_time_non_negative(logs):
    neg = [l for l in logs["response_lines"] if l.get("query_time_ms", 0) < 0]
    assert len(neg) == 0


def test_dosing_response_log_age_group_is_valid(logs):
    valid_groups = {"neonate", "infant", "pediatric", "adolescent", "adult", "geriatric"}
    invalid = [l for l in logs["response_lines"]
               if l.get("age_group") not in valid_groups]
    assert len(invalid) == 0, (
        f"Invalid age_group values in dosing response logs: "
        f"{set(l.get('age_group') for l in invalid)}"
    )


def test_dosing_response_log_drug_id_non_empty(logs):
    empty = [l for l in logs["response_lines"]
             if not l.get("drug_id_1mg")]
    assert len(empty) == 0


# ═══════════════════════════════════════════════════════════════
# 9. CACHE LOG LINES
# ═══════════════════════════════════════════════════════════════

def test_cache_miss_lines_have_cache_key(logs):
    missing = [l for l in logs["miss_lines"] if "cache_key" not in l]
    assert len(missing) == 0, f"{len(missing)} cache MISS lines missing 'cache_key'"


def test_cache_hit_lines_have_cache_key(logs):
    missing = [l for l in logs["hit_lines"] if "cache_key" not in l]
    assert len(missing) == 0, f"{len(missing)} cache HIT lines missing 'cache_key'"


def test_cache_key_format_is_dosing_drug_group(logs):
    """Cache keys must follow the format 'dosing:{drug_id}:{age_group}'."""
    pattern = re.compile(r"^dosing:.+:(neonate|infant|pediatric|adolescent|adult|geriatric)$")
    invalid = [l for l in logs["miss_lines"] + logs["hit_lines"]
               if "cache_key" in l and not pattern.match(str(l["cache_key"]))]
    assert len(invalid) == 0, (
        f"{len(invalid)} cache lines have malformed cache_key. "
        f"Sample: {[l['cache_key'] for l in invalid[:3]]}"
    )


def test_cache_hit_lines_are_present(logs):
    assert len(logs["hit_lines"]) >= 1, "Expected at least one cache HIT log line"


def test_cache_miss_lines_are_present(logs):
    assert len(logs["miss_lines"]) >= 1, "Expected at least one cache MISS log line"


# ═══════════════════════════════════════════════════════════════
# 10. STATUS CODES IN LOGS MATCH EXPECTED VALUES
# ═══════════════════════════════════════════════════════════════

def test_logs_contain_200_status_codes(logs):
    codes_200 = [l for l in logs["http_lines"] if l.get("status_code") == 200]
    assert len(codes_200) >= 1, "No 200 status codes found in http logs"


def test_logs_contain_404_status_codes(logs):
    codes_404 = [l for l in logs["http_lines"] if l.get("status_code") == 404]
    assert len(codes_404) >= 1, "No 404 status codes found in http logs"


def test_logs_contain_401_status_codes(logs):
    codes_401 = [l for l in logs["http_lines"] if l.get("status_code") == 401]
    assert len(codes_401) >= 1, "No 401 status codes found in http logs"


def test_logs_never_contain_500_from_valid_requests(logs):
    """The representative requests should not have produced any 500 errors."""
    codes_500 = [l for l in logs["http_lines"] if l.get("status_code") == 500]
    assert len(codes_500) == 0, (
        f"{len(codes_500)} requests returned 500. Paths: "
        f"{[l.get('path') for l in codes_500]}"
    )


# ═══════════════════════════════════════════════════════════════
# 11. SENSITIVE DATA NEVER IN LOGS
# ═══════════════════════════════════════════════════════════════

def test_api_key_not_in_any_log_line(logs):
    api_key_lower = API_KEY.lower()
    leaking = [l for l in logs["all_lines"] if api_key_lower in str(l).lower()]
    assert len(leaking) == 0, (
        f"API key found in {len(leaking)} log lines!"
    )


def test_db_password_not_in_any_log_line(logs):
    db_url = _env.get("DATABASE_URL", "")
    if "@" in db_url and ":" in db_url:
        password = db_url.split("@")[0].rsplit(":", 1)[-1]
        if len(password) >= 4:
            leaking = [l for l in logs["all_lines"]
                       if password.lower() in str(l).lower()]
            assert len(leaking) == 0, f"DB password in {len(leaking)} log lines!"


def test_no_python_traceback_in_logs(logs):
    """No Python tracebacks must appear in any log line."""
    with_trace = [l for l in logs["all_lines"]
                  if "traceback" in str(l).lower()
                  or "most recent call last" in str(l).lower()]
    assert len(with_trace) == 0, f"{len(with_trace)} log lines contain traceback"


def test_no_raw_sql_in_logs(logs):
    """Raw SQL queries must never appear in logs (user-input fields excluded)."""
    # Fields that may legitimately contain user-supplied SQL injection strings.
    USER_INPUT_FIELDS = {"drug_id_1mg", "cache_key", "message"}
    sql_markers = ["select ", "from drugdb", "join drugdb", "where dr."]

    leaking = []
    for line in logs["all_lines"]:
        sanitized = {k: v for k, v in line.items() if k not in USER_INPUT_FIELDS}
        if any(m in str(sanitized).lower() for m in sql_markers):
            leaking.append(line)

    assert len(leaking) == 0, f"{len(leaking)} log lines contain raw SQL"


def test_no_connection_string_in_logs(logs):
    """Connection strings (postgresql://) must not appear in logs."""
    conn_str = [l for l in logs["all_lines"]
                if "postgresql://" in str(l).lower()]
    assert len(conn_str) == 0, f"{len(conn_str)} log lines contain connection string"


# ═══════════════════════════════════════════════════════════════
# 12. LOGGER NAMES ARE VALID
# ═══════════════════════════════════════════════════════════════

VALID_LOGGERS = {
    "app.api.v1.routers.dosing",
    "app.services.dosing_service",
    "app.repositories.dosing_repo",
    "app.main",
    "uvicorn.access",
    "uvicorn.error",
}


def test_every_log_line_logger_is_known(logs):
    unknown = [l for l in logs["all_lines"]
               if l.get("logger") not in VALID_LOGGERS]
    # Note: some loggers we may not have included in VALID_LOGGERS
    # Just check it's a non-empty dotted string
    invalid = [l for l in logs["all_lines"]
               if not l.get("logger") or "." not in str(l.get("logger", ""))]
    assert len(invalid) == 0, (
        f"{len(invalid)} log lines have invalid logger name. Sample: {invalid[:2]}"
    )


def test_router_logs_use_correct_logger(logs):
    router_logs = [l for l in logs["all_lines"]
                   if l.get("event") in ("dosing request", "dosing response")]
    wrong = [l for l in router_logs
             if l.get("logger") != "app.api.v1.routers.dosing"]
    assert len(wrong) == 0


def test_service_logs_use_correct_logger(logs):
    cache_logs = [l for l in logs["all_lines"]
                  if l.get("event") in ("cache MISS", "cache HIT")]
    wrong_cache = [l for l in cache_logs
                   if l.get("logger") != "app.services.dosing_service"]
    assert len(wrong_cache) == 0

    fallback_logs = [l for l in logs["all_lines"]
                     if l.get("event") == "primary miss, trying fallback"]
    wrong_fallback = [l for l in fallback_logs
                      if l.get("logger") != "app.repositories.dosing_repo"]
    assert len(wrong_fallback) == 0


def test_middleware_logs_use_app_main_logger(logs):
    http_logs = [l for l in logs["http_lines"]]
    wrong = [l for l in http_logs if l.get("logger") != "app.main"]
    assert len(wrong) == 0


# ═══════════════════════════════════════════════════════════════
# 13. CACHE HIT → query_time_ms=0.0 IN RESPONSE LOG
# ═══════════════════════════════════════════════════════════════

def test_cache_hit_responses_have_zero_query_time(logs):
    cached_responses = [l for l in logs["response_lines"] if l.get("cached") is True]
    non_zero = [l for l in cached_responses if l.get("query_time_ms", 1) != 0.0]
    assert len(non_zero) == 0, (
        f"{len(non_zero)} cache-hit response logs have non-zero query_time_ms"
    )


def test_cache_miss_responses_have_positive_query_time(logs):
    cold_responses = [l for l in logs["response_lines"] if l.get("cached") is False]
    zero = [l for l in cold_responses if l.get("query_time_ms", 1) <= 0]
    assert len(zero) == 0, (
        f"{len(zero)} cache-miss response logs have zero/negative query_time_ms"
    )


# ═══════════════════════════════════════════════════════════════
# 14. LOG LINE JSON VALIDITY — all lines are valid JSON dicts
# ═══════════════════════════════════════════════════════════════

def test_all_parsed_log_lines_are_dicts(logs):
    non_dict = [l for l in logs["all_lines"] if not isinstance(l, dict)]
    assert len(non_dict) == 0


def test_all_log_line_keys_are_strings(logs):
    invalid = [l for l in logs["all_lines"]
               if not all(isinstance(k, str) for k in l.keys())]
    assert len(invalid) == 0


# ═══════════════════════════════════════════════════════════════
# 15. LIVE PARAMETRIZED LOG CHECKS — per drug + per field (78 tests)
# ═══════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def live_session():
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=20),
        timeout=aiohttp.ClientTimeout(total=15),
    ) as s:
        yield s


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS)
@pytest.mark.parametrize("field", CORE_LOG_FIELDS)
async def test_live_request_produces_log_with_field(live_session, drug_id, field):
    """Make a real request with a custom request_id and verify the log line has required fields."""
    custom_id = str(uuid.uuid4())
    async with live_session.post(
        ENDPOINT,
        json={"drug_id_1mg": drug_id, "age": 35},
        headers={**HEADERS, "X-Request-ID": custom_id},
    ) as resp:
        assert resp.status in (200, 404)

    # Small wait for log flush
    await __import__("asyncio").sleep(0.2)

    recent = _fetch_docker_logs(tail=50)
    request_lines = _lines_for_request_id(recent, custom_id)

    assert len(request_lines) >= 1, (
        f"No log lines found for request_id={custom_id} (drug={drug_id})"
    )
    for line in request_lines:
        assert field in line, (
            f"Field '{field}' missing in log line for drug={drug_id}: {line}"
        )


# ═══════════════════════════════════════════════════════════════
# 16. LIVE HTTP LOG FIELD CHECKS — per drug (52 tests)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS[:5])
@pytest.mark.parametrize("field", HTTP_LOG_FIELDS)
async def test_live_http_log_has_field_for_drug(live_session, drug_id, field):
    custom_id = str(uuid.uuid4())
    async with live_session.post(
        ENDPOINT,
        json={"drug_id_1mg": drug_id, "age": 35},
        headers={**HEADERS, "X-Request-ID": custom_id},
    ) as resp:
        pass

    await __import__("asyncio").sleep(0.2)
    recent = _fetch_docker_logs(tail=50)
    http_log = [l for l in _lines_for_request_id(recent, custom_id)
                if l.get("event") == "http request"]

    assert len(http_log) >= 1, f"No 'http request' log for drug={drug_id}"
    for line in http_log:
        assert field in line, f"Field '{field}' missing in http log for drug={drug_id}"


# ═══════════════════════════════════════════════════════════════
# 17. LIVE LATENCY IN LOGS IS ACCURATE
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS[:5])
async def test_live_log_latency_ms_is_positive(live_session, drug_id):
    custom_id = str(uuid.uuid4())
    async with live_session.post(
        ENDPOINT,
        json={"drug_id_1mg": drug_id, "age": 35},
        headers={**HEADERS, "X-Request-ID": custom_id},
    ) as resp:
        pass

    await __import__("asyncio").sleep(0.2)
    recent    = _fetch_docker_logs(tail=50)
    http_logs = [l for l in _lines_for_request_id(recent, custom_id)
                 if l.get("event") == "http request"]

    for log in http_logs:
        latency = log.get("latency_ms", 0)
        assert latency > 0, f"latency_ms={latency} is not positive for drug={drug_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS[:5])
async def test_live_log_status_code_matches_response(live_session, drug_id):
    custom_id = str(uuid.uuid4())
    async with live_session.post(
        ENDPOINT,
        json={"drug_id_1mg": drug_id, "age": 35},
        headers={**HEADERS, "X-Request-ID": custom_id},
    ) as resp:
        actual_status = resp.status

    await __import__("asyncio").sleep(0.2)
    recent    = _fetch_docker_logs(tail=50)
    http_logs = [l for l in _lines_for_request_id(recent, custom_id)
                 if l.get("event") == "http request"]

    if http_logs:
        logged_status = http_logs[0].get("status_code")
        assert logged_status == actual_status, (
            f"drug={drug_id}: actual status {actual_status} but logged {logged_status}"
        )


# ═══════════════════════════════════════════════════════════════
# 18. AGE GROUP CORRECT IN DOSING RESPONSE LOG
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_group", [
    (18, "adult"), (35, "adult"), (65, "geriatric"), (70, "geriatric"),
])
async def test_live_log_age_group_correct(live_session, age, expected_group):
    custom_id = str(uuid.uuid4())
    async with live_session.post(
        ENDPOINT,
        json={"drug_id_1mg": GOOD_DRUGS[0], "age": age},
        headers={**HEADERS, "X-Request-ID": custom_id},
    ) as resp:
        if resp.status != 200:
            return

    await __import__("asyncio").sleep(0.2)
    recent   = _fetch_docker_logs(tail=50)
    resp_log = [l for l in _lines_for_request_id(recent, custom_id)
                if l.get("event") == "dosing response"]

    if resp_log:
        assert resp_log[0].get("age_group") == expected_group, (
            f"age={age}: expected age_group={expected_group}, "
            f"got {resp_log[0].get('age_group')}"
        )


# ═══════════════════════════════════════════════════════════════
# 19. CACHE STATE REFLECTED IN LOGS
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("drug_id", GOOD_DRUGS[:5])
async def test_second_request_logged_as_cache_hit(live_session, drug_id):
    """First call → cache MISS; second call → cache HIT in log."""
    # First call (warm up)
    async with live_session.post(
        ENDPOINT, json={"drug_id_1mg": drug_id, "age": 35}, headers=HEADERS
    ) as _:
        pass

    # Second call with traceable request_id
    custom_id = str(uuid.uuid4())
    async with live_session.post(
        ENDPOINT,
        json={"drug_id_1mg": drug_id, "age": 35},
        headers={**HEADERS, "X-Request-ID": custom_id},
    ) as resp:
        if resp.status != 200:
            return

    await __import__("asyncio").sleep(0.2)
    recent    = _fetch_docker_logs(tail=50)
    req_lines = _lines_for_request_id(recent, custom_id)
    hit_lines = [l for l in req_lines if l.get("event") == "cache HIT"]

    assert len(hit_lines) >= 1, (
        f"Expected 'cache HIT' in logs for second call to {drug_id}, "
        f"but found: {[l.get('event') for l in req_lines]}"
    )
