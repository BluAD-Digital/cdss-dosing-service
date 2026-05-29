"""
Shared helpers and core runner for indian_brand age-coverage tests.

Hits the live /api/v1/dosing endpoint instead of raw SQL — 100% faithful to
real service behaviour, no SQL to maintain. The `source` field in the response
tells us whether the result came from the primary or fallback path.

AGE_GROUP_MAP format (per test file):
    { display_name: representative_age_in_years }
    e.g. {"neonate": 0, "infant": 1}

The service maps ages to groups internally via age_mapper.py:
    0        → neonate
    1        → infant + neonate
    2–17     → pediatric + any
    18–64    → adult + any
    65+      → geriatric + adult + any
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

import aiohttp
import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
DB_URL       = os.environ["DATABASE_URL"]
BASE_URL     = os.environ.get("DOSING_BASE_URL", "http://34.14.197.45:8001/api/v1/dosing")
API_KEY      = os.environ["API_KEY"]
CONCURRENCY  = 20   # per file; 4 files × 20 = 80 concurrent — matches server pool of 20×4 workers

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True, parents=True)

ICONS   = {"primary": "✓", "fallback": "~", "404_not_found": "✗", "error": "!"}
RESULTS = ("primary", "fallback", "404_not_found", "error")

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _zero() -> dict:
    return {r: 0 for r in RESULTS}


def find_latest_log(label: str) -> Path | None:
    """Return the largest (most data) log file for this label, or None."""
    candidates = [p for p in LOG_DIR.glob(f"{label}_*.log") if p.stat().st_size > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def load_prior_results(log_path: Path) -> dict[str, dict]:
    """Parse JSON debug lines from a prior run log. Returns {drug_id: {results, match_combinations}}."""
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
    session: aiohttp.ClientSession,
    drug_id: str,
    age_group_map: dict[str, int],
) -> dict[str, str]:
    """
    Hit the dosing endpoint once per age group, all in parallel.
    Returns {display_name: "primary" | "fallback" | "404_not_found" | "error"}.
    """
    async def check_one(display_name: str, age: int) -> tuple[str, str]:
        try:
            async with session.post(BASE_URL, json={"drug_id_1mg": drug_id, "age": age}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return display_name, data.get("source", "primary")
                if resp.status == 404:
                    return display_name, "404_not_found"
                return display_name, "error"
        except Exception:
            return display_name, "error"

    return dict(await asyncio.gather(*[
        check_one(name, age) for name, age in age_group_map.items()
    ]))


# ── Core runner ───────────────────────────────────────────────────────────────

async def run_coverage(
    *,
    label: str,
    age_group_map: dict[str, int],
    session: aiohttp.ClientSession | None = None,
    drugs: Sequence | None = None,
    resume_log: Path | None = None,
) -> dict:
    """
    For every drug_id_1mg in drugdb.indian_brand, hit the dosing endpoint for
    each age group and record stats + per-match_combination breakdown.

    `session`    — shared aiohttp session from run_all.py.
    `drugs`      — pre-fetched drug list (avoids 4 identical DB fetches).
    `resume_log` — path to a prior run's log; already-processed drugs are skipped
                   and seeded into stats so the final summary is always complete.
    """
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{label}_{ts}.log"
    logger   = setup_logging(label, log_path)

    own_session = session is None
    if own_session:
        connector = aiohttp.TCPConnector(limit=CONCURRENCY + 4)
        session   = aiohttp.ClientSession(connector=connector, headers={"X-API-Key": API_KEY})

    if drugs is None:
        pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3, command_timeout=30)
        async with pool.acquire() as conn:
            drugs = await conn.fetch(FETCH_ALL_DRUGS_SQL)
        await pool.close()

    total    = len(drugs)
    ag_names = list(age_group_map.keys())
    sem      = asyncio.Semaphore(CONCURRENCY)
    lock     = asyncio.Lock()

    # ── Resume ───────────────────────────────────────────────────────────────
    prior: dict[str, dict] = load_prior_results(resume_log) if resume_log else {}
    skipped = len(prior)

    stats: dict[str, dict[str, int]] = {ag: _zero() for ag in ag_names}
    combo_stats: dict[str, defaultdict] = {ag: defaultdict(_zero) for ag in ag_names}

    for rec in prior.values():
        combos_raw = rec.get("match_combinations", "unknown")
        combo_list = [c.strip() for c in combos_raw.split(",")]
        for ag_name in ag_names:
            result = rec["results"].get(ag_name, "error")
            if result in RESULTS:
                stats[ag_name][result] += 1
                for combo in combo_list:
                    combo_stats[ag_name][combo.strip()][result] += 1

    done = [skipped]

    logger.info(f"[{label}]  {total} drug_ids  ·  age groups: {ag_names}  ·  endpoint: {BASE_URL}")
    if skipped:
        logger.info(f"[{label}]  (resuming — {skipped:,} already done, {total-skipped:,} remaining)")
    logger.info(f"[{label}]  log → {log_path}")
    logger.info("─" * 80)

    async def process(drug_row):
        drug_id    = str(drug_row["drug_id_1mg"])
        combos_raw = drug_row["match_combinations"] or "unknown"
        combo_list = [c.strip() for c in combos_raw.split(",")]

        if drug_id in prior:
            return

        async with sem:
            ag_results = await _classify_all(session, drug_id, age_group_map)

        async with lock:
            done[0] += 1
            n = done[0]
            for ag_name, result in ag_results.items():
                stats[ag_name][result] += 1
                for combo in combo_list:
                    combo_stats[ag_name][combo.strip()][result] += 1

            ag_parts = "  ".join(f"{ag}={ICONS[r]}{r}" for ag, r in ag_results.items())
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

    if own_session:
        await session.close()

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
