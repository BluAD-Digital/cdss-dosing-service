#!/usr/bin/env python3
"""
Deep investigation of brands NOT found in drugdb.indian_brand.

For each missing drug it tries:
  1. Full name prefix                : 'Arcoxia 90%'
  2. Strength stripped               : 'Arcoxia%'
  3. First word only                 : 'Arcoxia%'
  4. Each word individually          : 'Arcoxia', '90'
  5. Salt/generic keyword search     : look for any indian_brand row whose
                                       salt_composition contains the first
                                       ingredient keyword from the CSV
  6. DB sample of what IS in table   : show existing brands with same salt

Outputs a diagnosis CSV + console report.

Usage:
  python3 tests/investigate_missing_brands.py
"""

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


# ─────────────────────────────────────────────────────────────────────────────
# SQL helpers
# ─────────────────────────────────────────────────────────────────────────────

BRAND_PREFIX_SQL = """
SELECT drug_id_1mg, brand_name, salt_composition, match_combination
FROM drugdb.indian_brand
WHERE brand_name ILIKE $1 || '%'
LIMIT 3
"""

BRAND_CONTAINS_SQL = """
SELECT drug_id_1mg, brand_name, salt_composition, match_combination
FROM drugdb.indian_brand
WHERE brand_name ILIKE '%' || $1 || '%'
LIMIT 3
"""

# Search by the first active ingredient keyword in any brand's salt_composition
SALT_SEARCH_SQL = """
SELECT DISTINCT ON (drug_id_1mg)
    drug_id_1mg, brand_name, salt_composition, match_combination
FROM drugdb.indian_brand
WHERE salt_composition ILIKE '%' || $1 || '%'
ORDER BY drug_id_1mg
LIMIT 5
"""


def strip_strength(name: str) -> str:
    cleaned = re.sub(
        r"\s+\d[\d./\s]*(?:mg|mcg|g|IU|%|ml|lakh|IU/ml)?$", "", name, flags=re.I
    ).strip()
    return cleaned or name


def first_ingredient_keyword(composition: str) -> str:
    """Extract the first meaningful word from the salt composition."""
    # Take text before first '+' or '/'
    first_part = re.split(r"[+/]", composition)[0].strip()
    # Remove strength numbers and units
    first_part = re.sub(r"\s*\d[\d./\s]*(?:mg|mcg|g|IU|%|ml|lakh)?", "", first_part, flags=re.I).strip()
    # Take the longest word (avoids 'Acid', 'Sodium' etc.)
    words = [w for w in first_part.split() if len(w) > 3]
    return words[0] if words else first_part.split()[0] if first_part.split() else ""


async def investigate_one(conn, num: str, brand_name: str, composition: str) -> dict:
    result = {
        "num": num,
        "csv_brand": brand_name,
        "composition": composition,
        "found_via": None,
        "drug_id_1mg": None,
        "db_brand_name": None,
        "db_salt": None,
        "match_combination": None,
        "diagnosis": None,
        "salt_alternatives": [],
    }

    # 1. Full name prefix
    rows = await conn.fetch(BRAND_PREFIX_SQL, brand_name)
    if rows:
        r = rows[0]
        result.update(found_via="full_name", drug_id_1mg=r["drug_id_1mg"],
                      db_brand_name=r["brand_name"], db_salt=r["salt_composition"],
                      match_combination=r["match_combination"], diagnosis="OK_full_name")
        return result

    # 2. Strength stripped
    stripped = strip_strength(brand_name)
    if stripped != brand_name:
        rows = await conn.fetch(BRAND_PREFIX_SQL, stripped)
        if rows:
            r = rows[0]
            result.update(found_via="stripped", drug_id_1mg=r["drug_id_1mg"],
                          db_brand_name=r["brand_name"], db_salt=r["salt_composition"],
                          match_combination=r["match_combination"], diagnosis="OK_stripped")
            return result

    # 3. First word
    first_word = brand_name.split()[0]
    if first_word not in (brand_name, stripped):
        rows = await conn.fetch(BRAND_PREFIX_SQL, first_word)
        if rows:
            r = rows[0]
            result.update(found_via="first_word", drug_id_1mg=r["drug_id_1mg"],
                          db_brand_name=r["brand_name"], db_salt=r["salt_composition"],
                          match_combination=r["match_combination"], diagnosis="OK_first_word")
            return result

    # 4. Contains search (brand_name anywhere in DB brand_name)
    for keyword in [brand_name, stripped, first_word]:
        if len(keyword) > 3:
            rows = await conn.fetch(BRAND_CONTAINS_SQL, keyword)
            if rows:
                r = rows[0]
                result.update(found_via=f"contains:{keyword}", drug_id_1mg=r["drug_id_1mg"],
                              db_brand_name=r["brand_name"], db_salt=r["salt_composition"],
                              match_combination=r["match_combination"], diagnosis="OK_contains")
                return result

    # 5. Salt/ingredient keyword search — can we find ANY brand with this salt?
    keyword = first_ingredient_keyword(composition)
    salt_rows = []
    if keyword and len(keyword) > 3:
        salt_rows = await conn.fetch(SALT_SEARCH_SQL, keyword)

    result["salt_alternatives"] = [
        {"drug_id": r["drug_id_1mg"], "brand": r["brand_name"], "salt": r["salt_composition"]}
        for r in salt_rows
    ]

    if salt_rows:
        result["diagnosis"] = f"BRAND_MISSING_SALT_EXISTS:{keyword}"
    else:
        result["diagnosis"] = f"DRUG_NOT_IN_DB:{keyword}"

    return result


async def main(csv_path: str) -> None:
    # Load CSV
    with open(csv_path, newline="", encoding="utf-8") as f:
        all_drugs = list(csv.DictReader(f))

    # Run the fast coverage check first to find which are missing
    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=8, command_timeout=30)

    FAST_CHECK_SQL = """
    SELECT 1 FROM drugdb.indian_brand
    WHERE brand_name ILIKE $1 || '%' LIMIT 1
    """

    missing = []
    async with pool.acquire() as conn:
        for row in all_drugs:
            brand = row["Brand Name (India)"]
            found = await conn.fetchrow(FAST_CHECK_SQL, brand)
            if not found:
                stripped = strip_strength(brand)
                if stripped != brand:
                    found = await conn.fetchrow(FAST_CHECK_SQL, stripped)
            if not found:
                first_word = brand.split()[0]
                if first_word not in (brand, stripped):
                    found = await conn.fetchrow(FAST_CHECK_SQL, first_word)
            if not found:
                missing.append(row)

    print(f"Found {len(missing)} drugs not in indian_brand. Investigating each...\n")

    # Deep investigation
    results = []
    async with pool.acquire() as conn:
        for row in missing:
            r = await investigate_one(conn, row["#"], row["Brand Name (India)"], row["Salt Composition"])
            results.append(r)

    await pool.close()

    # ── Console report ──────────────────────────────────────────────────────
    found_with_contains   = [r for r in results if r["found_via"] and r["found_via"].startswith("contains")]
    brand_missing_but_salt = [r for r in results if r["diagnosis"] and r["diagnosis"].startswith("BRAND_MISSING_SALT_EXISTS")]
    truly_missing          = [r for r in results if r["diagnosis"] and r["diagnosis"].startswith("DRUG_NOT_IN_DB")]

    print("=" * 100)
    print(f"INVESTIGATION SUMMARY  (total missing = {len(results)})")
    print("=" * 100)
    print(f"  A) Found via contains/fuzzy match     : {len(found_with_contains)}")
    print(f"  B) Brand missing BUT salt exists in DB: {len(brand_missing_but_salt)}")
    print(f"  C) Drug completely absent from DB     : {len(truly_missing)}")
    print()

    # ── Group A: fixable via contains ──────────────────────────────────────
    if found_with_contains:
        print("─" * 100)
        print(f"GROUP A — Fixable with contains/fuzzy search ({len(found_with_contains)} drugs)")
        print("  These matched when we searched brand_name ILIKE '%keyword%'. The current")
        print("  prefix-only search misses them. Fix: broaden FIND_DRUG_SQL to contains.")
        print()
        print(f"  {'#':<5} {'CSV brand':<30} {'DB match':<35} {'drug_id':<12} {'matched via'}")
        print(f"  {'-'*5} {'-'*30} {'-'*35} {'-'*12} {'-'*20}")
        for r in found_with_contains:
            print(f"  {r['num']:<5} {r['csv_brand']:<30} {(r['db_brand_name'] or '')[:33]:<35} {str(r['drug_id_1mg'] or ''):<12} {r['found_via']}")
        print()

    # ── Group B: brand not seeded, salt exists ──────────────────────────────
    if brand_missing_but_salt:
        print("─" * 100)
        print(f"GROUP B — Brand not in indian_brand, but SALT exists via other brands ({len(brand_missing_but_salt)} drugs)")
        print("  The brand name isn't in indian_brand at all, but the active ingredient")
        print("  is there under different brand entries. These need new rows in indian_brand.")
        print()
        print(f"  {'#':<5} {'CSV brand':<28} {'Composition':<35} {'Alt brands in DB (same salt)'}")
        print(f"  {'-'*5} {'-'*28} {'-'*35} {'-'*50}")
        for r in brand_missing_but_salt:
            alts = " | ".join(f"{a['brand']} (id:{a['drug_id']})" for a in r["salt_alternatives"][:2])
            print(f"  {r['num']:<5} {r['csv_brand']:<28} {r['composition'][:33]:<35} {alts}")
        print()

    # ── Group C: truly missing ──────────────────────────────────────────────
    if truly_missing:
        print("─" * 100)
        print(f"GROUP C — Drug completely absent from our DB ({len(truly_missing)} drugs)")
        print("  Neither brand name nor salt composition found in indian_brand.")
        print()
        print(f"  {'#':<5} {'CSV brand':<30} {'Composition'}")
        print(f"  {'-'*5} {'-'*30} {'-'*60}")
        for r in truly_missing:
            print(f"  {r['num']:<5} {r['csv_brand']:<30} {r['composition']}")
        print()

    # ── Write diagnosis CSV ─────────────────────────────────────────────────
    out_path = Path(__file__).parent / "missing_brand_diagnosis.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "num", "csv_brand", "composition", "diagnosis",
            "found_via", "drug_id_1mg", "db_brand_name", "db_salt",
            "match_combination", "salt_alternatives_count",
            "salt_alt_1_brand", "salt_alt_1_drug_id",
            "salt_alt_2_brand", "salt_alt_2_drug_id",
        ])
        writer.writeheader()
        for r in results:
            alts = r["salt_alternatives"]
            writer.writerow({
                "num": r["num"],
                "csv_brand": r["csv_brand"],
                "composition": r["composition"],
                "diagnosis": r["diagnosis"],
                "found_via": r["found_via"] or "",
                "drug_id_1mg": r["drug_id_1mg"] or "",
                "db_brand_name": r["db_brand_name"] or "",
                "db_salt": r["db_salt"] or "",
                "match_combination": r["match_combination"] or "",
                "salt_alternatives_count": len(alts),
                "salt_alt_1_brand": alts[0]["brand"] if len(alts) > 0 else "",
                "salt_alt_1_drug_id": alts[0]["drug_id"] if len(alts) > 0 else "",
                "salt_alt_2_brand": alts[1]["brand"] if len(alts) > 1 else "",
                "salt_alt_2_drug_id": alts[1]["drug_id"] if len(alts) > 1 else "",
            })
    print(f"\nFull diagnosis written to: {out_path}")


if __name__ == "__main__":
    csv_path = str(Path(__file__).parent / "top_500_india_drugs.csv")
    if len(sys.argv) > 1:
        csv_path = sys.argv[1]
    asyncio.run(main(csv_path))
