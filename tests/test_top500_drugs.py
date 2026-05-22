#!/usr/bin/env python3
"""
Diagnostic test for Top 500 Indian Brand Drugs against the dosing DB.

For each drug in the CSV, it:
  1. Looks up drug_id_1mg in drugdb.indian_brand by brand name (ILIKE)
  2. Checks if the primary dosing path returns data  (match_combination filter + rxcui join)
  3. If not, checks if the fallback dosing path returns data (indian_brand_ingredient bridge)
  4. Classifies result as: primary | fallback | 404_not_found | not_in_db

Usage:
  python tests/test_top500_drugs.py
  python tests/test_top500_drugs.py --age 10          # test pediatric
  python tests/test_top500_drugs.py --csv tests/Top_500_Indian_Brand_Drugs.csv --age 30
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

DB_URL = os.environ["DATABASE_URL"]

# ──────────────────────────────────────────────────────────────────────────────
# SQL
# ──────────────────────────────────────────────────────────────────────────────

FIND_DRUG_SQL = """
SELECT DISTINCT ON (drug_id_1mg)
    drug_id_1mg,
    brand_name,
    salt_composition,
    (
        SELECT STRING_AGG(DISTINCT match_combination, ', ' ORDER BY match_combination)
        FROM drugdb.indian_brand ib2
        WHERE ib2.drug_id_1mg = ib.drug_id_1mg
    ) AS match_combinations
FROM drugdb.indian_brand ib
WHERE LOWER(brand_name) ILIKE LOWER($1) || '%'
ORDER BY drug_id_1mg
LIMIT 3
"""

PRIMARY_CHECK_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM drugdb.indian_brand ib
    JOIN drugdb.drug d       ON d.rxcui = ANY(ib.rxcui)
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

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def age_to_groups(age: int) -> list[str]:
    if age < 1:
        return ["neonate"]
    if age < 2:
        return ["infant", "neonate"]
    if age < 18:
        return ["pediatric", "any"]
    if age < 65:
        return ["adult", "any"]
    return ["geriatric", "adult", "any"]


def clean_brand_name(raw: str) -> str:
    """Strip parenthetical suffixes like '(Modalert)' and trailing dosage tokens."""
    name = re.sub(r"\s*\(.*?\)", "", raw).strip()      # remove (...)
    name = re.sub(r"\s+\d[\d./\s]*(?:mg|mcg|g|IU|%|ml)?$", "", name, flags=re.I).strip()
    return name or raw


def setup_logging(log_path: str) -> logging.Logger:
    logger = logging.getLogger("top500")
    logger.setLevel(logging.DEBUG)

    fmt_console = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt_console)
    logger.addHandler(ch)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(message)s"))  # raw JSON lines
    logger.addHandler(fh)

    return logger


ICONS = {
    "primary":       "✓",
    "fallback":      "~",
    "404_not_found": "✗",
    "not_in_db":     "?",
}

# ──────────────────────────────────────────────────────────────────────────────
# Core logic
# ──────────────────────────────────────────────────────────────────────────────

def extract_paren_name(raw: str) -> str | None:
    """Extract brand name from parentheses, e.g. 'Fluconazole (Zocon)' → 'Zocon'."""
    m = re.search(r"\(([^)]+)\)", raw)
    if m:
        candidate = m.group(1).strip()
        # Skip if it looks like a dose/strength, not a brand name
        if not re.search(r"\d", candidate):
            return candidate
    return None


async def find_drug(conn, brand_name: str):
    # 1. Full CSV name with trailing wildcard: 'Dolo 650%' → 'Dolo 650 Tablet'
    rows = await conn.fetch(FIND_DRUG_SQL, brand_name)
    if rows:
        return rows

    # 2. Brand name stripped of parenthetical: 'Fluconazole (Zocon)' → 'Fluconazole%'
    cleaned = clean_brand_name(brand_name)
    if cleaned != brand_name:
        rows = await conn.fetch(FIND_DRUG_SQL, cleaned)
        if rows:
            return rows

    # 3. Text inside parentheses as brand name: 'Fluconazole (Zocon)' → 'Zocon%'
    paren = extract_paren_name(brand_name)
    if paren:
        rows = await conn.fetch(FIND_DRUG_SQL, paren)
        if rows:
            return rows

    # 4. First word only: 'Voveran SR' already cleaned, try 'Voveran%'
    first_word = brand_name.split()[0]
    if first_word not in (brand_name, cleaned):
        rows = await conn.fetch(FIND_DRUG_SQL, first_word)

    return rows


async def classify_drug(conn, drug_id: str, age_groups: list[str]) -> str:
    row = await conn.fetchrow(PRIMARY_CHECK_SQL, drug_id, age_groups)
    if row["has_dosing"]:
        return "primary"
    row = await conn.fetchrow(FALLBACK_CHECK_SQL, drug_id, age_groups)
    if row["has_dosing"]:
        return "fallback"
    return "404_not_found"


async def test_one(conn, csv_row: dict, age_groups: list[str]) -> dict:
    brand_name = csv_row["Brand Name"]
    db_rows = await find_drug(conn, brand_name)

    base = {
        "rank":            csv_row["Rank"],
        "brand_name":      brand_name,
        "composition":     csv_row["Composition / Salt(s)"],
        "category":        csv_row["Therapeutic Category"],
        "rx_otc":          csv_row["Rx/OTC"],
        "age_groups":      age_groups,
        "drug_id_1mg":     None,
        "db_brand_name":   None,
        "db_salt_composition": None,
        "match_combinations":  None,
        "result":          "not_in_db",
    }

    if not db_rows:
        return base

    match = db_rows[0]
    drug_id = match["drug_id_1mg"]
    status = await classify_drug(conn, drug_id, age_groups)

    return {
        **base,
        "drug_id_1mg":         drug_id,
        "db_brand_name":       match["brand_name"],
        "db_salt_composition": match["salt_composition"],
        "match_combinations":  match["match_combinations"],
        "result":              status,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def main(csv_path: str, age: int) -> None:
    age_groups = age_to_groups(age)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(__file__).parent / f"drug_test_age{age}_{ts}.log"

    logger = setup_logging(str(log_path))
    logger.info(f"Starting | csv={csv_path} | age={age} | age_groups={age_groups}")
    logger.info(f"Log file → {log_path}")
    logger.info("-" * 80)

    with open(csv_path, newline="", encoding="utf-8") as f:
        drugs = list(csv.DictReader(f))

    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=5, command_timeout=30)

    results = []
    counts: dict[str, int] = {
        "primary": 0, "fallback": 0, "404_not_found": 0, "not_in_db": 0,
    }

    async with pool.acquire() as conn:
        for i, row in enumerate(drugs, 1):
            result = await test_one(conn, row, age_groups)
            results.append(result)
            counts[result["result"]] += 1

            icon = ICONS[result["result"]]
            logger.info(
                f"[{i:3d}/500] {icon} {result['result']:<15} | "
                f"{row['Brand Name'][:28]:<28} | "
                f"drug_id={result['drug_id_1mg'] or 'N/A':<10} | "
                f"match_combo={result['match_combinations'] or '-'}"
            )
            logger.debug(json.dumps(result, ensure_ascii=False))

    await pool.close()

    total = len(drugs)
    logger.info("=" * 80)
    logger.info("SUMMARY")
    logger.info(f"  Total tested   : {total}")
    logger.info(f"  ✓ primary      : {counts['primary']:4d}  ({counts['primary']/total*100:.1f}%)")
    logger.info(f"  ~ fallback     : {counts['fallback']:4d}  ({counts['fallback']/total*100:.1f}%)")
    logger.info(f"  ✗ 404_not_found: {counts['404_not_found']:4d}  ({counts['404_not_found']/total*100:.1f}%)")
    logger.info(f"  ? not_in_db    : {counts['not_in_db']:4d}  ({counts['not_in_db']/total*100:.1f}%)")
    logger.info("=" * 80)

    problem_drugs = [r for r in results if r["result"] in ("404_not_found", "not_in_db")]
    if problem_drugs:
        logger.info(f"\nDrugs with NO dosing data ({len(problem_drugs)}):")
        logger.info(f"  {'Rank':<5} {'Brand Name':<30} {'Result':<15} {'drug_id':<12} {'match_combinations'}")
        logger.info(f"  {'-'*5} {'-'*30} {'-'*15} {'-'*12} {'-'*30}")
        for r in problem_drugs:
            logger.info(
                f"  {r['rank']:<5} {r['brand_name']:<30} {r['result']:<15} "
                f"{str(r['drug_id_1mg'] or 'N/A'):<12} {r['match_combinations'] or '-'}"
            )

    logger.info(f"\nFull JSON log: {log_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test top 500 Indian brand drugs against dosing DB")
    parser.add_argument(
        "--csv",
        default=str(Path(__file__).parent / "Top_500_Indian_Brand_Drugs.csv"),
        help="Path to CSV file (default: tests/Top_500_Indian_Brand_Drugs.csv)",
    )
    parser.add_argument(
        "--age", type=int, default=30,
        help="Patient age to test age-group filtering (default: 30 = adult)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.csv, args.age))
