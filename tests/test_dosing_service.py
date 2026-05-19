import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.response import DosingResponse


def _make_row(**overrides):
    defaults = {
        "brand_name": "TestBrand",
        "salt_composition": "Paracetamol 500mg",
        "generic_name": "Paracetamol",
        "frequency": "twice daily",
        "route": "oral",
        "dose_amount": "500mg",
        "dose_unit": "mg",
        "duration": "5 days",
        "indication": "pain",
        "instructions": None,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda k: defaults[k])
    return row


@pytest.mark.asyncio
async def test_cache_miss_calls_repo():
    pool = AsyncMock()
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()

    rows = [_make_row()]

    with patch("app.services.dosing_service.dosing_repo.fetch_dosing", new=AsyncMock(return_value=rows)) as mock_repo:
        from app.services.dosing_service import get_dosing
        result = await get_dosing("457491", 35, pool, redis)

    mock_repo.assert_awaited_once()
    assert isinstance(result, DosingResponse)
    assert result.cached is False


@pytest.mark.asyncio
async def test_cache_hit_skips_repo():
    pool = AsyncMock()
    redis = AsyncMock()

    cached_payload = {
        "drug_id_1mg": "457491",
        "brand_name": "TestBrand",
        "salt_composition": "Paracetamol 500mg",
        "generic_name": "Paracetamol",
        "age_group": "adult",
        "dosing": [],
        "cached": False,
        "query_time_ms": 12.5,
    }
    redis.get = AsyncMock(return_value=json.dumps(cached_payload))

    with patch("app.services.dosing_service.dosing_repo.fetch_dosing", new=AsyncMock()) as mock_repo:
        from app.services.dosing_service import get_dosing
        result = await get_dosing("457491", 35, pool, redis)

    mock_repo.assert_not_awaited()
    assert result.cached is True
    assert result.query_time_ms == 0.0


@pytest.mark.asyncio
async def test_empty_rows_raises_404():
    from fastapi import HTTPException

    pool = AsyncMock()
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)

    with patch("app.services.dosing_service.dosing_repo.fetch_dosing", new=AsyncMock(return_value=[])):
        from app.services.dosing_service import get_dosing
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("999999", 35, pool, redis)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_response_structure():
    pool = AsyncMock()
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()

    rows = [_make_row()]

    with patch("app.services.dosing_service.dosing_repo.fetch_dosing", new=AsyncMock(return_value=rows)):
        from app.services.dosing_service import get_dosing
        result = await get_dosing("457491", 35, pool, redis)

    assert result.drug_id_1mg == "457491"
    assert result.brand_name == "TestBrand"
    assert result.age_group == "adult"
    assert len(result.dosing) == 1
    assert result.dosing[0].frequency == "twice daily"
    assert result.query_time_ms >= 0
