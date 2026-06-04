from pathlib import Path

import asyncpg

from app.utils.logger import get_logger

logger = get_logger(__name__)

_SQL = (Path(__file__).parent.parent.parent / "queries" / "dosing.sql").read_text()
_FALLBACK_SQL = (Path(__file__).parent.parent.parent / "queries" / "dosing_fallback.sql").read_text()


async def fetch_dosing_with_fallback(
    pool: asyncpg.Pool,
    drug_id_1mg: str,
    age_groups: list[str],
) -> tuple[list[asyncpg.Record], str, bool]:
    """Return (rows, source, is_partial_match) using a single connection for both queries."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SQL, drug_id_1mg, age_groups)
            if rows:
                logger.debug("fetch_dosing primary hit", drug_id_1mg=drug_id_1mg, row_count=len(rows))
                return rows, "primary", False

            logger.info("primary miss, trying fallback", drug_id_1mg=drug_id_1mg)
            rows = await conn.fetch(_FALLBACK_SQL, drug_id_1mg, age_groups)
            if rows:
                is_partial = bool(rows[0]["is_partial_match"])
                logger.debug("fetch_dosing fallback hit", drug_id_1mg=drug_id_1mg, row_count=len(rows))
                return rows, "fallback", is_partial

        return [], "none", False
    except asyncpg.PostgresError as exc:
        logger.error("fetch_dosing DB error", drug_id_1mg=drug_id_1mg, error=str(exc), exc_info=True)
        raise
