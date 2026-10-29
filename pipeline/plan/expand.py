"""Expand a validated plan into dated daily sessions, with targets resolved from athlete.yaml.

For each date: find its block and week, take the week's template, apply that week's
`swap` (a session id or list replaces the slot, `null` drops it) and list the sessions
slot by slot, am then pm, in the order given. Endurance targets are resolved to watts
where an anchor allows it:

- `pct_ftp`   x that modality's FTP           -> watts band
- `watts`     as written                       -> watts band
- `pct_curve` x the curve point (e.g. 60 s)    -> single watts value
- `hr_zone`, `rpe`                             -> left as written (no watts)

Then the session changes in training/logs/changes.yaml (pipeline.plan.changes) are applied in
file order: a moved or swapped session carries `moved_from`, and the slot it left carries a
`Vacated` marker on its Day.

Resolved watts carry `provisional` when the anchor behind them is provisional
(the hand-bike FTP until it's tested). Strength weights are not prescribed here;
that is the progression engine's job.

    python -m pipeline.plan.expand config/plan.yaml [--from 2026-10-26] [--days 7 | --to DATE] [--csv PATH]
"""

import argparse
import csv
import datetime as dt
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from pipeline.athlete import Athlete
from pipeline.plan.changes import Applied, Pos, Vacated, apply
from pipeline.plan.schema import (
    Block,
    IntervalStep,
    PlanLoadError,
    Plan,
    RepeatStep,
    Session,
    Week,
    load_checked,
    report_load_error,
)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ENDURANCE_MODALITIES = ("bike", "run", "handbike", "concept2", "grinding")


@dataclass(frozen=True)
class ResolvedTarget:
    anchor: str  # pct_ftp | watts | pct_curve | hr_zone | rpe
    watts: tuple[int, int] | None = None
    hr_zone: int | None = None
    rpe: float | None = None
    cadence: tuple[float, float] | None = None
    provisional: bool = False

    def text(self) -> str:
        if self.watts:
            lo, hi = self.watts
            out = f"{lo} W" if lo == hi else f"{lo}-{hi} W"
            out += "*" if self.provisional else ""
        elif self.hr_zone is not None:
            out = f"HR Z{self.hr_zone}"
        else:
            out = f"RPE {self.rpe:g}"
        if self.cadence:
            out += f" @ {self.cadence[0]:g}-{self.cadence[1]:g} rpm"
        return out


@dataclass(frozen=True)
class ResolvedStep:
    kind: str
    duration_s: int  # for a repeat: the whole repeat
    target: ResolvedTarget | None = None  # None for a repeat
    note: str | None = None
    count: int = 1
    steps: tuple["ResolvedStep", ...] = ()

    def text(self) -> str:
        if self.kind == "repeat":
            return f"{self.count} x [" + ", ".join(s.text() for s in self.steps) + "]"
        return f"{self.kind} {_clock(self.duration_s)} {self.target.text()}"


@dataclass(frozen=True)
class ScheduledSession:
    date: dt.date
    slot: str  # mon_am ... sun_pm
    order: int  # position within the slot, from 0
    session_id: str
    session: Session
    steps: tuple[ResolvedStep, ...]
    duration_s: int | None  # None when the session has no timed steps
    tss: float
    moved_from: Pos | None = None  # where the plan had it, when changes.yaml moved or swapped it
    change_reason: str | None = None
    change_kind: str | None = None  # moved | done_late, with moved_from

    @property
    def steps_text(self) -> str:
        return "; ".join(s.text() for s in self.steps)

    @property
    def home_slot(self) -> str:
        """The plan's slot for this session: its history and basis follow it when it moves."""
        return self.moved_from.slot if self.moved_from else self.slot

    @property
    def moved_note(self) -> str:
        """'(moved from Mon)', '(moved from AM)' on the same day, '(done late, planned Mon)', or ''."""
        if not self.moved_from:
            return ""
        same_day = self.moved_from.date == self.date
        where = self.moved_from.half.upper() if same_day else f"{self.moved_from.date:%a}"
        return f"(done late, planned {where})" if self.change_kind == "done_late" else f"(moved from {where})"


@dataclass(frozen=True)
class Day:
    date: dt.date
    block: Block
    week: Week | None  # None in a design-pending block
    sessions: tuple[ScheduledSession, ...]
    vacated: tuple[Vacated, ...] = ()  # planned here, but moved away or dropped by changes.yaml

    @property
    def design_pending(self) -> bool:
        return self.block.status == "design_pending"


@dataclass(frozen=True)
class WeekSummary:
    block: Block
    week: Week
    start: dt.date
    end: dt.date
    planned_tss: float

    @property
    def verdict(self) -> str:
        if self.week.weekly_tss is None:
            return "no target"
        lo, hi = self.week.weekly_tss
        return "under" if self.planned_tss < lo else "over" if self.planned_tss > hi else "ok"


# ------------------------------------------------------------------ resolution


def _round(x: float) -> int:
    return math.floor(x + 0.5)  # half up: 399.5 W -> 400 W


def _clock(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def resolve_target(target, modality: str, athlete: Athlete) -> ResolvedTarget:
    common = {"anchor": target.anchor, "cadence": target.cadence}
    if target.pct_ftp is not None:
        ftp = athlete.ftp[modality]
        lo, hi = target.pct_ftp
        return ResolvedTarget(watts=(_round(lo * ftp.watts), _round(hi * ftp.watts)), provisional=ftp.provisional, **common)
    if target.watts is not None:
        lo, hi = target.watts
        return ResolvedTarget(watts=(_round(lo), _round(hi)), **common)
    if target.pct_curve is not None:
        ref, pct = target.pct_curve
        point = athlete.curves[ref]
        w = _round(pct * point.watts)
        return ResolvedTarget(watts=(w, w), provisional=point.provisional, **common)
    if target.hr_zone is not None:
        return ResolvedTarget(hr_zone=target.hr_zone, **common)
    return ResolvedTarget(rpe=target.rpe, **common)


def resolve_steps(steps, modality: str, athlete: Athlete) -> tuple[ResolvedStep, ...]:
    out = []
    for step in steps:
        if isinstance(step, RepeatStep):
            inner = resolve_steps(step.steps, modality, athlete)
            once = sum(s.duration_s for s in inner)
            out.append(ResolvedStep("repeat", once * step.count, count=step.count, steps=inner))
        else:
            assert isinstance(step, IntervalStep)
            target = resolve_target(step.target, modality, athlete)
            out.append(ResolvedStep(step.kind, step.duration_s, target, step.note))
    return tuple(out)


# ------------------------------------------------------------------ calendar


def _block_for(plan: Plan, day: dt.date) -> Block | None:
    return next((b for b in plan.blocks if b.start <= day <= b.end), None)


def _week_for(block: Block, day: dt.date) -> Week | None:
    current = None
    for week in block.weeks or []:
        if week.start <= day:
            current = week
    return current


def slots_for(plan: Plan, week: Week) -> dict[str, list[str]]:
    """The week's template with its swaps applied: slot -> session ids in order."""
    slots: dict[str, list[str]] = {}
    for slot, ref in plan.week_templates[week.template].items():
        slots[slot] = [ref] if isinstance(ref, str) else list(ref)
    for slot, ref in (week.swap or {}).items():
        if ref is None:
            slots.pop(slot, None)
        else:
            slots[slot] = [ref] if isinstance(ref, str) else list(ref)
    return slots


def planned_slots(plan: Plan, day: dt.date) -> dict[str, list[str]] | None:
    """The plan's own slots for one date (no changes): slot -> session ids. None outside a designed week."""
    block = _block_for(plan, day)
    week = _week_for(block, day) if block else None
    return slots_for(plan, week) if week is not None else None


def apply_changes(plan: Plan, athlete: Athlete, changes=None) -> Applied:
    """The plan's changes (default: plan.changes) applied in order. Raises changes.ChangesError."""
    return apply(plan.changes if changes is None else changes, lambda d: planned_slots(plan, d), plan.sessions,
                 (plan.valid_from, plan.valid_to))  # fmt: skip


def expand(plan: Plan, athlete: Athlete, start: dt.date, end: dt.date, *, changes=None) -> list[Day]:
    """One Day per date from start to end inclusive, clipped to the plan's validity window.
    `changes` defaults to plan.changes; pass () for the plan as written."""
    start, end = max(start, plan.valid_from), min(end, plan.valid_to)
    todo = plan.changes if changes is None else changes
    applied = apply_changes(plan, athlete, todo) if todo else None
    changed = applied.dates() if applied else set()
    resolved: dict[str, tuple[tuple[ResolvedStep, ...], int | None]] = {}
    days = []
    day = start
    while day <= end:
        block = _block_for(plan, day)
        if block is None:  # a gap between blocks
            day += dt.timedelta(days=1)
            continue
        week = _week_for(block, day)
        sessions = []
        if week is not None:
            slots = slots_for(plan, week)
            prefix = DAYS[day.weekday()]
            for half in ("am", "pm"):
                slot = f"{prefix}_{half}"
                here = Pos(day, half)
                entries = applied.slots.get(here, []) if day in changed else [None] * len(slots.get(slot, []))
                ids = [e.session_id for e in entries] if day in changed else slots.get(slot, [])
                for order, (sid, entry) in enumerate(zip(ids, entries)):
                    session = plan.sessions[sid]
                    if sid not in resolved:
                        steps = resolve_steps(session.steps or [], session.modality, athlete)
                        resolved[sid] = (steps, sum(s.duration_s for s in steps) if steps else None)
                    steps, duration = resolved[sid]
                    tss = session.targets.tss if session.targets and session.targets.tss is not None else 0.0
                    moved = entry.origin if entry is not None and entry.origin != here else None
                    sessions.append(ScheduledSession(day, slot, order, sid, session, steps, duration, tss, moved,
                                                     entry.reason if moved else None, entry.kind if moved else None))  # fmt: skip
        vacated = tuple(applied.vacated.get(day, ())) if applied else ()
        days.append(Day(day, block, week, tuple(sessions), vacated))
        day += dt.timedelta(days=1)
    return days


def week_summaries(plan: Plan, days: list[Day]) -> list[WeekSummary]:
    """Planned TSS per designed week, against that week's weekly_tss. Partial weeks count only the days given."""
    by_week: dict[tuple[str, dt.date], list[Day]] = {}
    for d in days:
        if d.week is not None:
            by_week.setdefault((d.block.id, d.week.start), []).append(d)
    return [
        WeekSummary(ds[0].block, ds[0].week, ds[0].date, ds[-1].date, sum(s.tss for d in ds for s in d.sessions))
        for ds in by_week.values()
    ]


# ------------------------------------------------------------------ output

CSV_FIELDS = (
    "date", "weekday", "slot", "order", "block_id", "week_label", "session_id", "modality",
    "environment", "summary", "duration_min", "tss", "readiness_exempt", "steps", "moved_from",
)  # fmt: skip


def write_csv(days: list[Day], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, CSV_FIELDS)
        w.writeheader()
        for d in days:
            for s in d.sessions:
                w.writerow({
                    "date": d.date.isoformat(),
                    "weekday": d.date.strftime("%a"),
                    "slot": s.slot,
                    "order": s.order,
                    "block_id": d.block.id,
                    "week_label": d.week.label,
                    "session_id": s.session_id,
                    "modality": s.session.modality,
                    "environment": s.session.environment or "",
                    "summary": s.session.summary,
                    "duration_min": "" if s.duration_s is None else round(s.duration_s / 60, 1),
                    "tss": f"{s.tss:g}",
                    "readiness_exempt": s.session.readiness_exempt,
                    "steps": s.steps_text,
                    "moved_from": str(s.moved_from) if s.moved_from else "",
                })  # fmt: skip


def format_days(plan: Plan, days: list[Day]) -> list[str]:
    lines = []
    for d in days:
        where = f"{d.block.id} {d.block.name}" + (f" / {d.week.label}" if d.week else "")
        lines.append(f"{d.date:%a %Y-%m-%d}  {where}")
        if d.design_pending:
            lines.append("    (design pending - no sessions yet)")
        elif not d.sessions and not d.vacated:
            lines.append("    (nothing scheduled)")
        rows = [(s.slot[-2:], 0, s) for s in d.sessions] + [(v.slot[-2:], 1, v) for v in d.vacated]
        for _, gone, s in sorted(rows, key=lambda r: (r[0], r[1])):
            if gone:
                lines.append(f"  {s.slot[-2:]}  {s.session_id:<22} {s.note(d.date)}")
                continue
            dur = f"{_clock(s.duration_s):>7}" if s.duration_s else " " * 7
            tss = f"{s.tss:>4g} TSS" if s.tss else " " * 8
            exempt = " [test: readiness-exempt]" if s.session.readiness_exempt else ""
            moved = f" {s.moved_note}" if s.moved_from else ""
            lines.append(f"  {s.slot[-2:]}  {s.session_id:<22} {s.session.modality:<9} {dur} {tss}  {s.session.summary}{exempt}{moved}")
            if s.steps:
                lines.append(f"        {s.steps_text}")
            for ex in s.session.exercises or []:
                groups = ", ".join(
                    f"{g.role} {g.sets}x{g.reps if isinstance(g.reps, int) else f'{g.reps[0]}-{g.reps[1]}'}"
                    + (f" @{g.pct_of_top:.0%} of top" if g.pct_of_top else "")
                    + (f" RPE<={g.rpe_cap:g}" if g.rpe_cap else "")
                    for g in ex.groups
                )
                rule = f" ({ex.progression})" if ex.progression else ""
                lines.append(f"        {ex.lift}{rule}: {groups}")
    summaries = week_summaries(plan, days)
    if summaries:
        lines.append("")
        lines.append("Planned TSS by week:")
        for w in summaries:
            target = f"target {w.week.weekly_tss[0]:g}-{w.week.weekly_tss[1]:g}" if w.week.weekly_tss else "no target"
            span = f"{w.start:%d %b}-{w.end:%d %b}"
            lines.append(f"  {w.week.label:<10} {span:<14} {w.planned_tss:>5g} TSS  {target}  {w.verdict}")
    if any(s.steps and any(_has_provisional(st) for st in s.steps) for d in days for s in d.sessions):
        lines.append("")
        lines.append("* resolved from a provisional anchor in athlete.yaml")
    return lines


def _has_provisional(step: ResolvedStep) -> bool:
    if step.kind == "repeat":
        return any(_has_provisional(s) for s in step.steps)
    return step.target.provisional


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.plan.expand", description="List the plan's sessions by date.")
    parser.add_argument("plan", type=Path, help="path to plan.yaml")
    parser.add_argument("--athlete", type=Path, help="path to athlete.yaml (default: beside the plan)")
    parser.add_argument("--from", dest="start", type=dt.date.fromisoformat, default=dt.date.today(), help="first date (default: today)")
    span = parser.add_mutually_exclusive_group()
    span.add_argument("--days", type=int, default=7, help="number of days (default: 7)")
    span.add_argument("--to", dest="end", type=dt.date.fromisoformat, help="last date, inclusive")
    parser.add_argument("--csv", type=Path, help="also write the sessions to this CSV file")
    args = parser.parse_args(argv)

    try:
        plan, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        return report_load_error(e)

    end = args.end or args.start + dt.timedelta(days=args.days - 1)
    days = expand(plan, athlete, args.start, end)
    if not days:
        print(f"no plan days between {args.start} and {end} (plan runs {plan.valid_from} to {plan.valid_to})")
        return 0
    print("\n".join(format_days(plan, days)))
    if args.csv:
        write_csv(days, args.csv)
        print(f"\nwrote {sum(len(d.sessions) for d in days)} sessions to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
