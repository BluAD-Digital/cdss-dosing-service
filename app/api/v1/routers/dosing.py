import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis

from app.api.deps import get_cache, get_db
from app.schemas.request import DosingRequest
from app.schemas.response import DosingResponse
from app.services import dosing_service
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["dosing"])


@router.post("/dosing", response_model=DosingResponse)
async def get_dosing(
    body: DosingRequest,
    pool: asyncpg.Pool = Depends(get_db),
    redis: Redis = Depends(get_cache),
) -> DosingResponse:
    logger.info("dosing request", drug_id_1mg=body.drug_id_1mg, age=body.age)

    try:
        result = await dosing_service.get_dosing(
            drug_id_1mg=body.drug_id_1mg,
            age=body.age,
            pool=pool,
            redis=redis,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("unexpected error in dosing endpoint", error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "An unexpected error occurred"},
        )

    logger.info(
        "dosing response",
        drug_id_1mg=body.drug_id_1mg,
        age_group=result.age_group,
        dosing_rows=len(result.dosing),
        cached=result.cached,
        query_time_ms=result.query_time_ms,
    )
    return result
