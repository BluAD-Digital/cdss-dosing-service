from typing import AsyncGenerator

import asyncpg

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def create_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.POOL_MIN_SIZE,
        max_size=settings.POOL_MAX_SIZE,
        command_timeout=settings.POOL_COMMAND_TIMEOUT,
    )
    logger.info("asyncpg pool created", min_size=settings.POOL_MIN_SIZE, max_size=settings.POOL_MAX_SIZE)
    return pool


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
    logger.info("asyncpg pool closed")


async def get_pool(request) -> AsyncGenerator[asyncpg.Pool, None]:  # noqa: ANN001
    yield request.app.state.pool
