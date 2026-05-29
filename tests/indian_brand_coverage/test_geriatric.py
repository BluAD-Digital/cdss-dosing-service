#!/usr/bin/env python3
"""
Indian-brand age-coverage: GERIATRIC

Fetches every drug_id_1mg from drugdb.indian_brand and checks the
geriatric age group (age_groups = ["geriatric", "adult", "any"]).

Run standalone : python tests/indian_brand_coverage/test_geriatric.py
Run all 4      : python tests/indian_brand_coverage/run_all.py
"""
import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _common import run_coverage  # noqa: E402

LABEL = "geriatric"

AGE_GROUP_MAP = {
    "geriatric": 70,  # age=70 → service resolves to ["geriatric", "adult", "any"]
}


async def main() -> dict:
    return await run_coverage(label=LABEL, age_group_map=AGE_GROUP_MAP)


if __name__ == "__main__":
    asyncio.run(main())
