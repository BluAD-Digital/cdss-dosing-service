#!/usr/bin/env python3
"""
Map missing Indian brands into drugdb.indian_brand.

Uses bulk SQL (UNNEST) to avoid per-row round trips.
Finds the correct rxcui by salt_composition search, then INSERTs
new rows with synthetic drug_id_1mg values (starting at 9000001).

Usage:
  python3 tests/map_missing_brands.py --dry-run
  python3 tests/map_missing_brands.py
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

SYNTHETIC_ID_START = 9_000_001

# Salt keyword overrides for brands where substring search returns wrong drug
SALT_OVERRIDE = {
    "Chloroquine 250":      "Chloroquine",        # DB uses "Chloroquine" not "Chloroquine Phosphate"
    "Arcoxia 90":           "Etoricoxib",
    "Ranexa 500":           "Ranolazine",
    "Asacol 400":           "Mesalazine",          # DB uses British spelling "Mesalazine"
    "Urdox 300":            "Ursodeoxycholic",
    "Anoro Ellipta":        "Umeclidinium",
    "Dextromethorphan 10":  "Dextromethorphan",
    "Pregabalin 75":        "Pregabalin",
    "Mosid 5":              "Mosapride",
    "iPill":                "Levonorgestrel",
    "Limcee 500":           "Ascorbic Acid",
    "Lindane 1%":           "Gamma Benzene",
    "Exocin 0.3%":          "Ofloxacin",
    "Nitrocontin 2.6":      "Nitroglycerin",
    "Propylthiouracil 50":  "Propylthiouracil",    # genuinely absent; will be skipped
    "Cyclosporine 25":      "Ciclosporin",          # DB uses "Ciclosporin" spelling
    "Losartan 50":          "Losartan",
    "Losartan 25":          "Losartan",
    "Glipizide 5":          "Glipizide",
    "Glitazone MF 15/500":  "Pioglitazone",
    "Losec 20":             "Omeprazole",
    "Torsemide 10":         "Torasemide",           # DB uses British spelling "Torasemide"
    "Dytor 10":             "Torasemide",
    "Warfarin 5":           "Warfarin",
    "Fenofibrate 145":      "Fenofibrate",
    "Ezetimibe 10":         "Ezetimibe",
    "Omeprazole 20":        "Omeprazole",
    "Venlafaxine 75":       "Venlafaxine",
    "Hydrocortisone 1%":    "Hydrocortisone",
    "Hydrocortisone 100":   "Hydrocortisone",
    "Permethrin 5%":        "Permethrin",
    "Avapro 150":           "Irbesartan",
    "Dicyclomine 20":       "Dicyclomine",
    "Ventolin Inhaler":     "Salbutamol",           # first_ingredient fails on 'MDI' suffix
    "Asthalin 2.5":         "Salbutamol",
    "Sodium Chloride Eye":  "Sodium Chloride",      # prevent matching sodium valproate
    "Electrobion":          "Potassium Chloride",   # ORS — use KCl to avoid sodium valproate
    "Becosules":            "Cyanocobalamin",        # B-Complex; avoid Vitamin D3 false match
    "Zincovit":             "Zinc Sulphate",
}


# ─────────────────────────────────────────────────────────────────────────────
def strip_strength(name: str) -> str:
    return re.sub(r"\s+\d[\d./\s]*(?:mg|mcg|g|IU|%|ml)?$", "", name, flags=re.I).strip() or name


def first_ingredient(composition: str) -> str:
    """Return first useful keyword from composition string."""
    first = re.split(r"\s*\+\s*", composition)[0]
    first = re.sub(r"\(.*?\)", "", first)
    first = re.sub(r"\s*[\d./]+\s*(?:mg|mcg|g|IU|%|ml|IU/ml)?", "", first, flags=re.I).strip()
    words = [w for w in first.split() if len(w) > 3]
    return words[0] if words else (first.split()[0] if first.split() else "")


def is_herbal(composition: str) -> bool:
    return any(h in composition.lower() for h in
               ["herbal", "tulsi", "adulsa", "yashthimadhu", "capparis", "cichorium"])


def is_combination(composition: str) -> bool:
    return "+" in composition


def infer_route(composition: str, brand: str) -> str:
    text = (composition + " " + brand).lower()
    if any(x in text for x in ["injection", " inj", "iv ", "infusion"]):
        return "PARENTERAL"
    if any(x in text for x in ["eye drop", "ophthalmic", "ear drop", "eye"]):
        return "OPHTHALMIC"
    if any(x in text for x in ["inhaler", "turbuhaler", "rotacap", "respule", "mdi", "nebuliz"]):
        return "INHALATION"
    if any(x in text for x in ["cream", "ointment", "gel", "lotion", "topical", "dusting", "patch"]):
        return "TOPICAL"
    return "ORAL"


# ─────────────────────────────────────────────────────────────────────────────
async def bulk_check_existing(conn, brands: list[str]) -> set[str]:
    """Return set of brand names (lowercased) that already exist in indian_brand."""
    # Build candidates list: original, stripped, first-word
    candidates = {}  # candidate → original brand
    for b in brands:
        for variant in {b, strip_strength(b), b.split()[0]}:
            candidates[variant.lower()] = b

    candidate_list = list(candidates.keys())
    # One query: for each candidate check if ILIKE prefix matches
    rows = await conn.fetch("""
        SELECT LOWER(c.candidate) as candidate
        FROM UNNEST($1::text[]) AS c(candidate)
        WHERE EXISTS (
            SELECT 1 FROM drugdb.indian_brand
            WHERE brand_name ILIKE c.candidate || '%'
        )
    """, candidate_list)

    found_candidates = {r["candidate"] for r in rows}
    # Map back: a brand is "found" if ANY of its variants is in found_candidates
    found_brands = set()
    for variant_lower, original in candidates.items():
        if variant_lower in found_candidates:
            found_brands.add(original)
    return found_brands


async def find_rxcui_one(pool, brand: str, composition: str) -> tuple[str, tuple | None]:
    """Return (brand, (rxcui, source_brand)) or (brand, None)."""
    if brand in SALT_OVERRIDE:
        kw = SALT_OVERRIDE[brand]
    else:
        kw = first_ingredient(composition)

    if not kw or len(kw) < 3:
        return brand, None

    async with pool.acquire() as conn:
        for sql in [
            # Prefer non-drugbank
            "SELECT rxcui, brand_name FROM drugdb.indian_brand WHERE salt_composition ILIKE '%' || $1 || '%' AND rxcui IS NOT NULL AND match_combination NOT IN ('drugbank','us_unapproved') ORDER BY drug_id_1mg LIMIT 1",
            # Fall back to any
            "SELECT rxcui, brand_name FROM drugdb.indian_brand WHERE salt_composition ILIKE '%' || $1 || '%' AND rxcui IS NOT NULL ORDER BY drug_id_1mg LIMIT 1",
        ]:
            row = await conn.fetchrow(sql, kw)
            if row and row["rxcui"]:
                return brand, (row["rxcui"], row["brand_name"])
    return brand, None


async def bulk_find_rxcui(pool, entries: list[dict]) -> dict[str, tuple]:
    """Run all rxcui lookups concurrently. Returns dict: brand_name → (rxcui, source_brand)."""
    tasks = [find_rxcui_one(pool, e["brand_name"], e["composition"]) for e in entries]
    results = await asyncio.gather(*tasks)
    return {brand: val for brand, val in results if val is not None}


async def main(csv_path: str, dry_run: bool) -> None:
    with open(csv_path, newline="", encoding="utf-8") as f:
        all_drugs = list(csv.DictReader(f))

    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=6, command_timeout=60)

    # ── Step 1: bulk check which brands exist ────────────────────────────────
    print("Step 1: bulk-checking existing brands...")
    all_brand_names = [row["Brand Name (India)"] for row in all_drugs]
    async with pool.acquire() as conn:
        found_brands = await bulk_check_existing(conn, all_brand_names)

    missing_rows = [r for r in all_drugs if r["Brand Name (India)"] not in found_brands]
    print(f"  → {len(found_brands)} already in DB, {len(missing_rows)} missing\n")

    # ── Step 2: separate herbal from mappable ────────────────────────────────
    herbal = [r for r in missing_rows if is_herbal(r["Salt Composition"])]
    mappable = [r for r in missing_rows if not is_herbal(r["Salt Composition"])]
    print(f"Step 2: {len(mappable)} mappable, {len(herbal)} herbal (will skip)\n")

    # ── Step 3: bulk rxcui lookup ────────────────────────────────────────────
    print("Step 3: bulk rxcui lookup...")
    entries = [{"brand_name": r["Brand Name (India)"], "composition": r["Salt Composition"]} for r in mappable]
    rxcui_map = await bulk_find_rxcui(pool, entries)
    print(f"  → rxcui resolved for {len(rxcui_map)}/{len(entries)} brands\n")

    # ── Step 4: build insert list ────────────────────────────────────────────
    to_insert = []
    no_rxcui = []
    for r in mappable:
        brand = r["Brand Name (India)"]
        comp = r["Salt Composition"]
        if brand in rxcui_map:
            rxcui, source_brand = rxcui_map[brand]
            to_insert.append({
                "num": r["#"],
                "brand_name": brand,
                "composition": comp,
                "rxcui": rxcui,
                "source_brand": source_brand,
                "is_combo": is_combination(comp),
                "route": infer_route(comp, brand),
                "match_combo": (
                    "generic_name"
                    if brand.split()[0].lower() in comp.lower()
                    else "manual_alias"
                ),
            })
        else:
            no_rxcui.append(r)

    # ── Step 5: get starting synthetic ID ────────────────────────────────────
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COALESCE(MAX(drug_id_1mg::bigint), 0) as max_id
            FROM drugdb.indian_brand WHERE drug_id_1mg ~ '^[0-9]+$'
        """)
    next_id = max(int(row["max_id"]) + 1, SYNTHETIC_ID_START)

    # ── Step 6: show plan / execute ──────────────────────────────────────────
    print(f"{'DRY RUN — ' if dry_run else ''}INSERT PLAN: {len(to_insert)} rows (starting drug_id={next_id})\n")
    print(f"  {'#':<5} {'drug_id':<12} {'Brand Name':<34} {'rxcui':<28} via source brand")
    print(f"  {'-'*5} {'-'*12} {'-'*34} {'-'*28} {'-'*35}")

    insert_rows = []
    for entry in to_insert:
        drug_id = str(next_id)
        next_id += 1
        print(
            f"  {entry['num']:<5} {drug_id:<12} {entry['brand_name']:<34} "
            f"{str(entry['rxcui'])[:26]:<28} {(entry['source_brand'] or '')[:35]}"
        )
        insert_rows.append((
            drug_id, entry["brand_name"], entry["composition"],
            entry["rxcui"], entry["match_combo"],
            entry["is_combo"], entry["route"],
        ))

    inserted = 0
    if not dry_run and insert_rows:
        async with pool.acquire() as conn:
            async with conn.transaction():
                for args in insert_rows:
                    await conn.execute("""
                        INSERT INTO drugdb.indian_brand (
                            drug_id_1mg, brand_name, salt_composition,
                            rxcui, match_combination, source,
                            is_combination, prescription_required, cdsco_approval, route
                        )
                        VALUES ($1,$2,$3,$4,$5,'manual_mapping',$6,true,true,$7)
                        ON CONFLICT (drug_id_1mg) DO NOTHING
                    """, *args)
                    inserted += 1

    await pool.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print(f"{'DRY RUN' if dry_run else 'DONE'}")
    print(f"  {'Would insert' if dry_run else 'Inserted'}  : {len(to_insert)}")
    print(f"  Herbal skipped: {len(herbal)}")
    print(f"  No rxcui found: {len(no_rxcui)}")
    print("=" * 80)

    if no_rxcui:
        print(f"\nCannot map (no rxcui resolvable):")
        for r in no_rxcui:
            print(f"  #{r['#']:<5} {r['Brand Name (India)']:<35} {r['Salt Composition'][:50]}")

    if herbal:
        print(f"\nHerbal (skipped):")
        for r in herbal:
            print(f"  #{r['#']:<5} {r['Brand Name (India)']}")

    if dry_run and to_insert:
        print("\nRun without --dry-run to execute all inserts.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--csv", default=str(Path(__file__).parent / "top_500_india_drugs.csv"))
    args = parser.parse_args()
    asyncio.run(main(args.csv, args.dry_run))
