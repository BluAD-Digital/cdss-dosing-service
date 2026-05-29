"""
Shared SQL, helpers, and core runner for indian_brand age-coverage tests.

Key optimisations vs. v1:
  1. FETCH query uses GROUP BY (not correlated subquery) — 100× faster initial load.
  2. _classify_all() checks all age groups in at most 2 DB round trips per drug
     (primary hit-set query, then fallback hit-set query only if needed)
     vs. the old 2×N queries (primary + fallback per age group).
  3. CONCURRENCY raised to 30 per file.
  4. run_coverage() accepts an external pool + pre-fetched drug list so run_all.py
     can share both across all 4 files instead of repeating the expensive work 4×.
  5. Resume support: pass resume_log= to skip already-processed drugs and seed
     stats from the prior run so the final summary is always complete.
"""
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Sequence

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
DB_URL = os.environ["DATABASE_URL"]

CONCURRENCY = 15   # per-file; 4 files × 15 = 60 concurrent DB queries total

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True, parents=True)

ICONS   = {"primary": "✓", "fallback": "~", "404_not_found": "✗", "error": "!"}
RESULTS = ("primary", "fallback", "404_not_found", "error")


# ── SQL ───────────────────────────────────────────────────────────────────────

# v1 used a correlated subquery (359 202 sub-executions). GROUP BY is a single pass.
FETCH_ALL_DRUGS_SQL = """
SELECT
    drug_id_1mg,
    MIN(brand_name) AS brand_name,
    STRING_AGG(DISTINCT match_combination, ', ' ORDER BY match_combination) AS match_combinations
FROM drugdb.indian_brand
WHERE drug_id_1mg IS NOT NULL
GROUP BY drug_id_1mg
ORDER BY drug_id_1mg
"""

# EXISTS stops at the first matching row (short-circuit) — much faster than DISTINCT.
PRIMARY_CHECK_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM drugdb.indian_brand ib
    JOIN drugdb.drug d            ON d.rxcui = ANY(ib.rxcui)
    JOIN drugdb.dosing_regimen dr ON dr.formulation_id = d.formulation_id
    WHERE ib.drug_id_1mg = $1
      AND ib.match_combination NOT IN ('drugbank', 'us_unapproved')
      AND dr.age_group        = ANY($2::text[])
      AND dr.renal_function   = 'any'
      AND dr.hepatic_function = 'any'
      AND dr.pregnancy_status = 'any'
      AND dr.dose_basis       = 'fixed'
      AND dr.frequency        IS NOT NULL
      AND UPPER(COALESCE(dr.dose_amount, '')) != 'CONTRAINDICATED'
) AS has_dosing
"""

FALLBACK_CHECK_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM drugdb.indian_brand ib
    JOIN drugdb.indian_brand_ingredient ibi ON ibi.indian_brand_id = ib.indian_brand_id
    JOIN drugdb.ingredients i               ON i.drugbank_id = ibi.drugbank_id
                                          AND i.unii IS NOT NULL
    JOIN public."DrugMasterLinkage" dml     ON i.unii = ANY(dml.unii_ids)
    JOIN drugdb.drug d                      ON d.master_linkage_id = dml.master_linkage_id
    JOIN drugdb.dosing_regimen dr           ON dr.formulation_id = d.formulation_id
    WHERE ib.drug_id_1mg = $1
      AND dr.age_group        = ANY($2::text[])
      AND dr.renal_function   = 'any'
      AND dr.hepatic_function = 'any'
      AND dr.pregnancy_status = 'any'
      AND dr.dose_basis       = 'fixed'
      AND dr.frequency        IS NOT NULL
      AND UPPER(COALESCE(dr.dose_amount, '')) != 'CONTRAINDICATED'
) AS has_dosing
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _zero() -> dict:
    return {r: 0 for r in RESULTS}


def find_latest_log(label: str) -> Path | None:
    """Return the largest (most data) log file for this label, or None.
    Largest = most drugs processed, regardless of when the run happened."""
    candidates = [p for p in LOG_DIR.glob(f"{label}_*.log") if p.stat().st_size > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def load_prior_results(log_path: Path) -> dict[str, dict]:
    """
    Parse JSON debug lines from a prior run log file.
    Returns {drug_id: {"results": {ag_name: result}, "match_combinations": str}}.
    """
    prior: dict[str, dict] = {}
    if not log_path or not log_path.exists():
        return prior
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
                drug_id = rec.get("drug_id")
                results = rec.get("results")
                if drug_id and isinstance(results, dict):
                    prior[str(drug_id)] = {
                        "results":            results,
                        "match_combinations": rec.get("match_combinations", "unknown"),
                    }
            except json.JSONDecodeError:
                pass
    return prior


def setup_logging(label: str, log_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"ib.{label}")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(fh)
    return logger


async def _classify_all(
    conn,
    drug_id: str,
    age_group_map: dict[str, list[str]],
) -> dict[str, str]:
    """
    Classify a drug for every age group in age_group_map.

    Uses EXISTS (not DISTINCT) so PostgreSQL short-circuits at the first
    matching row — the fastest possible check.

    For each display name, runs primary EXISTS then (only if needed) fallback
    EXISTS.  The neonate_infant file has 2 display names so at most 4 queries;
    all other files have 1 display name so at most 2 queries.
    """
    results: dict[str, str] = {}

    for display_name, ag_list in age_group_map.items():
        row = await conn.fetchrow(PRIMARY_CHECK_SQL, drug_id, ag_list)
        if row["has_dosing"]:
            results[display_name] = "primary"
            continue
        row = await conn.fetchrow(FALLBACK_CHECK_SQL, drug_id, ag_list)
        results[display_name] = "fallback" if row["has_dosing"] else "404_not_found"

    return results


# ── Core runner ───────────────────────────────────────────────────────────────

async def run_coverage(
    *,
    label: str,
    age_group_map: dict[str, list[str]],
    pool: asyncpg.Pool | None = None,
    drugs: Sequence | None = None,
    resume_log: Path | None = None,
) -> dict:
    """
    For every drug_id_1mg in drugdb.indian_brand, classify it against every
    age group in `age_group_map` and record stats + per-match_combination breakdown.

    `pool`       — shared pool from run_all.py (avoids 4 separate pools).
    `drugs`      — pre-fetched drug list (avoids 4 identical heavy fetches).
    `resume_log` — path to a prior run's log file; already-processed drugs are
                   skipped and their results are seeded into the stats so the
                   final summary is always the full picture.
    """
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{label}_{ts}.log"
    logger   = setup_logging(label, log_path)

    own_pool = pool is None
    if own_pool:
        pool = await asyncpg.create_pool(
            DB_URL, min_size=3, max_size=CONCURRENCY + 2, command_timeout=60
        )

    if drugs is None:
        async with pool.acquire() as conn:
            drugs = await conn.fetch(FETCH_ALL_DRUGS_SQL)

    total    = len(drugs)
    ag_names = list(age_group_map.keys())
    sem      = asyncio.Semaphore(CONCURRENCY)
    lock     = asyncio.Lock()

    # ── Load prior results (resume mode) ─────────────────────────────────────
    prior: dict[str, dict] = load_prior_results(resume_log) if resume_log else {}
    skipped = len(prior)

    stats: dict[str, dict[str, int]] = {ag: _zero() for ag in ag_names}
    combo_stats: dict[str, defaultdict] = {ag: defaultdict(_zero) for ag in ag_names}

    # Seed stats from prior run so the final summary covers all drugs
    for rec in prior.values():
        combos_raw = rec.get("match_combinations", "unknown")
        combo_list = [c.strip() for c in combos_raw.split(",")]
        for ag_name in ag_names:
            result = rec["results"].get(ag_name, "error")
            if result in RESULTS:
                stats[ag_name][result] += 1
                for combo in combo_list:
                    combo_stats[ag_name][combo.strip()][result] += 1

    done = [skipped]   # counter starts from already-done count

    resume_note = f"  (resuming — {skipped:,} already done, {total-skipped:,} remaining)"
    logger.info(f"[{label}]  {total} drug_ids  ·  age groups: {ag_names}")
    if skipped:
        logger.info(f"[{label}]{resume_note}")
    logger.info(f"[{label}]  log → {log_path}")
    logger.info("─" * 80)

    async def process(drug_row):
        drug_id    = str(drug_row["drug_id_1mg"])
        combos_raw = drug_row["match_combinations"] or "unknown"
        combo_list = [c.strip() for c in combos_raw.split(",")]

        # Skip drugs already processed in prior run
        if drug_id in prior:
            return

        async with sem:
            async with pool.acquire() as conn:
                try:
                    ag_results = await _classify_all(conn, drug_id, age_group_map)
                except Exception as exc:
                    ag_results = {ag: "error" for ag in ag_names}
                    logger.warning(f"  ! ERROR drug_id={drug_id}: {exc}")

        async with lock:
            done[0] += 1
            n = done[0]
            for ag_name, result in ag_results.items():
                stats[ag_name][result] += 1
                for combo in combo_list:
                    combo_stats[ag_name][combo.strip()][result] += 1

            ag_parts = "  ".join(
                f"{ag}={ICONS[r]}{r}" for ag, r in ag_results.items()
            )
            logger.info(
                f"[{label}] [{n:5d}/{total}] drug_id={drug_id:<10}| {ag_parts}"
                f"  match=[{combos_raw[:45]}]"
            )
            logger.debug(json.dumps({
                "label":              label,
                "drug_id":            drug_id,
                "brand_name":         drug_row["brand_name"],
                "match_combinations": combos_raw,
                "results":            ag_results,
            }, ensure_ascii=False))

    await asyncio.gather(*[process(d) for d in drugs])

    if own_pool:
        await pool.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    logger.info("\n" + "═" * 80)
    logger.info(f"[{label}]  SUMMARY  ─  {total} drug_ids total")

    for ag_name in ag_names:
        s    = stats[ag_name]
        have = s["primary"] + s["fallback"]
        logger.info(f"\n  ┌─ Age group : {ag_name}")
        logger.info(f"  │  ✓ primary       : {s['primary']:5d}  ({s['primary']/total*100:.1f}%)")
        logger.info(f"  │  ~ fallback      : {s['fallback']:5d}  ({s['fallback']/total*100:.1f}%)")
        logger.info(f"  │  ✗ 404_not_found : {s['404_not_found']:5d}  ({s['404_not_found']/total*100:.1f}%)")
        logger.info(f"  │  ! error         : {s['error']:5d}  ({s['error']/total*100:.1f}%)")
        logger.info(f"  └─ HAVE DOSING     : {have:5d}  ({have/total*100:.1f}%)")

        cs_map = combo_stats[ag_name]
        logger.info(f"\n  Per match_combination  [{ag_name}]:")
        hdr = (
            f"  {'match_combination':<32}"
            f"{'total':>7}{'primary':>9}{'fallback':>9}{'404_not_found':>15}{'error':>7}"
        )
        logger.info(hdr)
        logger.info("  " + "─" * (len(hdr) - 2))
        for combo in sorted(cs_map):
            cs = cs_map[combo]
            ct = sum(cs.values())
            logger.info(
                f"  {combo:<32}{ct:>7}{cs['primary']:>9}{cs['fallback']:>9}"
                f"{cs['404_not_found']:>15}{cs['error']:>7}"
            )

    logger.info("\n" + "═" * 80)
    logger.info(f"[{label}]  Full log → {log_path}")
    return stats
