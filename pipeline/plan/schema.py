"""Schema for `config/plan.yaml`. This module is the authority; docs/plan-format.md explains it.

Validation runs in two passes, and both raise `pydantic.ValidationError` with the failing
field's location:

1. The pydantic models check shape, types, value ranges and every rule that lives inside
   one object (one anchor per step target, a backoff needs `pct_of_top`, ...).
2. `check_references` checks what needs the whole plan or `config/athlete.yaml`: block
   order and dates, week and template references, session ids, lift ids, progression
   names, FTP and curve anchors.

    python -m pipeline.plan.schema config/plan.yaml [--athlete config/athlete.yaml]

The athlete file defaults to `athlete.yaml` beside the plan.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import (
    AfterValidator,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from pipeline.athlete import Athlete
from pipeline.common import Fraction, Rpe, StrictModel, load_yaml

ENDURANCE = ("bike", "run", "handbike", "concept2", "grinding")
ANCHORS = ("pct_ftp", "watts", "pct_curve", "hr_zone", "rpe")

Modality = Literal["bike", "run", "handbike", "concept2", "grinding", "strength", "mobility", "rest"]
Slot = Literal[
    "mon_am", "mon_pm", "tue_am", "tue_pm", "wed_am", "wed_pm", "thu_am",
    "thu_pm", "fri_am", "fri_pm", "sat_am", "sat_pm", "sun_am", "sun_pm",
]  # fmt: skip


def _ordered(pair):
    lo, hi = pair
    if lo > hi:
        raise ValueError(f"low end {lo} is above high end {hi}")
    return pair


Range = Annotated[tuple[NonNegativeFloat, NonNegativeFloat], AfterValidator(_ordered)]
PositiveRange = Annotated[tuple[PositiveFloat, PositiveFloat], AfterValidator(_ordered)]
RepRange = Annotated[tuple[PositiveInt, PositiveInt], AfterValidator(_ordered)]
DateRange = Annotated[tuple[dt.date, dt.date], AfterValidator(_ordered)]
SessionRef = str | Annotated[list[str], Field(min_length=1)]


# ------------------------------------------------------------------ goals


class Goal(StrictModel):
    id: str = Field(min_length=1)
    priority: Literal["A", "B", "C"]
    date: dt.date | None = None
    window: DateRange | None = None
    description: str = Field(min_length=1)
    target: dict[str, Any] | None = None

    @field_validator("window")
    @classmethod
    def _not_with_date(cls, window, info: ValidationInfo):
        if window is not None and info.data.get("date") is not None:
            raise ValueError("give date or window, not both")
        return window


class StrengthLine(StrictModel):
    lift: str
    checkpoints: list[tuple[dt.date, PositiveFloat]] = Field(min_length=1)

    @field_validator("checkpoints")
    @classmethod
    def _dates_ascend(cls, checkpoints):
        dates = [d for d, _ in checkpoints]
        if any(b <= a for a, b in zip(dates, dates[1:])):
            raise ValueError("checkpoint dates must be strictly ascending")
        return checkpoints


# ------------------------------------------------------------------ progression rules


class Linear(StrictModel):
    type: Literal["linear"]
    increment_kg: PositiveFloat
    max_rpe: Rpe
    reset_after_misses: PositiveInt
    reset_pct: Annotated[float, Field(gt=0, lt=1)]


class Double(StrictModel):
    type: Literal["double"]
    rep_range: RepRange
    increment_kg: PositiveFloat
    max_rpe: Rpe


class Wave(StrictModel):
    type: Literal["wave"]
    pcts: list[Fraction] = Field(min_length=1)
    e1rm_step_kg: PositiveFloat


class RpeRule(StrictModel):
    type: Literal["rpe"]
    target_rpe: Rpe


class PctE1rm(StrictModel):
    type: Literal["pct_e1rm"]
    pct: Fraction


Progression = Annotated[Union[Linear, Double, Wave, RpeRule, PctE1rm], Field(discriminator="type")]


# ------------------------------------------------------------------ endurance steps


class Target(StrictModel):
    pct_ftp: PositiveRange | None = None
    watts: PositiveRange | None = None
    pct_curve: tuple[str, PositiveFloat] | None = None
    hr_zone: Annotated[int, Field(ge=1, le=5)] | None = None
    rpe: Rpe | None = None
    cadence: PositiveRange | None = None

    @model_validator(mode="after")
    def _one_anchor(self):
        given = [a for a in ANCHORS if getattr(self, a) is not None]
        if len(given) != 1:
            raise PydanticCustomError(
                "target_anchor",
                "needs exactly one anchor of {anchors}; got {given}",
                {"anchors": ", ".join(ANCHORS), "given": ", ".join(given) or "none"},
            )
        return self

    @property
    def anchor(self) -> str:
        return next(a for a in ANCHORS if getattr(self, a) is not None)


class IntervalStep(StrictModel):
    kind: Literal["warmup", "work", "rest", "cooldown", "steady", "test"]
    duration_s: PositiveInt
    target: Target
    note: str | None = None


class RepeatStep(StrictModel):
    kind: Literal["repeat"]
    count: PositiveInt
    steps: list["Step"] = Field(min_length=1)


Step = Annotated[Union[IntervalStep, RepeatStep], Field(discriminator="kind")]
RepeatStep.model_rebuild()


# ------------------------------------------------------------------ strength exercises


class Group(StrictModel):
    role: Literal["top", "backoff", "work", "speed", "amrap", "test"]
    sets: PositiveInt
    reps: PositiveInt | RepRange
    rpe_cap: Rpe | None = None
    rest_s: NonNegativeInt | None = None
    pct_of_top: Fraction | None = Field(default=None, validate_default=True)

    @field_validator("pct_of_top")
    @classmethod
    def _backoff_only(cls, pct, info: ValidationInfo):
        role = info.data.get("role")
        if role == "backoff" and pct is None:
            raise ValueError("required for a backoff group")
        if role is not None and role != "backoff" and pct is not None:
            raise ValueError(f"only a backoff group takes pct_of_top, not role {role!r}")
        return pct


class Exercise(StrictModel):
    lift: str
    progression: str | None = None
    start_kg: PositiveFloat | None = None
    groups: list[Group] = Field(min_length=1)
    note: str | None = None

    @field_validator("groups")
    @classmethod
    def _backoff_needs_top(cls, groups: list[Group]):
        roles = {g.role for g in groups}
        if "backoff" in roles and "top" not in roles:
            raise ValueError("a backoff group needs a top group in the same exercise")
        return groups


# ------------------------------------------------------------------ sessions


class SessionTargets(StrictModel):
    tss: NonNegativeFloat | None = None
    steps_held_pct: Fraction | None = None
    note: str | None = None


class Session(StrictModel):
    modality: Modality
    summary: str = Field(min_length=1)
    environment: Literal["indoor", "outdoor", "either"] | None = None
    targets: SessionTargets | None = None
    readiness_exempt: bool = False
    steps: list[Step] | None = Field(default=None, min_length=1, validate_default=True)
    exercises: list[Exercise] | None = Field(default=None, min_length=1, validate_default=True)

    @field_validator("steps")
    @classmethod
    def _steps_for_endurance(cls, steps, info: ValidationInfo):
        modality = info.data.get("modality")
        if modality in ENDURANCE and steps is None:
            raise ValueError(f"required for modality {modality!r}")
        if modality is not None and modality not in ENDURANCE and steps is not None:
            raise ValueError(f"not allowed for modality {modality!r} (only {', '.join(ENDURANCE)})")
        return steps

    @field_validator("exercises")
    @classmethod
    def _exercises_for_strength(cls, exercises, info: ValidationInfo):
        modality = info.data.get("modality")
        if modality == "strength" and exercises is None:
            raise ValueError("required for modality 'strength'")
        if modality is not None and modality != "strength" and exercises is not None:
            raise ValueError(f"not allowed for modality {modality!r} (only strength)")
        return exercises


# ------------------------------------------------------------------ calendar


class Week(StrictModel):
    start: dt.date
    label: str = Field(min_length=1)
    template: str
    weekly_tss: Range | None = None
    volume_pct: PositiveFloat | None = None
    swap: dict[Slot, SessionRef | None] | None = None


class Block(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    start: dt.date
    end: dt.date
    intent: str = Field(min_length=1)
    status: Literal["designed", "design_pending"] = "designed"
    weekly_tss: Range | None = None
    ctl_end: Range | None = None
    targets_provisional: bool = False
    weeks: list[Week] | None = Field(default=None, min_length=1, validate_default=True)

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, end, info: ValidationInfo):
        start = info.data.get("start")
        if start is not None and end < start:
            raise ValueError(f"block ends ({end}) before it starts ({start})")
        return end

    @field_validator("weeks")
    @classmethod
    def _designed_has_weeks(cls, weeks, info: ValidationInfo):
        if info.data.get("status", "designed") == "designed" and weeks is None:
            raise ValueError("required for a designed block (mark it status: design_pending otherwise)")
        return weeks


class Plan(StrictModel):
    schema_version: Literal[1]
    plan_id: str = Field(min_length=1)
    created: dt.date
    designed_in: str = Field(min_length=1)
    valid_from: dt.date
    valid_to: dt.date
    goals: list[Goal] = Field(min_length=1)
    strength_lines: list[StrengthLine] = []
    progressions: dict[str, Progression]
    blocks: list[Block] = Field(min_length=1)
    week_templates: dict[str, dict[Slot, SessionRef]]
    sessions: dict[str, Session] = Field(min_length=1)


# ------------------------------------------------------------------ pass 2: references

Loc = tuple[str | int, ...]


def check_references(plan: Plan, athlete: Athlete) -> list[tuple[Loc, str, Any]]:
    """Rules that need the whole plan or athlete.yaml. Returns (location, message, value) per problem."""
    problems: list[tuple[Loc, str, Any]] = []
    lifts = athlete.strength.lifts

    def bad(loc: Loc, msg: str, value: Any) -> None:
        problems.append((loc, msg, value))

    def session_refs(loc: Loc, ref: SessionRef | None) -> None:
        for sid in [ref] if isinstance(ref, str) else ref or []:
            if sid not in plan.sessions:
                bad(loc, f"unknown session {sid!r} (not in sessions)", ref)

    # Validity window and calendar
    if plan.valid_from != plan.blocks[0].start:
        bad(("valid_from",), f"must equal the first block start ({plan.blocks[0].start})", plan.valid_from)
    if plan.valid_to != plan.blocks[-1].end:
        bad(("valid_to",), f"must equal the last block end ({plan.blocks[-1].end})", plan.valid_to)

    seen_blocks: set[str] = set()
    for i, block in enumerate(plan.blocks):
        if block.id in seen_blocks:
            bad(("blocks", i, "id"), f"duplicate block id {block.id!r}", block.id)
        seen_blocks.add(block.id)
        if i and block.start <= plan.blocks[i - 1].end:
            prev = plan.blocks[i - 1]
            bad(("blocks", i, "start"), f"overlaps or precedes block {prev.id!r} (ends {prev.end})", block.start)

        for j, week in enumerate(block.weeks or []):
            wloc = ("blocks", i, "weeks", j)
            if not block.start <= week.start <= block.end:
                bad(wloc + ("start",), f"outside block {block.id!r} ({block.start} to {block.end})", week.start)
            elif j == 0 and week.start != block.start:
                bad(wloc + ("start",), f"first week must start on the block start ({block.start})", week.start)
            elif j and week.start <= block.weeks[j - 1].start:
                bad(wloc + ("start",), "weeks must be in date order", week.start)
            if week.template not in plan.week_templates:
                bad(wloc + ("template",), f"unknown week template {week.template!r}", week.template)
            for slot, ref in (week.swap or {}).items():
                session_refs(wloc + ("swap", slot), ref)

    for name, template in plan.week_templates.items():
        for slot, ref in template.items():
            session_refs(("week_templates", name, slot), ref)

    # Goals and strength lines
    seen_goals: set[str] = set()
    for i, goal in enumerate(plan.goals):
        if goal.id in seen_goals:
            bad(("goals", i, "id"), f"duplicate goal id {goal.id!r}", goal.id)
        seen_goals.add(goal.id)
        lift = (goal.target or {}).get("lift")
        if lift is not None and lift not in lifts:
            bad(("goals", i, "target", "lift"), f"unknown lift {lift!r} (not in athlete.yaml strength.lifts)", lift)

    for i, line in enumerate(plan.strength_lines):
        if line.lift not in lifts:
            bad(("strength_lines", i, "lift"), f"unknown lift {line.lift!r} (not in athlete.yaml strength.lifts)", line.lift)

    # Sessions
    def check_steps(loc: Loc, modality: str, steps: list) -> None:
        for k, step in enumerate(steps):
            sloc = loc + (k,)
            if isinstance(step, RepeatStep):
                check_steps(sloc + ("steps",), modality, step.steps)
                continue
            target = step.target
            if target.pct_ftp is not None and modality not in athlete.ftp:
                bad(sloc + ("target", "pct_ftp"), f"no {modality} FTP in athlete.yaml to take a fraction of", target.pct_ftp)
            if target.pct_curve is not None and target.pct_curve[0] not in athlete.curves:
                ref = target.pct_curve[0]
                bad(sloc + ("target", "pct_curve"), f"unknown curve point {ref!r} (not in athlete.yaml curves)", target.pct_curve)

    for sid, session in plan.sessions.items():
        if session.steps:
            check_steps(("sessions", sid, "steps"), session.modality, session.steps)
        for k, ex in enumerate(session.exercises or []):
            eloc = ("sessions", sid, "exercises", k)
            if ex.lift not in lifts:
                bad(eloc + ("lift",), f"unknown lift {ex.lift!r} (not in athlete.yaml strength.lifts)", ex.lift)
            rule = None
            if ex.progression is not None:
                rule = plan.progressions.get(ex.progression)
                if rule is None:
                    bad(eloc + ("progression",), f"unknown progression {ex.progression!r} (not in progressions)", ex.progression)
            for g, group in enumerate(ex.groups):
                gloc = eloc + ("groups", g, "reps")
                if isinstance(rule, Double):
                    if group.reps != rule.rep_range:
                        bad(gloc, f"must be the rep_range of {ex.progression!r}: {list(rule.rep_range)}", group.reps)
                elif isinstance(group.reps, tuple) and (rule is not None or ex.progression is None):
                    bad(gloc, "a rep range [lo, hi] needs a double progression", group.reps)

    return problems


def validate_plan(data: Any, athlete: Athlete) -> Plan:
    """Validate parsed plan data against the schema and the athlete config. Raises ValidationError."""
    plan = Plan.model_validate(data)
    problems = check_references(plan, athlete)
    if problems:
        raise ValidationError.from_exception_data(
            "Plan",
            [
                {"type": PydanticCustomError("plan_reference", "{msg}", {"msg": msg}), "loc": loc, "input": value}
                for loc, msg, value in problems
            ],
        )
    return plan


def plan_warnings(plan: Plan) -> list[str]:
    """Soft rules from the doc ("should"): reported, never fatal."""
    out = []
    for sid, session in plan.sessions.items():
        if session.modality not in ("mobility", "rest") and (session.targets is None or session.targets.tss is None):
            out.append(f"sessions.{sid}.targets.tss: missing; every session should carry a TSS estimate")
    for i, block in enumerate(plan.blocks):
        if block.weekly_tss is None:
            out.append(f"blocks[{i}].weekly_tss: missing for block {block.id!r}")
    return out


# ------------------------------------------------------------------ error formatting and CLI


def format_loc(loc: Loc, data: Any = None) -> str:
    """`sessions.z2_60.steps[1].target`. With the input data, drops the union-member segments
    pydantic adds to a location (e.g. the step kind or `int`) that aren't keys in the file."""
    parts: list[str] = []
    node = data
    for n, seg in enumerate(loc):
        last = n == len(loc) - 1
        if data is not None:
            if isinstance(node, dict) and seg in node:
                node = node[seg]
            elif isinstance(node, list) and isinstance(seg, int) and 0 <= seg < len(node):
                node = node[seg]
            elif not (last and isinstance(node, dict)):  # a missing field is the last segment
                continue
        parts.append(f"[{seg}]" if isinstance(seg, int) else ("." if parts else "") + str(seg))
    return "".join(parts) or "<plan>"


def format_errors(err: ValidationError, data: Any = None) -> list[str]:
    out = []
    for e in err.errors():
        loc, msg = e["loc"], e["msg"]
        if loc and loc[-1] == "[key]":  # the mapping key itself is invalid, not its value
            loc, msg = loc[:-1], f"invalid key: {msg}"
        out.append(f"{format_loc(loc, data)}: {msg}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.plan.schema", description="Validate a plan file.")
    parser.add_argument("plan", type=Path, help="path to plan.yaml")
    parser.add_argument("--athlete", type=Path, help="path to athlete.yaml (default: beside the plan)")
    args = parser.parse_args(argv)
    athlete_path = args.athlete or args.plan.with_name("athlete.yaml")

    try:
        athlete_data = load_yaml(athlete_path)
        data = load_yaml(args.plan)
    except (OSError, yaml.YAMLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        athlete = Athlete.model_validate(athlete_data)
    except ValidationError as e:
        print(f"{athlete_path}: INVALID - {e.error_count()} error(s)")
        for line in format_errors(e, athlete_data):
            print(f"  {line}")
        return 1

    try:
        plan = validate_plan(data, athlete)
    except ValidationError as e:
        print(f"{args.plan}: INVALID - {e.error_count()} error(s)")
        for line in format_errors(e, data):
            print(f"  {line}")
        return 1

    designed = sum(b.status == "designed" for b in plan.blocks)
    print(
        f"{args.plan}: OK - plan {plan.plan_id}, {plan.valid_from} to {plan.valid_to}; "
        f"{len(plan.blocks)} blocks ({designed} designed, {len(plan.blocks) - designed} design pending), "
        f"{len(plan.week_templates)} week templates, {len(plan.sessions)} sessions, "
        f"{len(plan.progressions)} progressions, {len(plan.goals)} goals"
    )
    for w in plan_warnings(plan):
        print(f"  warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
