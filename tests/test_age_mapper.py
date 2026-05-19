from app.utils.age_mapper import age_to_groups, age_to_primary_group


def test_neonate():
    assert age_to_groups(0) == ["neonate"]


def test_infant():
    assert age_to_groups(1) == ["infant", "neonate"]


def test_pediatric():
    assert age_to_groups(10) == ["pediatric", "any"]


def test_adult():
    assert age_to_groups(35) == ["adult", "any"]


def test_geriatric():
    assert age_to_groups(70) == ["geriatric", "adult", "any"]


def test_primary_group_adult():
    assert age_to_primary_group(35) == "adult"


def test_primary_group_geriatric():
    assert age_to_primary_group(70) == "geriatric"
