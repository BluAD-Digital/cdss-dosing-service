"""
Comprehensive age-group coverage tests.

Goals:
  1. Verify age → group mapping at every boundary.
  2. Verify the service passes the correct age_groups list to the repo.
  3. Verify the fallback path is triggered (or skipped) correctly per age group.
  4. Verify 404 is raised when both primary and fallback return nothing.
  5. Flag known data gaps (neonate/infant have no 'any' catch-all group,
     and the SQL filters dose_basis='fixed' which excludes weight-based dosing).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.utils.age_mapper import age_to_groups, age_to_primary_group


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_row(**overrides):
    """Return a minimal asyncpg-Record-like mock with all keys dosing_service reads."""
    defaults = {
        "formulation_id": 1001,
        "brand_name": "TestBrand",
        "salt_composition": "Amoxicillin 250mg",
        "generic_name": "Amoxicillin",
        "frequency": "three times daily",
        "route": "oral",
        "dose_amount": "250",
        "dose_unit": "mg",
        "duration": "7 days",
        "indication": "infection",
        "instructions": None,
        "food_timing": None,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda k: defaults[k])
    return row


def _patch_repo(*, primary_rows=None, fallback_rows=None):
    """Patch fetch_dosing_with_fallback to return primary rows if non-empty, else fallback rows."""
    primary_rows = primary_rows if primary_rows is not None else []
    fallback_rows = fallback_rows if fallback_rows is not None else []

    if primary_rows:
        return_value = (primary_rows, "primary", False)
    elif fallback_rows:
        return_value = (fallback_rows, "fallback", False)
    else:
        return_value = ([], "none", False)

    return patch(
        "app.services.dosing_service.dosing_repo.fetch_dosing_with_fallback",
        new=AsyncMock(return_value=return_value),
    )


def _make_async_deps():
    pool = MagicMock()
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)   # always cache MISS
    redis.set = AsyncMock()
    return pool, redis


# ---------------------------------------------------------------------------
# 1. Age-group boundary mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("age,expected_groups,expected_primary", [
    (0,  ["neonate"],                   "neonate"),
    (1,  ["infant", "neonate"],         "infant"),
    (2,  ["pediatric", "any"],             "pediatric"),
    (17, ["adolescent", "adult", "any"],  "adolescent"),
    (18, ["adult", "any"],                "adult"),
    (64, ["adult", "any"],              "adult"),
    (65, ["geriatric", "adult", "any"], "geriatric"),
    (90, ["geriatric", "adult", "any"], "geriatric"),
])
def test_age_group_boundary_mapping(age, expected_groups, expected_primary):
    assert age_to_groups(age) == expected_groups
    assert age_to_primary_group(age) == expected_primary


# ---------------------------------------------------------------------------
# 2. Known gap: neonate and infant have no 'any' catch-all
#    → if the DB has no age_group='neonate'/'infant' rows they always get 404
# ---------------------------------------------------------------------------

def test_neonate_has_no_any_group():
    """
    GAP: neonate age_groups=["neonate"] — no 'any' fallback.
    A drug with only age_group='adult' or 'any' rows will return nothing.
    """
    assert "any" not in age_to_groups(0)


def test_infant_has_no_any_group():
    """
    GAP: infant age_groups=["infant", "neonate"] — no 'any' fallback.
    A drug with only age_group='adult' or 'any' rows will return nothing.
    """
    assert "any" not in age_to_groups(1)


def test_pediatric_has_any_group():
    """Pediatric includes 'any' so it can match generic adult dosing rows."""
    assert "any" in age_to_groups(10)


def test_adult_has_any_group():
    assert "any" in age_to_groups(35)


def test_geriatric_has_adult_and_any():
    groups = age_to_groups(70)
    assert "adult" in groups
    assert "any" in groups


# ---------------------------------------------------------------------------
# 3. Service passes correct age_groups to repo
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_age_groups", [
    (0,  ["neonate"]),
    (1,  ["infant", "neonate"]),
    (10, ["pediatric", "any"]),
    (35, ["adult", "any"]),
    (70, ["geriatric", "adult", "any"]),
])
async def test_service_passes_correct_age_groups_to_primary(age, expected_age_groups):
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()
    rows = [_make_db_row()]

    with _patch_repo(primary_rows=rows) as mock_repo:
        await get_dosing("457491", age, pool, redis)

    passed_groups = mock_repo.call_args[0][2]   # positional: pool, drug_id, age_groups
    assert passed_groups == expected_age_groups


# ---------------------------------------------------------------------------
# 4. Fallback triggered when primary returns empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_age_groups", [
    (0,  ["neonate"]),
    (1,  ["infant", "neonate"]),
    (10, ["pediatric", "any"]),
    (35, ["adult", "any"]),
    (70, ["geriatric", "adult", "any"]),
])
async def test_fallback_called_when_primary_empty(age, expected_age_groups):
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()
    fallback_rows = [_make_db_row()]

    with _patch_repo(primary_rows=[], fallback_rows=fallback_rows) as mock_repo:
        result = await get_dosing("457491", age, pool, redis)

    mock_repo.assert_awaited_once()
    assert result.source == "fallback"
    assert result.age_group == age_to_primary_group(age)


# ---------------------------------------------------------------------------
# 5. Fallback also receives correct age_groups
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_age_groups", [
    (0,  ["neonate"]),
    (1,  ["infant", "neonate"]),
    (10, ["pediatric", "any"]),
    (35, ["adult", "any"]),
    (70, ["geriatric", "adult", "any"]),
])
async def test_fallback_passes_correct_age_groups(age, expected_age_groups):
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()
    fallback_rows = [_make_db_row()]

    with _patch_repo(primary_rows=[], fallback_rows=fallback_rows) as mock_repo:
        await get_dosing("457491", age, pool, redis)

    passed_groups = mock_repo.call_args[0][2]
    assert passed_groups == expected_age_groups


# ---------------------------------------------------------------------------
# 6. Primary path returns correct source label
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_primary_path_source_label(age):
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()
    rows = [_make_db_row()]

    with _patch_repo(primary_rows=rows):
        result = await get_dosing("457491", age, pool, redis)

    assert result.source == "primary"


# ---------------------------------------------------------------------------
# 7. 404 raised when both primary and fallback return nothing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_404_when_both_queries_empty(age):
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()

    with _patch_repo(primary_rows=[], fallback_rows=[]):
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", age, pool, redis)

    assert exc_info.value.status_code == 404
    detail = exc_info.value.detail
    assert detail["error"] == "not_found"


# ---------------------------------------------------------------------------
# 8. Response age_group field reflects primary group for each age
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected_primary", [
    (0,  "neonate"),
    (1,  "infant"),
    (10, "pediatric"),
    (35, "adult"),
    (70, "geriatric"),
])
async def test_response_age_group_field(age, expected_primary):
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()
    rows = [_make_db_row()]

    with _patch_repo(primary_rows=rows):
        result = await get_dosing("457491", age, pool, redis)

    assert result.age_group == expected_primary


# ---------------------------------------------------------------------------
# 9. Both primary and fallback paths return a valid DosingResponse for each group
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_primary_path_returns_valid_response(age):
    from app.schemas.response import DosingResponse
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()
    rows = [_make_db_row()]

    with _patch_repo(primary_rows=rows):
        result = await get_dosing("457491", age, pool, redis)

    assert isinstance(result, DosingResponse)
    assert len(result.dosing) == 1
    assert result.cached is False


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_fallback_path_returns_valid_response(age):
    from app.schemas.response import DosingResponse
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()
    fallback_rows = [_make_db_row()]

    with _patch_repo(primary_rows=[], fallback_rows=fallback_rows):
        result = await get_dosing("457491", age, pool, redis)

    assert isinstance(result, DosingResponse)
    assert len(result.dosing) == 1


# ---------------------------------------------------------------------------
# 10. GAP documentation: dose_basis='fixed' blocks weight-based pediatric dosing
#     These are marker tests — they always pass but document the known SQL gap.
# ---------------------------------------------------------------------------

def test_gap_dose_basis_fixed_excludes_weight_based():
    """
    KNOWN GAP (dosing.sql:75, dosing_fallback.sql:93):
      WHERE dr.dose_basis = 'fixed'
    Pediatric (and infant/neonate) dosing is typically weight-based (mg/kg).
    That means dose_basis='weight' rows are silently excluded for ALL age groups,
    but the impact is worst for under-18 patients where weight-based dosing is standard.
    Fix: add dose_basis='weight' support or remove the filter and expose dose_basis
    in the response so the caller can handle it.
    """
    assert True, "Marker test — see docstring for the gap description"


def test_gap_administration_notes_pediatric_filter():
    """
    KNOWN GAP (dosing.sql:78-79, dosing_fallback.sql:96-97):
      AND (dr.administration_notes NOT ILIKE '%pediatric%' OR dr.administration_notes IS NULL)
    This EXCLUDES rows whose notes mention 'pediatric', which are exactly the rows
    most relevant to under-18 patients. The filter was likely meant to exclude adult
    rows with a note like 'not for pediatric use', but it's too broad and discards
    valid pediatric dosing rows as well.
    Fix: either remove this filter or flip it for pediatric age groups.
    """
    assert True, "Marker test — see docstring for the gap description"
