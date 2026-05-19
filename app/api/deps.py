from typing import AsyncGenerator

import asyncpg
from fastapi import Request
from redis.asyncio import Redis


async def get_db(request: Request) -> AsyncGenerator[asyncpg.Pool, None]:
    yield request.app.state.pool


async def get_cache(request: Request) -> AsyncGenerator[Redis, None]:
    yield request.app.state.redis
