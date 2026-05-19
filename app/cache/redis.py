import json
from typing import AsyncGenerator

from redis.asyncio import Redis, from_url

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def create_redis() -> Redis:
    client = from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
    logger.info("Redis client created", url=settings.REDIS_URL)
    return client


async def close_redis(redis: Redis) -> None:
    await redis.aclose()
    logger.info("Redis connection closed")


async def get_redis(request) -> AsyncGenerator[Redis, None]:  # noqa: ANN001
    yield request.app.state.redis


async def get_cached(redis: Redis, key: str) -> dict | None:
    try:
        raw = await redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Redis get failed", key=key, error=str(exc))
        return None


async def set_cached(redis: Redis, key: str, value: dict, ttl: int) -> None:
    try:
        await redis.set(key, json.dumps(value), ex=ttl)
    except Exception as exc:
        logger.warning("Redis set failed", key=key, error=str(exc))
