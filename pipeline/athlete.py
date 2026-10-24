"""Schema for `config/athlete.yaml`: the anchors the plan points at.

FTP, LTHR, power-curve points, strength rounding, readiness rules and the lift ids a
plan may use live here and never in the plan (docs/plan-format.md).
"""

import datetime as dt
import re
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


class ReadinessRules(StrictModel):
    """How the day's colour is computed (pipeline/readiness.py)."""

    baseline_days: PositiveInt
    min_baseline_days: PositiveInt
    hrv_sd_below: PositiveFloat
    resting_hr_above_bpm: PositiveFloat
    sleep_min_h: PositiveFloat

    @field_validator("min_baseline_days")
    @classmethod
    def _fits_window(cls, n, info):
        window = info.data.get("baseline_days")
        if n < 2:
            raise ValueError("needs at least 2 days to compute an SD")
        if window is not None and n > window:
            raise ValueError(f"more than baseline_days ({window})")
        return n


class Readiness(StrictModel):
    rules: ReadinessRules
    amber: AmberRule
    red: RedRule


HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class Schedule(StrictModel):
    """When the daily run may go: a local-time window in `timezone` (end inclusive)."""

    timezone: str
    window: tuple[HHMM, HHMM]

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, tz: str) -> str:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {tz!r} (use an IANA name such as Europe/Madrid)") from None
        return tz

    @field_validator("window")
    @classmethod
    def _ordered(cls, window: tuple[str, str]) -> tuple[str, str]:
        if window[0] >= window[1]:
            raise ValueError(f"window start {window[0]} must be before its end {window[1]}")
        return window

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def contains(self, local: dt.datetime) -> bool:
        return self.window[0] <= local.strftime("%H:%M") <= self.window[1]


class Athlete(StrictModel):
    schema_version: Literal[1]
    lthr_bpm: PositiveInt
    ftp: dict[FtpModality, FtpAnchor]
    curves: dict[CurveRef, CurvePoint] = {}
    strength: Strength
    readiness: Readiness
    schedule: Schedule

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
