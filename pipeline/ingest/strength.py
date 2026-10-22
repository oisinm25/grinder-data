"""Strength log reader: `training/logs/strength.csv` (format: `training/logs/README.md`).

One row per set. Every row is checked: types and ranges, the lift id against
`athlete.yaml`, the session id against the plan. Errors name the file line and the
column. Softer mismatches (a lift the session doesn't list, a session not scheduled
on that date) are warnings: sessions move and extra work happens.
"""

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, NonNegativeFloat, PositiveInt, ValidationError, field_validator

from pipeline.athlete import Athlete
from pipeline.common import Rpe, StrictModel
from pipeline.plan.expand import expand
from pipeline.plan.schema import Plan

COLUMNS = ("date", "session", "lift", "role", "kg", "reps", "rpe", "notes")
TOP_ROLES = ("top", "amrap", "test")  # what counts as a top set


class LoggedSet(StrictModel):
    date: dt.date
    session: str = Field(min_length=1)
    lift: str = Field(min_length=1)
    role: Literal["top", "backoff", "work", "speed", "amrap", "test"]
    kg: NonNegativeFloat  # added load; 0 = bodyweight only
    reps: PositiveInt
    rpe: Rpe | None = None
    notes: str = ""

    @field_validator("rpe", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def e1rm_kg(self) -> float:
        return e1rm(self.kg, self.reps, self.rpe)


def e1rm(kg: float, reps: int, rpe: float | None) -> float:
    """e1RM = kg x (1 + (reps + (10 - RPE)) / 30), clean reps only. A missing RPE counts as 10."""
    return kg * (1 + (reps + (10 - (10 if rpe is None else rpe))) / 30)


@dataclass(frozen=True)
class StrengthLog:
    sets: list[tuple[int, LoggedSet]]  # (file line, set)
    warnings: list[str]


class StrengthLogError(Exception):
    def __init__(self, path: Path, errors: list[str]):
        super().__init__(f"{path}: {len(errors)} error(s)")
        self.path = path
        self.errors = errors


def read_strength_log(path: Path, plan: Plan, athlete: Athlete) -> StrengthLog:
    path = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    sets: list[tuple[int, LoggedSet]] = []

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = tuple(reader.fieldnames or ())
        if header != COLUMNS:
            raise StrengthLogError(path, [f"line 1: header must be {','.join(COLUMNS)}; got {','.join(header) or 'nothing'}"])
        for row in reader:
            line = reader.line_num
            if None in row:  # more cells than columns
                errors.append(f"line {line}: {len(row[None])} extra cell(s); quote notes that contain commas")
                continue
            try:
                s = LoggedSet.model_validate({k: (v or "").strip() for k, v in row.items()})
            except ValidationError as e:
                for err in e.errors():
                    col = err["loc"][0] if err["loc"] else "row"
                    errors.append(f"line {line}, {col}: {err['msg']} (got {row.get(col)!r})")
                continue

            if s.lift not in athlete.strength.lifts:
                errors.append(f"line {line}, lift: unknown lift {s.lift!r} (not in athlete.yaml strength.lifts)")
            session = plan.sessions.get(s.session)
            if session is None:
                errors.append(f"line {line}, session: unknown session {s.session!r} (not in plan.yaml sessions)")
            elif session.modality != "strength":
                errors.append(f"line {line}, session: {s.session!r} is a {session.modality} session, not strength")
            else:
                if s.lift not in {ex.lift for ex in session.exercises or []}:
                    warnings.append(f"line {line}: {s.lift} is not part of session {s.session}")
                scheduled = {x.session_id for d in expand(plan, athlete, s.date, s.date) for x in d.sessions}
                if s.session not in scheduled:
                    warnings.append(f"line {line}: {s.session} is not scheduled on {s.date} in the plan")
            sets.append((line, s))

    if errors:
        raise StrengthLogError(path, errors)
    return StrengthLog(sets, warnings)


def latest_top_sets(log: StrengthLog) -> dict[str, LoggedSet]:
    """Per lift: the heaviest top/amrap/test set on the latest date that has one,
    falling back to the heaviest set on the lift's latest date."""
    out: dict[str, LoggedSet] = {}
    by_lift: dict[str, list[LoggedSet]] = {}
    for _, s in log.sets:
        by_lift.setdefault(s.lift, []).append(s)
    for lift, sets in by_lift.items():
        pool = [s for s in sets if s.role in TOP_ROLES] or sets
        latest = max(s.date for s in pool)
        out[lift] = max((s for s in pool if s.date == latest), key=lambda s: (s.kg, s.reps))
    return dict(sorted(out.items()))
