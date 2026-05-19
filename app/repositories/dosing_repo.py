from pathlib import Path

import asyncpg

from app.utils.logger import get_logger

logger = get_logger(__name__)

_SQL = (Path(__file__).parent.parent.parent / "queries" / "dosing.sql").read_text()


async def fetch_dosing(
    pool: asyncpg.Pool,
    drug_id_1mg: str,
    age_groups: list[str],
) -> list[asyncpg.Record]:
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL, drug_id_1mg, age_groups)
        logger.debug("fetch_dosing executed", drug_id_1mg=drug_id_1mg, age_groups=age_groups, row_count=len(rows))
        return rows
    except asyncpg.PostgresError as exc:
        logger.error("fetch_dosing DB error", drug_id_1mg=drug_id_1mg, error=str(exc), exc_info=True)
        raise
