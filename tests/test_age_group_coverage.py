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
from unittest.mock import AsyncMock, MagicMock, call, patch

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
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda k: defaults[k])
    return row


def _patch_repo(
    *,
    exists: bool = True,
    primary_rows=None,
    fallback_rows=None,
):
    """Context manager that patches all three repo calls at once."""
    primary_rows = primary_rows if primary_rows is not None else []
    fallback_rows = fallback_rows if fallback_rows is not None else []

    return (
        patch("app.services.dosing_service.dosing_repo.drug_exists",
              new=AsyncMock(return_value=exists)),
        patch("app.services.dosing_service.dosing_repo.fetch_dosing",
              new=AsyncMock(return_value=primary_rows)),
        patch("app.services.dosing_service.dosing_repo.fetch_dosing_fallback",
              new=AsyncMock(return_value=fallback_rows)),
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
    (2,  ["pediatric", "any"],          "pediatric"),
    (17, ["pediatric", "any"],          "pediatric"),
    (18, ["adult", "any"],              "adult"),
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
# 3. Service passes correct age_groups to primary repo call
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

    p_exists, p_primary, p_fallback = _patch_repo(primary_rows=rows)
    with p_exists, p_primary as mock_primary, p_fallback:
        await get_dosing("457491", age, pool, redis)

    _, call_kwargs = mock_primary.call_args
    passed_groups = mock_primary.call_args[0][2]   # positional: pool, drug_id, age_groups
    assert passed_groups == expected_age_groups


# ---------------------------------------------------------------------------
# 4. Fallback triggered when primary returns empty (drug exists)
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

    p_exists, p_primary, p_fallback = _patch_repo(
        exists=True,
        primary_rows=[],
        fallback_rows=fallback_rows,
    )
    with p_exists, p_primary as mock_primary, p_fallback as mock_fallback:
        result = await get_dosing("457491", age, pool, redis)

    mock_primary.assert_awaited_once()
    mock_fallback.assert_awaited_once()

    # fallback also receives the same age_groups
    passed_groups = mock_fallback.call_args[0][2]
    assert passed_groups == expected_age_groups

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

    p_exists, p_primary, p_fallback = _patch_repo(
        primary_rows=[], fallback_rows=fallback_rows
    )
    with p_exists, p_primary, p_fallback as mock_fallback:
        await get_dosing("457491", age, pool, redis)

    passed_groups = mock_fallback.call_args[0][2]
    assert passed_groups == expected_age_groups


# ---------------------------------------------------------------------------
# 6. Fallback NOT called when primary succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_fallback_not_called_when_primary_succeeds(age):
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()
    rows = [_make_db_row()]

    p_exists, p_primary, p_fallback = _patch_repo(primary_rows=rows)
    with p_exists, p_primary, p_fallback as mock_fallback:
        await get_dosing("457491", age, pool, redis)

    mock_fallback.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. Fallback called when drug does NOT exist (drug_exists=False)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_fallback_called_when_drug_not_found_in_primary_table(age):
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()
    fallback_rows = [_make_db_row()]

    p_exists, p_primary, p_fallback = _patch_repo(
        exists=False,
        primary_rows=[],
        fallback_rows=fallback_rows,
    )
    with p_exists, p_primary as mock_primary, p_fallback as mock_fallback:
        await get_dosing("000000", age, pool, redis)

    # primary fetch skipped entirely when drug doesn't exist
    mock_primary.assert_not_awaited()
    mock_fallback.assert_awaited_once()


# ---------------------------------------------------------------------------
# 8. 404 raised when BOTH primary and fallback return nothing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_404_when_both_queries_empty(age):
    from fastapi import HTTPException
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()

    p_exists, p_primary, p_fallback = _patch_repo(primary_rows=[], fallback_rows=[])
    with p_exists, p_primary, p_fallback:
        with pytest.raises(HTTPException) as exc_info:
            await get_dosing("457491", age, pool, redis)

    assert exc_info.value.status_code == 404
    detail = exc_info.value.detail
    assert detail["error"] == "not_found"


# ---------------------------------------------------------------------------
# 9. Response age_group field reflects primary group for each age
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

    p_exists, p_primary, p_fallback = _patch_repo(primary_rows=rows)
    with p_exists, p_primary, p_fallback:
        result = await get_dosing("457491", age, pool, redis)

    assert result.age_group == expected_primary


# ---------------------------------------------------------------------------
# 10. Both primary and fallback paths return a valid DosingResponse for each group
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("age", [0, 1, 10, 35, 70])
async def test_primary_path_returns_valid_response(age):
    from app.schemas.response import DosingResponse
    from app.services.dosing_service import get_dosing

    pool, redis = _make_async_deps()
    rows = [_make_db_row()]

    p_exists, p_primary, p_fallback = _patch_repo(primary_rows=rows)
    with p_exists, p_primary, p_fallback:
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

    p_exists, p_primary, p_fallback = _patch_repo(
        primary_rows=[], fallback_rows=fallback_rows
    )
    with p_exists, p_primary, p_fallback:
        result = await get_dosing("457491", age, pool, redis)

    assert isinstance(result, DosingResponse)
    assert len(result.dosing) == 1


# ---------------------------------------------------------------------------
# 11. GAP documentation: dose_basis='fixed' blocks weight-based pediatric dosing
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
