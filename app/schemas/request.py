from pydantic import BaseModel, Field


class DosingRequest(BaseModel):
    drug_id_1mg: str = Field(..., description="1mg catalog drug ID")
    age: int = Field(..., ge=0, le=120, description="Patient age in years")
