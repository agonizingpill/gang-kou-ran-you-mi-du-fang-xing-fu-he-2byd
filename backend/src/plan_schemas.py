from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator


class CleanInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class PlanIn(CleanInput):
    code: str = Field(min_length=2, max_length=70)
    note: str = Field(default="", max_length=600)
    baseline_plan_id: int | None = Field(default=None, gt=0)
    references: dict[Literal["VLSFO", "MGO"], list[int]]

    @field_validator("references")
    @classmethod
    def has_both_grades(cls, v):
        for grade in ("VLSFO", "MGO"):
            if grade not in v:
                raise ValueError("候选方案须同时提供 VLSFO 与 MGO 的参考样清单")
        return v


class PlanDecision(CleanInput):
    expected_revision: int = Field(ge=0)
    note: str = Field(default="", max_length=600)
