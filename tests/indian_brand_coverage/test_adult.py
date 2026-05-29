#!/usr/bin/env python3
"""
Indian-brand age-coverage: ADULT

Fetches every drug_id_1mg from drugdb.indian_brand and checks the
adult age group (age_groups = ["adult", "any"]).

Run standalone : python tests/indian_brand_coverage/test_adult.py
Run all 4      : python tests/indian_brand_coverage/run_all.py
"""
import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _common import run_coverage  # noqa: E402

LABEL = "adult"

AGE_GROUP_MAP = {
    "adult": ["adult", "any"],
}


async def main() -> dict:
    return await run_coverage(label=LABEL, age_group_map=AGE_GROUP_MAP)


if __name__ == "__main__":
    asyncio.run(main())
