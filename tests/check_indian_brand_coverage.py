#!/usr/bin/env python3
"""
Check how many drugs from top_500_india_drugs.csv exist in drugdb.indian_brand.

This is a BRAND-PRESENCE check only (not dosing coverage).
It answers: "Is this brand name in our indian_brand table at all?"

The lookup uses the same multi-strategy search as the dosing service:
  1. Full CSV name prefix:  'Dolo 650%'
  2. Strength-stripped:     'Dolo%'
  3. First word only:       'Dolo%'

Usage:
  python tests/check_indian_brand_coverage.py
  python tests/check_indian_brand_coverage.py --csv tests/top_500_india_drugs.csv
"""

import argparse
import asyncio
import csv
import os
import re
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

DB_URL = os.environ["DATABASE_URL"]

FIND_BRAND_SQL = """
SELECT DISTINCT ON (drug_id_1mg)
    drug_id_1mg,
    brand_name,
    salt_composition,
    match_combination
FROM drugdb.indian_brand
WHERE brand_name ILIKE $1 || '%'
ORDER BY drug_id_1mg
LIMIT 3
"""


def strip_strength(name: str) -> str:
    """Remove trailing strength tokens: 'Dolo 650' → 'Dolo', 'Voveran SR 100' → 'Voveran SR'."""
    cleaned = re.sub(r"\s+\d[\d./\s]*(?:mg|mcg|g|IU|%|ml|lakh)?$", "", name, flags=re.I).strip()
    return cleaned or name


async def find_brand(conn, brand_name: str):
    # Strategy 1: full name prefix
    rows = await conn.fetch(FIND_BRAND_SQL, brand_name)
    if rows:
        return rows, "full_name"

    # Strategy 2: strip strength suffix
    stripped = strip_strength(brand_name)
    if stripped != brand_name:
        rows = await conn.fetch(FIND_BRAND_SQL, stripped)
        if rows:
            return rows, "stripped_strength"

    # Strategy 3: first word only
    first_word = brand_name.split()[0]
    if first_word not in (brand_name, stripped):
        rows = await conn.fetch(FIND_BRAND_SQL, first_word)
        if rows:
            return rows, "first_word"

    return [], "not_found"


async def main(csv_path: str) -> None:
    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=5, command_timeout=30)

    with open(csv_path, newline="", encoding="utf-8") as f:
        drugs = list(csv.DictReader(f))

    found = []
    not_found = []

    print(f"Checking {len(drugs)} drugs against drugdb.indian_brand ...\n")
    print(f"{'#':<5} {'Brand Name (CSV)':<30} {'Status':<15} {'DB brand_name':<35} {'drug_id_1mg':<12} {'match_via'}")
    print("-" * 120)

    async with pool.acquire() as conn:
        for row in drugs:
            num = row["#"]
            brand_name = row["Brand Name (India)"]

            db_rows, strategy = await find_brand(conn, brand_name)

            if db_rows:
                match = db_rows[0]
                found.append({
                    "num": num,
                    "csv_name": brand_name,
                    "db_name": match["brand_name"],
                    "drug_id": match["drug_id_1mg"],
                    "match_combination": match["match_combination"],
                    "strategy": strategy,
                })
                print(
                    f"{num:<5} {brand_name:<30} {'FOUND':<15} "
                    f"{match['brand_name'][:33]:<35} {match['drug_id_1mg']:<12} {strategy}"
                )
            else:
                not_found.append({"num": num, "csv_name": brand_name})
                print(f"{num:<5} {brand_name:<30} {'NOT FOUND':<15}")

    await pool.close()

    total = len(drugs)
    n_found = len(found)
    n_missing = len(not_found)

    print("\n" + "=" * 80)
    print("SUMMARY")
    print(f"  Total in CSV  : {total}")
    print(f"  Found in DB   : {n_found:4d}  ({n_found/total*100:.1f}%)")
    print(f"  NOT in DB     : {n_missing:4d}  ({n_missing/total*100:.1f}%)")
    print("=" * 80)

    if not_found:
        print(f"\nMISSING from indian_brand ({n_missing}):")
        for r in not_found:
            print(f"  #{r['num']:<5} {r['csv_name']}")

    print(f"\nBreakdown of found drugs by match strategy:")
    for strat in ("full_name", "stripped_strength", "first_word"):
        count = sum(1 for r in found if r["strategy"] == strat)
        print(f"  {strat:<20}: {count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv",
        default=str(Path(__file__).parent / "top_500_india_drugs.csv"),
        help="Path to CSV (default: tests/top_500_india_drugs.csv)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.csv))
