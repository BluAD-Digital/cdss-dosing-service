from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error: str
    message: str


class DosingRow(BaseModel):
    frequency: str | None
    frequency_meaning: str | None
    route: str | None
    dose_amount: str | None
    dose_unit: str | None
    duration: str | None
    indication: str | None
    instructions: str | None
    food_timing: str | None


class DosingResponse(BaseModel):
    drug_id_1mg: str
    formulation_id: str
    brand_name: str
    salt_composition: str
    generic_name: str
    age_group: str
    source: str = "primary"
    is_partial_match: bool = False
    dosing: list[DosingRow]
    cached: bool = False
    query_time_ms: float
