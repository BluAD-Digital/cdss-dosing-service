# CDSS Dosing Service

A production-grade FastAPI microservice that provides evidence-based drug dosing recommendations for Indian medical practitioners. Given a 1mg catalog drug ID and patient age, the service queries a curated PostgreSQL drug database (sourced from DailyMed, OpenFDA, DrugBank, and RxNorm), maps the patient's age to the correct dosing cohort, and returns structured dosing regimen data with Redis caching for sub-millisecond repeat lookups.

---

## Architecture

```text
HTTP Request
     │
     ▼
[Nginx]  ← rate-limit 100 req/min/IP, gzip, reverse proxy
     │
     ▼
[FastAPI app]
  ├── API-Key Middleware  (401 if missing/wrong)
  ├── Request Logger      (structlog JSON)
  │
  ▼
[Router]  POST /api/v1/dosing
     │
     ▼
[Service]  dosing_service.get_dosing()
  ├── age_mapper   → age_groups, primary_group
  ├── Redis cache  → HIT: return immediately
  │                  MISS: continue
  ▼
[Repository]  dosing_repo.fetch_dosing_with_fallback()
  ├── Primary query (dosing.sql)      → RxCUI direct match
  └── Fallback query (dosing_fallback.sql) → UNII-based ingredient resolution
     │
     ▼
[PostgreSQL 16]  drugdb schema
  ├── indian_brand       (1mg catalog → RxCUI mapping)
  ├── drug               (formulation registry)
  ├── dosing_regimen     (age/route/dose rows)
  └── drug_ingredient_mapping + ingredients
```

---

## Endpoints

### POST /api/v1/dosing

Returns dosing recommendations for a drug and patient age.

#### Request

```http
POST /api/v1/dosing
Content-Type: application/json
X-API-Key: your-secret-api-key
```

```json
{
  "drug_id_1mg": "457491",
  "age": 35
}
```

#### Response (200)

```json
{
  "drug_id_1mg": "457491",
  "formulation_id": "abc-123",
  "brand_name": "Crocin",
  "salt_composition": "Paracetamol 500 MG",
  "generic_name": "Paracetamol",
  "age_group": "adult",
  "source": "primary",
  "is_partial_match": false,
  "dosing": [
    {
      "frequency": "q4-6h",
      "frequency_meaning": "every 4-6 hours",
      "route": "oral",
      "dose_amount": "325-650",
      "dose_unit": "mg",
      "duration": null,
      "indication": "pain",
      "instructions": "Do not exceed 4g/day",
      "food_timing": null
    },
    {
      "frequency": "q6h",
      "frequency_meaning": "every 6 hours",
      "route": "oral",
      "dose_amount": "500",
      "dose_unit": "mg",
      "duration": null,
      "indication": "fever",
      "instructions": null,
      "food_timing": "after food"
    }
  ],
  "cached": false,
  "query_time_ms": 18.4
}
```

#### Response fields

| Field | Type | Description |
|-------|------|-------------|
| `drug_id_1mg` | string | Echo of the requested drug ID |
| `formulation_id` | string | Internal formulation identifier from the drug registry |
| `brand_name` | string | Indian brand name from the 1mg catalog |
| `salt_composition` | string | Salt/ingredient composition string |
| `generic_name` | string | Ingredient names joined by ` / ` |
| `age_group` | string | Primary age cohort resolved from the input age |
| `source` | string | `"primary"` (RxCUI match) or `"fallback"` (UNII-based match) |
| `is_partial_match` | bool | `true` when only a subset of the drug's ingredients resolved via UNII in the fallback path |
| `dosing` | array | One object per unique dosing row (see below) |
| `cached` | bool | `true` when the response was served from Redis |
| `query_time_ms` | float | DB query time in milliseconds (`0.0` when cached) |

#### DosingRow fields

| Field | Type | Description |
|-------|------|-------------|
| `frequency` | string\|null | Raw frequency code, e.g. `"q8h"` |
| `frequency_meaning` | string\|null | Human-readable expansion, e.g. `"every 8 hours"` |
| `route` | string\|null | Administration route, e.g. `"oral"`, `"iv"` |
| `dose_amount` | string\|null | Dose quantity as a string, e.g. `"500"` or `"325-650"` |
| `dose_unit` | string\|null | Unit, e.g. `"mg"`, `"mcg"` |
| `duration` | string\|null | Course length if specified |
| `indication` | string\|null | Clinical indication, lowercased |
| `instructions` | string\|null | Administration notes |
| `food_timing` | string\|null | Food relationship, e.g. `"after food"`, `"before food"`, `null` if unspecified |

#### Error responses

| Status | body.error | Cause |
|--------|------------|-------|
| 401 | `unauthorized` | Missing or wrong X-API-Key |
| 404 | `not_found` | Drug not in DB or no dosing data for this age |
| 422 | `validation_error` | age < 0 or > 120, missing field |
| 500 | `internal_error` | DB error (details not exposed) |

---

### GET /health

No auth required. Returns 200 when both DB and Redis are reachable, 503 otherwise.

```json
{"status": "ok", "db": "connected", "cache": "connected"}
```

---

## Age group mapping

| Patient age | age_groups passed to query | primary_group (cache key) |
|-------------|---------------------------|--------------------------|
| < 1 | `["neonate"]` | `neonate` |
| 1 – 1 | `["infant", "neonate"]` | `infant` |
| 2 – 11 | `["pediatric", "any"]` | `pediatric` |
| 12 – 17 | `["adolescent", "adult", "any"]` | `adolescent` |
| 18 – 64 | `["adult", "any"]` | `adult` |
| ≥ 65 | `["geriatric", "adult", "any"]` | `geriatric` |

---

## Query strategy — primary and fallback

Every dosing request tries two SQL queries in sequence on the same DB connection.

### Primary (`queries/dosing.sql`)

Resolves the drug's RxCUI(s) from `indian_brand` (excluding `drugbank` and `us_unapproved` match types), finds the best-evidenced formulation in `drug`, and fetches dosing rows filtered for the patient's age group. Returns `source = "primary"`, `is_partial_match = false`.

### Fallback (`queries/dosing_fallback.sql`)

Used when the primary returns 0 rows. Resolves each RxCUI through its ingredient UNII identifier into `DrugMasterLinkage`, then finds a formulation by `master_linkage_id` instead of `rxcui`. Handles active and inactive ingredients separately:

- **Active / untyped ingredients** — must each resolve through a single-RxCUI `DrugMasterLinkage` entry.
- **Inactive ingredients** — automatically pass; any `DrugMasterLinkage` row containing the UNII is used.

If at least one ingredient resolves, the query proceeds and sets `is_partial_match = true` when not all ingredients resolved. If no ingredients resolve, returns 0 rows and the service raises 404.

Returns `source = "fallback"`.

### Formulation ranking (both queries)

Within each RxCUI, the best formulation is chosen by data source quality:

1. DailyMed
2. OpenFDA
3. DrugBank
4. RxNorm

Ties broken by most dosing rows, then `formulation_id` ascending.

### Dosing row deduplication

`ROW_NUMBER()` partitioned by `(frequency, route, dose_value, dose_unit, indication)` keeps one row per unique dose. Within each partition, the row with both `indication` and `instructions` wins.

Hard filters applied in both queries:

| Filter | Value |
|--------|-------|
| `renal_function` | `any` |
| `hepatic_function` | `any` |
| `pregnancy_status` | `any` |
| `frequency` | `IS NOT NULL` |
| `dose_amount` | not `CONTRAINDICATED` |
| Pediatric guard | `administration_notes NOT ILIKE '%pediatric%'` unless the patient is pediatric/infant/neonate |

---

## Setup

### 1. Configure environment

```bash
cd cdss-dosing-service
cp .env.example .env
```

Edit `.env` — fill in your PostgreSQL connection string and choose an API key:

```env
DATABASE_URL=postgresql://cdss_app:your_password@178.236.185.230:5432/drugdb
REDIS_URL=redis://redis:6379
API_KEY=generate-a-strong-random-key
```

> **Note:** PostgreSQL runs on the GCP VM outside Docker. Use `host.docker.internal` or the VM's internal IP.

### 2. Build and start

```bash
docker-compose up --build -d
```

Services started:
- `dosing-service` — FastAPI on port 8001 (internal)
- `redis` — Redis 7 on port 6379 (internal, 256 MB LRU)
- `nginx` — reverse proxy on port 80

### 3. Verify

```bash
curl http://localhost/health
# {"status":"ok","db":"connected","cache":"connected"}

curl -X POST http://localhost/api/v1/dosing \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{"drug_id_1mg": "457491", "age": 35}'
```

---

## Running tests

```bash
# Install deps (or use a venv)
pip install -r requirements.txt

# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_age_mapper.py -v
pytest tests/test_dosing_service.py -v
pytest tests/test_dosing_router.py -v

# Run with coverage
pytest tests/ --cov=app --cov-report=term-missing
```

---

## Adding a new clinical query endpoint

The service is structured so each new endpoint is self-contained. Follow this pattern:

**1. Add the SQL query**

```text
queries/interactions.sql   ← new SQL file
```

**2. Add the repository**

```python
# app/repositories/interactions_repo.py
_SQL = (Path(__file__).parent.parent.parent / "queries" / "interactions.sql").read_text()

async def fetch_interactions(pool, drug_id_1mg, drug_id_2):
    ...
```

**3. Add the service**

```python
# app/services/interactions_service.py
async def get_interactions(drug_id_1mg, drug_id_2, pool, redis):
    ...
```

**4. Add the schemas** (if different from existing ones)

```python
# app/schemas/request.py  — add InteractionRequest
# app/schemas/response.py — add InteractionResponse
```

**5. Add the router**

```python
# app/api/v1/routers/interactions.py
router = APIRouter(tags=["interactions"])

@router.post("/interactions", response_model=InteractionResponse)
async def get_interactions(...):
    ...
```

**6. Register in main.py** — one line:

```python
from app.api.v1.routers.interactions import router as interactions_router
app.include_router(interactions_router, prefix="/api/v1")
```

No restructuring required. Each endpoint is an independent vertical slice.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | required | asyncpg DSN for PostgreSQL 16 |
| `REDIS_URL` | required | Redis connection URL |
| `API_KEY` | required | Secret key for `X-API-Key` header auth |
| `POOL_MIN_SIZE` | `5` | Minimum asyncpg pool connections |
| `POOL_MAX_SIZE` | `20` | Maximum asyncpg pool connections |
| `POOL_COMMAND_TIMEOUT` | `10` | Per-query timeout in seconds |
| `CACHE_TTL_SECONDS` | `86400` | Redis TTL for cached dosing responses (24 hrs) |
| `WORKERS` | `4` | Gunicorn worker count |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `ENVIRONMENT` | `production` | `production` → JSON logs; other → colored logs |

---

## Frequency code reference

The `frequency_meaning` field in every dosing row is resolved from 134 known frequency codes (96 unique meanings) found in the database. Codes are matched case-insensitively, so `QD`, `qd`, and `q24h` all resolve to the same meaning.

### Once / twice / N-times daily

| Code(s) | Meaning |
|---------|---------|
| `OD`, `QD`, `qd`, `q24h`, `Q24h`, `daily`, `once daily`, `every 24 hours` | once daily |
| `BD`, `BID`, `twice daily` | twice daily |
| `TD`, `TID`, `three times daily` | three times daily |
| `QID`, `4 times daily` | four times daily |
| `4-6 times daily` | four to six times daily |
| `5 times per day` | 5 times daily |
| `BID or TID` | two to three times daily |
| `QD or BID` | once to twice daily |

### Every N hours

| Code(s) | Meaning |
|---------|---------|
| `q1h`, `hourly`, `q60min` | every hour |
| `q2h` | every 2 hours |
| `q2-3h` | every 2-3 hours |
| `q2-4h` | every 2-4 hours |
| `q2-6h` | every 2-6 hours |
| `q3h` | every 3 hours |
| `q3-4h` | every 3-4 hours |
| `q3-6h` | every 3-6 hours |
| `q4h` | every 4 hours |
| `q4-5h` | every 4-5 hours |
| `q4-6h` | every 4-6 hours |
| `q6h` | every 6 hours |
| `q6-8h` | every 6-8 hours |
| `q8h` | every 8 hours |
| `q8-10h` | every 8-10 hours |
| `q8-12h`, `q8h to q12h` | every 8-12 hours |
| `q8h or q12h`, `q12h or q8h` | every 8 or 12 hours |
| `q12h`, `Q12h`, `every 12 hours` | every 12 hours |
| `q12h or q6-8h` | every 6-12 hours |
| `q18h` | every 18 hours |
| `q18-24h` | every 18-24 hours |
| `every 24 hours` | every 24 hours |
| `q24-36h`, `q24h to q36h` | every 24-36 hours |
| `q24-48h`, `q24h to q48h` | every 24-48 hours |
| `q36h` | every 36 hours |
| `q36-48h` | every 36-48 hours |
| `q40-60h` | every 40-60 hours |
| `q48h` | every 48 hours |
| `q60h` | every 60 hours |
| `q72h` | every 72 hours |
| `q72-96h` | every 72-96 hours |
| `q96h` | every 96 hours |
| `q1-3h` | every 1-3 hours |
| `q1-4h` | every 1-4 hours |

### Every N minutes

| Code(s) | Meaning |
|---------|---------|
| `q1-2min` | every 1-2 minutes |
| `q2min` | every 2 minutes |
| `q2-3min` | every 2-3 minutes |
| `q2-5min` | every 2-5 minutes |
| `q3-5min` | every 3-5 minutes |
| `q5min` | every 5 minutes |
| `q5-10min` | every 5-10 minutes |
| `q5-15min` | every 5-15 minutes |
| `q10min` | every 10 minutes |
| `q10-15min` | every 10-15 minutes |
| `q12-15min` | every 12-15 minutes |
| `q15min` | every 15 minutes |
| `q15-25min` | every 15-25 minutes |
| `q20min` | every 20 minutes |
| `q20-30min` | every 20-30 minutes |
| `q30min`, `every 30 minutes` | every 30 minutes |
| `q90min` | every 90 minutes |

### Every N days

| Code(s) | Meaning |
|---------|---------|
| `QOD`, `every other day` | every other day |
| `q3-5d` | every 3-5 days |
| `q3-7d` | every 3-7 days |
| `q4-7d` | every 4-7 days |
| `q7d`, `q7days` | every 7 days |
| `q21d` | every 21 days |

### Weekly / biweekly

| Code(s) | Meaning |
|---------|---------|
| `q1w`, `weekly`, `once weekly`, `every 7 days` | once weekly |
| `BIWEEKLY`, `biweekly`, `twice weekly` | twice weekly |
| `TIW`, `three times weekly` | three times weekly |
| `three times per week` | three times per week |
| `3 times weekly` | 3 times weekly |
| `2-3 times per week` | two to three times weekly |
| `twice weekly or weekly` | once to twice weekly |
| `q2w`, `Q2W`, `every 2 weeks` | every 2 weeks |
| `q1-2w` | every 1-2 weeks |
| `q2-4w` | every 2-4 weeks |
| `q3w`, `every 3 weeks` | every 3 weeks |
| `q3-4w` | every 3-4 weeks |
| `q4w`, `Q4W` | every 4 weeks |
| `q6w` | every 6 weeks |
| `q6-8w` | every 6-8 weeks |
| `q8w` | every 8 weeks |
| `q12w` | every 12 weeks |
| `q16w` | every 16 weeks |

### Every N months / yearly

| Code(s) | Meaning |
|---------|---------|
| `monthly`, `once monthly` | once monthly |
| `q3m` | every 3 months |
| `q6m`, `q6mo` | every 6 months |
| `once a year` | once yearly |

### Special / PRN

| Code(s) | Meaning |
|---------|---------|
| `once`, `single`, `single dose` | single dose |
| `as_needed`, `as needed`, `prn`, `on demand` | as needed |
| `continuous` | continuous infusion |
| `loading` | loading dose |
| `q4-6h as needed` | every 4-6 hours as needed |
| `q1-2min prn` | every 1-2 minutes as needed |
| `q2min prn` | every 2 minutes as needed |
| `q3-5min as_needed` | every 3-5 minutes as needed |

---

## File structure

```text
cdss-dosing-service/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── README.md
│
├── app/
│   ├── main.py                  FastAPI app: lifespan, middleware, /health, router registration
│   ├── config.py                Pydantic Settings — all env vars with defaults
│   │
│   ├── api/
│   │   ├── deps.py              get_db / get_cache FastAPI dependencies
│   │   └── v1/routers/
│   │       └── dosing.py        POST /api/v1/dosing router
│   │
│   ├── schemas/
│   │   ├── request.py           DosingRequest (drug_id_1mg, age)
│   │   └── response.py          DosingResponse, DosingRow, ErrorResponse
│   │
│   ├── services/
│   │   └── dosing_service.py    Orchestration: cache check → repo → cache write
│   │
│   ├── repositories/
│   │   └── dosing_repo.py       fetch_dosing_with_fallback() — runs primary then fallback SQL
│   │
│   ├── cache/
│   │   └── redis.py             get_cached / set_cached helpers
│   │
│   ├── db/
│   │   └── postgres.py          asyncpg pool create/close
│   │
│   └── utils/
│       ├── age_mapper.py        age_to_groups(), age_to_primary_group()
│       ├── frequency_mapper.py  resolve_frequency() — 134 code → meaning mappings
│       └── logger.py            structlog JSON/colored logger, request-id context
│
├── queries/
│   ├── dosing.sql               Primary dosing query (RxCUI direct match)
│   └── dosing_fallback.sql      Fallback query (UNII → DrugMasterLinkage)
│
├── nginx/                       Nginx config (rate-limit, gzip, reverse proxy)
│
└── tests/
    ├── conftest.py
    ├── test_age_mapper.py
    ├── test_dosing_service.py
    ├── test_dosing_router.py
    ├── test_age_group_coverage.py
    ├── test_concurrency.py
    ├── test_top500_drugs.py
    ├── smoke_test.py
    └── phase*/                  Phase-gated test suites (broken, reliability, security, observability)
```
