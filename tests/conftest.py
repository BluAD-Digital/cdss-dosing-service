import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

# Set required env vars before any app module is imported.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("ENVIRONMENT", "development")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def mock_pool():
    # asyncpg pool.acquire() is a sync call returning an async context manager,
    # not a coroutine. Model it with MagicMock + AsyncMock __aenter__/__aexit__.
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(return_value=[])

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool.acquire = MagicMock(return_value=ctx)
    pool.close = AsyncMock()
    return pool


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.ping = AsyncMock(return_value=True)
    return redis


@pytest.fixture
def sample_dosing_request() -> dict:
    return {"drug_id_1mg": "457491", "age": 35}


@pytest.fixture
def sample_db_rows() -> list:
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda k: {
        "brand_name": "TestBrand",
        "salt_composition": "Paracetamol 500mg",
        "generic_name": "Paracetamol",
        "frequency": "twice daily",
        "route": "oral",
        "dose_amount": "500mg",
        "dose_unit": "mg",
        "duration": "5 days",
        "indication": "pain relief",
        "instructions": "take with food",
    }[k])
    return [row]


@pytest.fixture
def app_client(mock_pool, mock_redis):
    from app.main import app

    app.state.pool = mock_pool
    app.state.redis = mock_redis

    with patch("app.main.create_pool", new=AsyncMock(return_value=mock_pool)), \
         patch("app.main.create_redis", new=AsyncMock(return_value=mock_redis)):
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
