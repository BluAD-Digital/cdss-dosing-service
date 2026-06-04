def age_to_groups(age: int) -> list[str]:
    if age < 1:
        return ["neonate"]
    if age < 2:
        return ["infant", "neonate"]
    if age < 12:
        return ["pediatric", "any"]
    if age < 18:
        return ["adolescent", "adult", "any"]
    if age < 65:
        return ["adult", "any"]
    return ["geriatric", "adult", "any"]


def age_to_primary_group(age: int) -> str:
    return age_to_groups(age)[0]
