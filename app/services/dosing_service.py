import time

from fastapi import HTTPException
from redis.asyncio import Redis

import asyncpg

from app.cache.redis import get_cached, set_cached
from app.config import settings
from app.repositories import dosing_repo
from app.schemas.response import DosingResponse, DosingRow
from app.utils.age_mapper import age_to_groups, age_to_primary_group
from app.utils.frequency_mapper import resolve_frequency
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def get_dosing(
    drug_id_1mg: str,
    age: int,
    pool: asyncpg.Pool,
    redis: Redis,
) -> DosingResponse:
    age_groups = age_to_groups(age)
    primary_group = age_to_primary_group(age)
    cache_key = f"dosing:{drug_id_1mg}:{primary_group}"

    cached_data = await get_cached(redis, cache_key)
    if cached_data is not None:
        logger.info("cache HIT", cache_key=cache_key)
        cached_data.pop("cached", None)
        cached_data.pop("query_time_ms", None)
        return DosingResponse(**cached_data, cached=True, query_time_ms=0.0)

    logger.info("cache MISS", cache_key=cache_key)

    t0 = time.perf_counter()
    try:
        rows = await dosing_repo.fetch_dosing(pool, drug_id_1mg, age_groups)
    except asyncpg.PostgresError:
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "Database error"})

    elapsed_ms = (time.perf_counter() - t0) * 1000

    if not rows:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"No dosing data found for drug_id_1mg={drug_id_1mg}, age={age}"},
        )

    first = rows[0]
    dosing_rows = [
        DosingRow(
            frequency=r["frequency"],
            frequency_meaning=resolve_frequency(r["frequency"]),
            route=r["route"],
            dose_amount=r["dose_amount"],
            dose_unit=r["dose_unit"],
            duration=r["duration"],
            indication=r["indication"],
            instructions=r["instructions"],
        )
        for r in rows
    ]

    response = DosingResponse(
        drug_id_1mg=drug_id_1mg,
        formulation_id=str(first["formulation_id"]),
        brand_name=first["brand_name"] or "",
        salt_composition=first["salt_composition"] or "",
        generic_name=first["generic_name"] or "",
        age_group=primary_group,
        dosing=dosing_rows,
        cached=False,
        query_time_ms=round(elapsed_ms, 2),
    )

    await set_cached(redis, cache_key, response.model_dump(), settings.CACHE_TTL_SECONDS)
    return response
