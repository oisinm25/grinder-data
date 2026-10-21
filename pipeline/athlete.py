"""Schema for `config/athlete.yaml`: the anchors the plan points at.

FTP, LTHR, power-curve points, strength rounding, readiness rules and the lift ids a
plan may use live here and never in the plan (docs/plan-format.md).
"""

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, PositiveFloat, PositiveInt, StringConstraints, field_validator

from pipeline.common import Fraction, Rpe, StrictModel, load_yaml

LiftId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
CurveRef = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*_\d+s$")]
FtpModality = Literal["bike", "run", "handbike", "concept2"]


class FtpAnchor(StrictModel):
    watts: PositiveFloat
    provisional: bool = False
    note: str | None = None


class CurvePoint(StrictModel):
    duration_s: PositiveInt
    watts: PositiveFloat
    provisional: bool = False
    note: str | None = None


class Lift(StrictModel):
    name: str = Field(min_length=1)


class Strength(StrictModel):
    rounding_kg: PositiveFloat
    lifts: dict[LiftId, Lift] = Field(min_length=1)


class AmberRule(StrictModel):
    hold_last_weight: bool
    allow_increment: bool
    rpe_cap: Rpe


class RedRule(StrictModel):
    pct_of_last_weight: Fraction
    rpe_cap: Rpe


class Readiness(StrictModel):
    amber: AmberRule
    red: RedRule


class Athlete(StrictModel):
    schema_version: Literal[1]
    lthr_bpm: PositiveInt
    ftp: dict[FtpModality, FtpAnchor]
    curves: dict[CurveRef, CurvePoint] = {}
    strength: Strength
    readiness: Readiness

    @field_validator("curves")
    @classmethod
    def _key_matches_duration(cls, curves: dict[str, CurvePoint]) -> dict[str, CurvePoint]:
        for key, point in curves.items():
            seconds = int(re.search(r"_(\d+)s$", key).group(1))
            if seconds != point.duration_s:
                raise ValueError(f"{key}: key says {seconds} s but duration_s is {point.duration_s}")
        return curves


def load_athlete(path: str | Path) -> Athlete:
    return Athlete.model_validate(load_yaml(path))
