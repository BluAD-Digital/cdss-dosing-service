#!/usr/bin/env python3
"""
Indian-brand age-coverage: NEONATE + INFANT  (combined)

Fetches every drug_id_1mg from drugdb.indian_brand and checks both
neonate and infant age groups in a single pass for each drug.

Age-group SQL lists used:
  neonate → ["neonate"]
  infant  → ["infant", "neonate"]

Run standalone : python tests/indian_brand_coverage/test_neonate_infant.py
Run all 4      : python tests/indian_brand_coverage/run_all.py
"""
import asyncio
import sys
from pathlib import Path

# Make _common importable whether run as a script or imported by run_all.py
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _common import run_coverage  # noqa: E402

LABEL = "neonate_infant"

AGE_GROUP_MAP = {
    "neonate": ["neonate"],
    "infant":  ["infant", "neonate"],
}


async def main() -> dict:
    return await run_coverage(label=LABEL, age_group_map=AGE_GROUP_MAP)


if __name__ == "__main__":
    asyncio.run(main())
