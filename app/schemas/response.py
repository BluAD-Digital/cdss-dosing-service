from pydantic import BaseModel


class DosingRow(BaseModel):
    frequency: str | None
    frequency_meaning: str | None
    route: str | None
    dose_amount: str | None
    dose_unit: str | None
    duration: str | None
    indication: str | None
    instructions: str | None


class DosingResponse(BaseModel):
    drug_id_1mg: str
    formulation_id: str
    brand_name: str
    salt_composition: str
    generic_name: str
    age_group: str
    dosing: list[DosingRow]
    cached: bool = False
    query_time_ms: float
