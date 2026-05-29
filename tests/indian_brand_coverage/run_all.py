#!/usr/bin/env python3
"""
Run all 4 indian_brand age-coverage tests simultaneously.

Hits the live /api/v1/dosing endpoint — no raw SQL, 100% faithful to real
service behaviour. All 4 files fire concurrently via asyncio.gather().

Usage (from project root):
    python3 tests/indian_brand_coverage/run_all.py
"""
import asyncio
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import aiohttp
import asyncpg
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
DB_URL = os.environ["DATABASE_URL"]

from _common import (
    FETCH_ALL_DRUGS_SQL,
    CONCURRENCY,
    BASE_URL,
    API_KEY,
    run_coverage,
    find_latest_log,
)
from test_neonate_infant import AGE_GROUP_MAP as AGE_NEONATE_INFANT, LABEL as LABEL_NI
from test_pediatric      import AGE_GROUP_MAP as AGE_PEDIATRIC,      LABEL as LABEL_PED
from test_adult          import AGE_GROUP_MAP as AGE_ADULT,          LABEL as LABEL_ADULT
from test_geriatric      import AGE_GROUP_MAP as AGE_GERIATRIC,      LABEL as LABEL_GER


async def main() -> None:
    print("=" * 80)
    print("indian_brand age-coverage  —  all 4 age-group files starting NOW")
    print(f"  Endpoint : {BASE_URL}")
    print("  File 1 : neonate (age=0)  + infant (age=1)")
    print("  File 2 : pediatric        (age=10)")
    print("  File 3 : adult            (age=30)")
    print("  File 4 : geriatric        (age=70)")
    print("=" * 80)

    # ── Auto-detect prior logs for resume ─────────────────────────────────────
    prior_logs = {
        LABEL_NI:    find_latest_log(LABEL_NI),
        LABEL_PED:   find_latest_log(LABEL_PED),
        LABEL_ADULT: find_latest_log(LABEL_ADULT),
        LABEL_GER:   find_latest_log(LABEL_GER),
    }
    for label, log in prior_logs.items():
        if log:
            print(f"  [resume] {label}: continuing from {log.name}")
        else:
            print(f"  [fresh ] {label}: starting from scratch")
    print("─" * 80)

    t0 = time.perf_counter()

    # ── Fetch drug list once from DB ──────────────────────────────────────────
    print("Fetching drug list from drugdb.indian_brand …", flush=True)
    t_fetch = time.perf_counter()
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3, command_timeout=30)
    async with pool.acquire() as conn:
        drugs = await conn.fetch(FETCH_ALL_DRUGS_SQL)
    await pool.close()
    print(f"  → {len(drugs):,} drug_ids fetched in {time.perf_counter()-t_fetch:.1f}s")
    print("─" * 80)

    # ── Shared aiohttp session: 4 files × CONCURRENCY concurrent connections ──
    connector = aiohttp.TCPConnector(limit=CONCURRENCY * 4)
    async with aiohttp.ClientSession(connector=connector, headers={"X-API-Key": API_KEY}) as session:
        all_stats = await asyncio.gather(
            run_coverage(label=LABEL_NI,    age_group_map=AGE_NEONATE_INFANT, session=session, drugs=drugs, resume_log=prior_logs[LABEL_NI]),
            run_coverage(label=LABEL_PED,   age_group_map=AGE_PEDIATRIC,      session=session, drugs=drugs, resume_log=prior_logs[LABEL_PED]),
            run_coverage(label=LABEL_ADULT, age_group_map=AGE_ADULT,          session=session, drugs=drugs, resume_log=prior_logs[LABEL_ADULT]),
            run_coverage(label=LABEL_GER,   age_group_map=AGE_GERIATRIC,      session=session, drugs=drugs, resume_log=prior_logs[LABEL_GER]),
            return_exceptions=True,
        )

    elapsed = time.perf_counter() - t0

    labels = [LABEL_NI, LABEL_PED, LABEL_ADULT, LABEL_GER]
    print("\n" + "═" * 80)
    print(f"ALL DONE  ─  wall time {elapsed:.1f}s  ({elapsed/60:.1f} min)")
    print("═" * 80)
    for label, result in zip(labels, all_stats):
        if isinstance(result, Exception):
            print(f"  [{label}]  FAILED: {result}")
        else:
            print(f"  [{label}]  OK — logs in tests/indian_brand_coverage/logs/")
    print("═" * 80)


if __name__ == "__main__":
    asyncio.run(main())
