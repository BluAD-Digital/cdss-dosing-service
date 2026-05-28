# CDSS Dosing Service — Query Reference

All SQL queries used by the API, organized per endpoint with a step-by-step flow explanation.

---

## Table of Contents

1. [POST /api/v1/dosing](#post-apiv1dosing)
   - [Request & Response](#request--response)
   - [End-to-End Flow](#end-to-end-flow)
   - [Query 0 — Drug Exists Check](#query-0--drug-exists-check)
   - [Query 1 — Primary Dosing Query](#query-1--primary-dosing-query)
   - [Query 2 — Fallback Dosing Query](#query-2--fallback-dosing-query)
2. [GET /health](#get-health)
3. [Database Tables Reference](#database-tables-reference)

---

## POST /api/v1/dosing

### Request & Response

**Request** (`POST`, requires `X-API-Key` header):
```json
{
  "drug_id_1mg": "457491",
  "age": 35
}
```

**Success Response (200)**:
```json
{
  "drug_id_1mg": "457491",
  "formulation_id": "...",
  "brand_name": "Calpol 500mg",
  "salt_composition": "Paracetamol 500mg",
  "generic_name": "Paracetamol",
  "age_group": "adult",
  "dosing": [
    {
      "frequency": "q4-6h",
      "frequency_meaning": "every 4-6 hours",
      "route": "oral",
      "dose_amount": "500-1000",
      "dose_unit": "mg",
      "duration": null,
      "indication": "pain relief",
      "instructions": null
    }
  ],
  "cached": false,
  "query_time_ms": 12.4
}
```

**Error Responses**:

| Status | Trigger |
|--------|---------|
| 401 | Missing or invalid `X-API-Key` header |
| 404 | Drug not in DB or no dosing data found for given age |
| 422 | Validation error (age out of range, missing field) |
| 500 | Unhandled database error |

---

### End-to-End Flow

```
CLIENT REQUEST
│
│  POST /api/v1/dosing
│  { drug_id_1mg, age }
│
▼
┌─────────────────────────────────────────────────┐
│  MIDDLEWARE                                     │
│  1. API Key check (X-API-Key header)            │
│     └─ fail → 401                               │
│  2. Request logging + timing starts             │
└──────────────────┬──────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────┐
│  AGE → AGE GROUPS MAPPING                      │
│                                                 │
│  age < 1          → ["neonate"]                 │
│  1  <= age <  2   → ["infant", "neonate"]       │
│  2  <= age < 18   → ["pediatric", "any"]        │
│  18 <= age < 65   → ["adult", "any"]            │
│  age >= 65        → ["geriatric", "adult", "any"]│
│                                                 │
│  primary_group → single value, used as          │
│  part of the Redis cache key                    │
└──────────────────┬──────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────┐
│  REDIS CACHE CHECK                              │
│  key = "dosing:{drug_id_1mg}:{primary_group}"   │
│                                                 │
│  HIT ──────────────────────────────────────────►│ Return response
│       cached=true, query_time_ms=0.0            │ immediately
│                                                 │
│  MISS → continue                                │
└──────────────────┬──────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────┐
│  QUERY 0 — drug_exists()                        │
│  SELECT 1 FROM drugdb.indian_brand              │
│  WHERE drug_id_1mg = $1                         │
│    AND match_combination NOT IN                 │
│        ('drugbank', 'us_unapproved')            │
│                                                 │
│  FALSE → raise 404 immediately                  │
│  TRUE  → continue                               │
└──────────────────┬──────────────────────────────┘
                   │  timer starts here
                   ▼
┌─────────────────────────────────────────────────┐
│  QUERY 1 — Primary Dosing (dosing.sql)          │
│  fetch_dosing(pool, drug_id_1mg, age_groups)    │
│                                                 │
│  Match: RxCUI direct match from indian_brand    │
│  (excludes drugbank / us_unapproved entries)    │
│                                                 │
│  rows returned > 0 ──────────────────────────► │ Skip fallback
│  rows = 0 → try fallback                        │
└──────────────────┬──────────────────────────────┘
                   │ (only if primary returned 0 rows)
                   ▼
┌─────────────────────────────────────────────────┐
│  QUERY 2 — Fallback Dosing (dosing_fallback.sql)│
│  fetch_dosing_fallback(pool, drug_id_1mg,       │
│                        age_groups)              │
│                                                 │
│  Match: UNII-based ingredient resolution        │
│  (more liberal, does NOT exclude drugbank       │
│   match_combination entries)                    │
│                                                 │
│  rows returned > 0 ──────────────────────────► │ Use these rows
│  rows = 0 → raise 404                           │
└──────────────────┬──────────────────────────────┘
                   │  timer stops here
                   ▼
┌─────────────────────────────────────────────────┐
│  RESPONSE BUILDING                              │
│  • Extract brand_name, salt_composition,        │
│    generic_name, formulation_id from row[0]     │
│  • For every row, build DosingRow:              │
│      frequency_meaning = resolve_frequency()   │
│  • Wrap into DosingResponse                     │
└──────────────────┬──────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────┐
│  STORE IN REDIS CACHE                           │
│  set_cached(redis, cache_key, response,         │
│             CACHE_TTL_SECONDS)  [default 24h]   │
└──────────────────┬──────────────────────────────┘
                   │
                   ▼
             Return 200 response
             cached=false, query_time_ms=<elapsed>
```

---

### Query 0 — Drug Exists Check

**When**: Before running any heavy query. Validates the drug_id_1mg is in the approved Indian brand catalog.

**Why this filter**: `match_combination NOT IN ('drugbank', 'us_unapproved')` — only drugs that were matched through trusted Indian sources are served; DrugBank and US-unapproved matches are excluded from the primary path.

```sql
SELECT 1
FROM drugdb.indian_brand
WHERE drug_id_1mg = $1
  AND match_combination NOT IN ('drugbank', 'us_unapproved')
LIMIT 1
```

**Parameters**:
- `$1` — `drug_id_1mg` (string)

**Returns**: Single row if drug exists and is valid, nothing if not.

**Effect**: If no row is returned → service raises HTTP 404 immediately (skips all DB-heavy queries).

---

### Query 1 — Primary Dosing Query

**File**: [queries/dosing.sql](../queries/dosing.sql)

**When**: Cache miss + drug_exists is true.

**Strategy**: Direct RxCUI match — find the drug's RxCUI(s) from `indian_brand`, then find the best formulation in `drug` that shares those RxCUIs, then fetch its dosing rows filtered for the patient's age group and standard conditions.

#### Step-by-Step CTE Flow

```
indian_brand (drug_id_1mg = $1, match_combination valid)
       │
       │ salt_composition, rxcui[]
       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: salt_ingredients                              │
│  Pulls the brand's salt_composition string and      │
│  rxcui array for the given drug_id_1mg.             │
│  Filters out 'drugbank' and 'us_unapproved' rows.   │
└──────────────────────┬──────────────────────────────┘
                       │ rxcui[]
                       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: candidate_formulations                        │
│  Finds all formulations in drugdb.drug whose rxcui  │
│  is ANY of the salt rxcuis.                         │
│  Counts dosing_regimen rows per formulation         │
│  (used later to break ties).                        │
└──────────────────────┬──────────────────────────────┘
                       │ formulation_id, rxcui, source flags, dosing_row_count
                       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: best_formulation                              │
│  DISTINCT ON (rxcui) — one best formulation per     │
│  rxcui, ranked by data source quality:              │
│    1. DailyMed  (most authoritative)               │
│    2. OpenFDA                                       │
│    3. DrugBank                                      │
│    4. RxNorm                                        │
│  Tie-broken by most dosing rows, then formulation_id│
└──────────────────────┬──────────────────────────────┘
                       │ formulation_id, rxcui
                       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: ranked                                        │
│  Joins best_formulation → dosing_regimen.           │
│  Applies hard filters:                              │
│    • age_group = ANY($2)   ← patient's age groups  │
│    • renal_function = 'any'                         │
│    • hepatic_function = 'any'                       │
│    • pregnancy_status = 'any'                       │
│    • dose_basis = 'fixed'                           │
│    • frequency IS NOT NULL                          │
│    • dose_amount != 'CONTRAINDICATED'               │
│    • administration_notes NOT ILIKE '%pediatric%'   │
│                                                     │
│  ROW_NUMBER() deduplicates identical               │
│  (frequency, route, dose_value, dose_unit,          │
│   indication) combinations, preferring rows that   │
│  have BOTH indication AND instructions.            │
└──────────────────────┬──────────────────────────────┘
                       │ rn = 1 rows only
                       ▼
┌─────────────────────────────────────────────────────┐
│  FINAL SELECT                                       │
│  Joins ranked → best_formulation → indian_brand    │
│  Subquery: STRING_AGG ingredient names from        │
│   drug_ingredient_mapping + ingredients tables     │
│                                                     │
│  Returns per-dose-row:                             │
│    formulation_id, brand_name, salt_composition,   │
│    generic_name, frequency, route, dose_amount,    │
│    dose_unit, duration, indication, instructions   │
└─────────────────────────────────────────────────────┘
```

#### Full SQL

```sql
WITH salt_ingredients AS (
  SELECT ib.salt_composition, ib.rxcui
  FROM drugdb.indian_brand ib
  WHERE ib.drug_id_1mg = $1
    AND ib.match_combination NOT IN ('drugbank', 'us_unapproved')
  LIMIT 1
),
candidate_formulations AS (
  SELECT
    d.formulation_id,
    d.rxcui,
    d.generic_name,
    d.has_dailymed,
    d.has_openfda,
    d.has_drugbank,
    d.has_rxnorm,
    COUNT(dr.id) AS dosing_row_count
  FROM drugdb.drug d
  JOIN salt_ingredients si ON d.rxcui = ANY(si.rxcui)
  LEFT JOIN drugdb.dosing_regimen dr ON dr.formulation_id = d.formulation_id
  GROUP BY
    d.formulation_id, d.rxcui, d.generic_name,
    d.has_dailymed, d.has_openfda, d.has_drugbank, d.has_rxnorm
),
best_formulation AS (
  SELECT DISTINCT ON (rxcui)
    formulation_id,
    rxcui
  FROM candidate_formulations
  ORDER BY
    rxcui,
    CASE
      WHEN has_dailymed = true THEN 1
      WHEN has_openfda  = true THEN 2
      WHEN has_drugbank = true THEN 3
      WHEN has_rxnorm   = true THEN 4
      ELSE 5
    END ASC,
    dosing_row_count DESC,
    formulation_id ASC
),
ranked AS (
  SELECT
    dr.frequency,
    dr.route,
    dr.dose_amount,
    dr.dose_value,
    dr.dose_unit,
    dr.duration,
    dr.indication,
    dr.administration_notes,
    ROW_NUMBER() OVER (
      PARTITION BY
        dr.frequency,
        dr.route,
        dr.dose_value,
        dr.dose_unit,
        LOWER(COALESCE(dr.indication, ''))
      ORDER BY
        CASE
          WHEN dr.indication IS NOT NULL
           AND dr.administration_notes IS NOT NULL THEN 1
          WHEN dr.indication IS NOT NULL            THEN 2
          WHEN dr.administration_notes IS NOT NULL  THEN 3
          ELSE 4
        END ASC,
        dr.id ASC
    ) AS rn
  FROM best_formulation bf
  JOIN drugdb.dosing_regimen dr ON dr.formulation_id = bf.formulation_id
  WHERE dr.age_group        = ANY($2::text[])
    AND dr.renal_function   = 'any'
    AND dr.hepatic_function = 'any'
    AND dr.pregnancy_status = 'any'
    AND dr.dose_basis       = 'fixed'
    AND dr.frequency        IS NOT NULL
    AND UPPER(COALESCE(dr.dose_amount, '')) != 'CONTRAINDICATED'
    AND (dr.administration_notes NOT ILIKE '%pediatric%'
         OR dr.administration_notes IS NULL)
)
SELECT
  bf.formulation_id,
  ib.brand_name,
  ib.salt_composition,
  (
    SELECT STRING_AGG(i.name, ' / ' ORDER BY i.name)
    FROM drugdb.drug_ingredient_mapping dim
    JOIN drugdb.ingredients i ON i.id = dim.ingredient_id
    WHERE dim.formulation_id = bf.formulation_id
  ) AS generic_name,
  r.frequency,
  r.route,
  r.dose_amount,
  r.dose_unit,
  r.duration,
  LOWER(r.indication)      AS indication,
  r.administration_notes   AS instructions
FROM ranked r
CROSS JOIN best_formulation bf
JOIN drugdb.indian_brand ib
  ON ib.drug_id_1mg = $1
  AND ib.match_combination NOT IN ('drugbank', 'us_unapproved')
WHERE r.rn = 1
ORDER BY r.frequency, r.dose_value;
```

**Parameters**:
- `$1` — `drug_id_1mg` (string)
- `$2` — `age_groups` (text array, e.g. `["adult", "any"]`)

---

### Query 2 — Fallback Dosing Query

**File**: [queries/dosing_fallback.sql](../queries/dosing_fallback.sql)

**When**: Primary query returned 0 rows.

**Why a fallback exists**: Some Indian brand drugs are matched to DrugBank or US-unapproved RxCUI entries that don't have dosing data. The fallback resolves the drug's ingredients through their UNII identifiers to find a globally-matched formulation that does have dosing data.

**Key difference from primary**: Instead of matching by RxCUI directly, it goes through `UNII → DrugMasterLinkage → master_linkage_id`, allowing it to find equivalent international formulations even if the local RxCUI mapping is sparse.

#### Step-by-Step CTE Flow

```
indian_brand (drug_id_1mg = $1)   ← NO match_combination filter here
       │
       │ salt_composition, rxcui[]
       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: salt_ingredients                              │
│  Same as primary but WITHOUT the                   │
│  match_combination exclusion filter.               │
│  This lets drugbank-matched drugs reach the        │
│  fallback path.                                     │
└──────────────────────┬──────────────────────────────┘
                       │ rxcui[] (may include drugbank RxCUIs)
                       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: resolvable_rxcuis                             │
│  For each rxcui in the array:                       │
│    UNNEST rxcui[]  →  ingredients (rxcui match)     │
│    ingredients.unii → DrugMasterLinkage             │
│                        (unii_ids @> ARRAY[unii])    │
│                                                     │
│  Hard constraints:                                  │
│    • i.unii IS NOT NULL                             │
│    • array_length(dml.rxcui_ids, 1) = 1             │
│      (linkage must resolve to exactly 1 RxCUI —    │
│       ambiguous multi-RxCUI entries are excluded)   │
│                                                     │
│  Returns: (rxcui, master_linkage_id) pairs          │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: all_pass_check                                │
│  Verifies that EVERY ingredient in the drug can    │
│  be resolved via UNII:                              │
│    COUNT(DISTINCT resolvable_rxcuis.rxcui)          │
│      = array_length(salt_ingredients.rxcui, 1)      │
│                                                     │
│  If any ingredient fails to resolve → this CTE     │
│  returns no rows → downstream CTEs return nothing  │
│  → query returns 0 rows → 404                       │
└──────────────────────┬──────────────────────────────┘
                       │ (only if all ingredients resolve)
                       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: linkage                                       │
│  Collects the distinct master_linkage_ids from      │
│  resolvable_rxcuis (only when all_pass_check passes)│
└──────────────────────┬──────────────────────────────┘
                       │ master_linkage_id[]
                       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: candidate_formulations                        │
│  Joins drugdb.drug ON master_linkage_id             │
│  (not on rxcui like primary query)                  │
│  Counts dosing_regimen rows per formulation.        │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: best_formulation                              │
│  Identical ranking logic to primary query:         │
│    DailyMed > OpenFDA > DrugBank > RxNorm           │
│    Tie-broken by dosing_row_count, formulation_id   │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  CTE: ranked                                        │
│  Identical filtering and deduplication logic       │
│  to primary query.                                  │
└──────────────────────┬──────────────────────────────┘
                       │ rn = 1 rows only
                       ▼
┌─────────────────────────────────────────────────────┐
│  FINAL SELECT                                       │
│  Same columns as primary query.                     │
│                                                     │
│  Difference: brand_name and salt_composition are   │
│  fetched via a LATERAL subquery on indian_brand    │
│  (not a direct JOIN) because the formulation       │
│  was found by master_linkage_id, not drug_id_1mg.  │
└─────────────────────────────────────────────────────┘
```

#### Full SQL

```sql
WITH salt_ingredients AS (
  SELECT ib.salt_composition, ib.rxcui
  FROM drugdb.indian_brand ib
  WHERE ib.drug_id_1mg = $1
  LIMIT 1
),
resolvable_rxcuis AS (
  SELECT DISTINCT r.rxcui, dml.master_linkage_id
  FROM salt_ingredients si
  CROSS JOIN LATERAL unnest(si.rxcui) AS r(rxcui)
  JOIN drugdb.ingredients i ON i.rxcui = r.rxcui
  JOIN public."DrugMasterLinkage" dml ON dml.unii_ids @> ARRAY[i.unii::text]
  WHERE i.unii IS NOT NULL
    AND array_length(dml.rxcui_ids, 1) = 1
),
all_pass_check AS (
  SELECT 1
  FROM salt_ingredients si
  WHERE (SELECT COUNT(DISTINCT rxcui) FROM resolvable_rxcuis) = array_length(si.rxcui, 1)
),
linkage AS (
  SELECT DISTINCT master_linkage_id
  FROM resolvable_rxcuis
  WHERE EXISTS (SELECT 1 FROM all_pass_check)
),
candidate_formulations AS (
  SELECT
    d.formulation_id,
    d.rxcui,
    d.generic_name,
    d.has_dailymed,
    d.has_openfda,
    d.has_drugbank,
    d.has_rxnorm,
    COUNT(dr.id) AS dosing_row_count
  FROM drugdb.drug d
  JOIN linkage l ON d.master_linkage_id = l.master_linkage_id
  LEFT JOIN drugdb.dosing_regimen dr ON dr.formulation_id = d.formulation_id
  GROUP BY
    d.formulation_id, d.rxcui, d.generic_name,
    d.has_dailymed, d.has_openfda, d.has_drugbank, d.has_rxnorm
),
best_formulation AS (
  SELECT DISTINCT ON (rxcui)
    formulation_id,
    rxcui
  FROM candidate_formulations
  ORDER BY
    rxcui,
    CASE
      WHEN has_dailymed = true THEN 1
      WHEN has_openfda  = true THEN 2
      WHEN has_drugbank = true THEN 3
      WHEN has_rxnorm   = true THEN 4
      ELSE 5
    END ASC,
    dosing_row_count DESC,
    formulation_id ASC
),
ranked AS (
  SELECT
    dr.frequency,
    dr.route,
    dr.dose_amount,
    dr.dose_value,
    dr.dose_unit,
    dr.duration,
    dr.indication,
    dr.administration_notes,
    ROW_NUMBER() OVER (
      PARTITION BY
        dr.frequency,
        dr.route,
        dr.dose_value,
        dr.dose_unit,
        LOWER(COALESCE(dr.indication, ''))
      ORDER BY
        CASE
          WHEN dr.indication IS NOT NULL
           AND dr.administration_notes IS NOT NULL THEN 1
          WHEN dr.indication IS NOT NULL            THEN 2
          WHEN dr.administration_notes IS NOT NULL  THEN 3
          ELSE 4
        END ASC,
        dr.id ASC
    ) AS rn
  FROM best_formulation bf
  JOIN drugdb.dosing_regimen dr ON dr.formulation_id = bf.formulation_id
  WHERE dr.age_group        = ANY($2::text[])
    AND dr.renal_function   = 'any'
    AND dr.hepatic_function = 'any'
    AND dr.pregnancy_status = 'any'
    AND dr.dose_basis       = 'fixed'
    AND dr.frequency        IS NOT NULL
    AND UPPER(COALESCE(dr.dose_amount, '')) != 'CONTRAINDICATED'
    AND (dr.administration_notes NOT ILIKE '%pediatric%'
         OR dr.administration_notes IS NULL)
)
SELECT
  bf.formulation_id,
  ib.brand_name,
  ib.salt_composition,
  (
    SELECT STRING_AGG(i.name, ' / ' ORDER BY i.name)
    FROM drugdb.drug_ingredient_mapping dim
    JOIN drugdb.ingredients i ON i.id = dim.ingredient_id
    WHERE dim.formulation_id = bf.formulation_id
  ) AS generic_name,
  r.frequency,
  r.route,
  r.dose_amount,
  r.dose_unit,
  r.duration,
  LOWER(r.indication)      AS indication,
  r.administration_notes   AS instructions
FROM ranked r
CROSS JOIN best_formulation bf
JOIN LATERAL (
  SELECT brand_name, salt_composition
  FROM drugdb.indian_brand
  WHERE drug_id_1mg = $1
  LIMIT 1
) ib ON true
WHERE r.rn = 1
ORDER BY r.frequency, r.dose_value;
```

**Parameters**:
- `$1` — `drug_id_1mg` (string)
- `$2` — `age_groups` (text array, e.g. `["adult", "any"]`)

---

### Primary vs Fallback — Side-by-Side Comparison

| Aspect | Primary (`dosing.sql`) | Fallback (`dosing_fallback.sql`) |
|--------|----------------------|----------------------------------|
| **Triggered when** | Cache miss + drug_exists = true | Primary returns 0 rows |
| **Matching strategy** | Direct RxCUI from `indian_brand` → `drug.rxcui` | RxCUI → UNII → DrugMasterLinkage → `drug.master_linkage_id` |
| **match_combination filter** | Excludes `drugbank` and `us_unapproved` | No filter — includes all entries |
| **All-ingredients check** | Not needed (direct match) | All salt ingredients must resolve via UNII |
| **brand_name source** | Direct JOIN on `indian_brand` | LATERAL subquery on `indian_brand` |
| **Use case** | Standard Indian brands with clean RxCUI mapping | Drugs matched via DrugBank with UNII cross-reference available |

---

## GET /health

**No SQL queries are run** unless the health check endpoint probes the DB.

**Flow**:
```
GET /health (no auth required)
       │
       ▼
┌──────────────────────────────┐
│  Try: SELECT 1 on PostgreSQL │  ← DB connectivity test
│  Try: redis.ping()           │  ← Cache connectivity test
└──────────────────┬───────────┘
                   │
       ┌───────────┴───────────┐
       │ both OK               │ either fails
       ▼                       ▼
  200 { status: "ok",     503 { status: "degraded",
        db: "connected",        db: "connected"/"disconnected",
        cache: "connected" }    cache: "connected"/"disconnected" }
```

**DB Probe SQL**:
```sql
SELECT 1
```

---

## Database Tables Reference

| Table | Schema | Purpose |
|-------|--------|---------|
| `indian_brand` | `drugdb` | Maps 1mg catalog drug IDs to RxCUI, brand name, salt composition, and match source |
| `drug` | `drugdb` | Formulation registry with source flags (DailyMed, OpenFDA, etc.) and master_linkage_id |
| `dosing_regimen` | `drugdb` | Individual dosing rows per formulation (frequency, route, dose, age group, conditions) |
| `drug_ingredient_mapping` | `drugdb` | Maps formulation_id → ingredient_id |
| `ingredients` | `drugdb` | Ingredient records with rxcui and unii identifiers |
| `DrugMasterLinkage` | `public` | Cross-reference: unii_ids[] ↔ rxcui_ids[] ↔ master_linkage_id |

### Key Columns in `dosing_regimen` and their filter values

| Column | Filter used | Meaning |
|--------|------------|---------|
| `age_group` | `= ANY($2)` | e.g. `adult`, `pediatric`, `any` |
| `renal_function` | `= 'any'` | Only standard renal function |
| `hepatic_function` | `= 'any'` | Only standard hepatic function |
| `pregnancy_status` | `= 'any'` | Excludes pregnancy-specific doses |
| `dose_basis` | `= 'fixed'` | Excludes weight-based dosing |
| `frequency` | `IS NOT NULL` | Rows without frequency are excluded |
| `dose_amount` | `!= 'CONTRAINDICATED'` | Excludes contraindicated entries |
| `administration_notes` | `NOT ILIKE '%pediatric%'` | Excludes pediatric-only instructions |
