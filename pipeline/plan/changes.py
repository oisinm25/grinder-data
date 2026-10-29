"""Date-scoped session changes on top of config/plan.yaml: training/logs/changes.yaml.

A list, applied in file order by pipeline.plan.expand:

    - { type: move, session: upper_me, from: 2026-10-26 am, to: 2026-10-27 am, reason: "skipped Monday" }
    - { type: swap, a: 2026-10-27 pm, b: 2026-10-28 pm }
    - { type: drop, session: z2_60, date: 2026-10-28 pm, reason: "travel" }
    - { type: done_late, session: upper_me, planned: 2026-10-26 am, done: 2026-10-27 pm }

A move takes one session out of its slot and adds it to the end of another; a swap exchanges
everything in two slots; a drop removes one session. done_late records after the fact that a
session was done later than planned: it applies like a move (graded and prescribed on the day
it was done), and is the one kind that may be added for past dates. A moved or swapped session carries
`moved_from` (its slot in the plan) and leaves a `Vacated` marker in that slot, so compliance and
the debrief show "moved to Tue AM" or "dropped" there, not skipped. The plan file is never edited.

    python -m pipeline.plan.changes [--today DATE]   # validate, check new entries, warn on design rules

Entries already committed (git HEAD's copy of the file) are history: they stay and may lie in
the past. New entries can't touch a date before today, except done_late, whose done date
can't be after today.
"""

import argparse
import datetime as dt
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from pipeline.common import load_yaml

CHANGES_PATH = Path("training/logs/changes.yaml")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_POS = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(am|pm)$")


@dataclass(frozen=True, order=True)
class Pos:
    """A dated slot: 2026-10-27 am."""

    date: dt.date
    half: str  # am | pm

    @property
    def slot(self) -> str:
        return f"{DAYS[self.date.weekday()]}_{self.half}"

    def __str__(self) -> str:
        return f"{self.date.isoformat()} {self.half}"

    def label(self, relative_to: dt.date | None = None) -> str:
        """'Tue AM', or just 'AM' on the same day."""
        return self.half.upper() if relative_to == self.date else f"{self.date:%a} {self.half.upper()}"

    @classmethod
    def parse(cls, raw) -> "Pos":
        m = _POS.match(str(raw).strip())
        if not m:
            raise ValueError(f"{raw!r}: expected 'YYYY-MM-DD am' or 'YYYY-MM-DD pm'")
        return cls(dt.date.fromisoformat(m.group(1)), m.group(2))


class _Change(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True, arbitrary_types_allowed=True)
    reason: str | None = None

    @field_validator("from_", "to", "a", "b", "date", "planned", "done", mode="before", check_fields=False)
    @classmethod
    def _positions(cls, v):
        return v if isinstance(v, Pos) else Pos.parse(v)


class Move(_Change):
    type: Literal["move"]
    session: str = Field(min_length=1)
    from_: Pos = Field(alias="from")
    to: Pos


class Swap(_Change):
    type: Literal["swap"]
    a: Pos
    b: Pos


class Drop(_Change):
    type: Literal["drop"]
    session: str = Field(min_length=1)
    date: Pos
    reason: str = Field(min_length=1)


class DoneLate(_Change):
    type: Literal["done_late"]
    session: str = Field(min_length=1)
    planned: Pos
    done: Pos


Change = Annotated[Union[Move, Swap, Drop, DoneLate], Field(discriminator="type")]
_LIST = TypeAdapter(list[Change])


class ChangesError(Exception):
    def __init__(self, lines: list[str]):
        super().__init__("\n".join(lines))
        self.lines = lines


def parse(data) -> tuple:
    """Validate the file's structure. Raises ChangesError naming each entry."""
    if data is None:
        return ()
    try:
        return tuple(_LIST.validate_python(data))
    except ValidationError as e:
        lines = []
        for err in e.errors():
            loc = [p for p in err["loc"] if p not in ("move", "swap", "drop", "done_late")]
            where = f"changes[{loc[0]}]" + ("." + ".".join(str(p) for p in loc[1:]) if len(loc) > 1 else "") if loc else "changes"
            lines.append(f"{where.replace('from_', 'from')}: {err['msg']}")
        raise ChangesError(lines) from e


def load(path: Path) -> tuple:
    if not path.exists():
        return ()
    try:
        return parse(load_yaml(path))
    except (OSError, yaml.YAMLError) as e:
        raise ChangesError([f"{path}: {e}"]) from e


# ------------------------------------------------------------------ applying


@dataclass(frozen=True)
class Entry:
    session_id: str
    origin: Pos  # where the plan put it
    reason: str | None = None
    kind: str = "moved"  # moved | done_late: how it got here, when it isn't at its origin


@dataclass(frozen=True)
class Vacated:
    """A planned session that isn't in its slot any more: moved elsewhere, or dropped."""

    slot: str
    session_id: str
    kind: str  # moved | done_late | dropped
    to: Pos | None
    reason: str | None

    def note(self, on: dt.date) -> str:
        if self.kind == "done_late":
            return f"done late: {self.to.label(on)}" + (f" ({self.reason})" if self.reason else "")
        if self.kind == "moved":
            return f"moved to {self.to.label(on)}" + (f": {self.reason}" if self.reason else "")
        return "dropped" + (f": {self.reason}" if self.reason else "")


@dataclass
class Applied:
    """The changed slots: position -> sessions in order, and what each date lost."""

    slots: dict[Pos, list[Entry]]
    vacated: dict[dt.date, list[Vacated]]

    def dates(self) -> set[dt.date]:
        return {p.date for p in self.slots} | set(self.vacated)


def apply(changes, base, plan_sessions, valid: tuple[dt.date, dt.date]) -> Applied:
    """Apply changes in order. `base(date) -> {slot: [session ids]}` or None (no designed week that day).
    Raises ChangesError on the first change that can't apply, naming it."""
    slots: dict[Pos, list[Entry]] = {}
    dropped: list[Entry] = []

    def at(pos: Pos, i: int) -> list[Entry]:
        if not valid[0] <= pos.date <= valid[1]:
            raise ChangesError([f"changes[{i}]: {pos} is outside the plan ({valid[0]} to {valid[1]})"])
        if pos not in slots:
            day = base(pos.date)
            if day is None:
                raise ChangesError([f"changes[{i}]: {pos} has no designed week in the plan"])
            for half in ("am", "pm"):
                p = Pos(pos.date, half)
                slots.setdefault(p, [Entry(sid, p) for sid in day.get(p.slot, [])])
        return slots[pos]

    def take(pos: Pos, sid: str, i: int, verb: str) -> Entry:
        if sid not in plan_sessions:
            raise ChangesError([f"changes[{i}]: session {sid!r} isn't in the plan"])
        here = at(pos, i)
        found = next((e for e in here if e.session_id == sid), None)
        if found is None:
            where = [str(p) for p, es in slots.items() if any(e.session_id == sid for e in es)]
            also = f"; after the changes before it, it's at {', '.join(where)}" if where else ""
            raise ChangesError([f"changes[{i}]: can't {verb} {sid}: it isn't at {pos}{also}"])
        here.remove(found)
        return found

    for i, c in enumerate(changes):
        if c.type == "move":
            if c.from_ == c.to:
                raise ChangesError([f"changes[{i}]: from and to are the same slot ({c.to})"])
            e = take(c.from_, c.session, i, "move")
            at(c.to, i).append(Entry(e.session_id, e.origin, c.reason or e.reason))
        elif c.type == "done_late":
            if c.done <= c.planned:
                raise ChangesError([f"changes[{i}]: done ({c.done}) must be after planned ({c.planned})"])
            e = take(c.planned, c.session, i, "mark done late")
            at(c.done, i).append(Entry(e.session_id, e.origin, c.reason or e.reason, "done_late"))
        elif c.type == "swap":
            if c.a == c.b:
                raise ChangesError([f"changes[{i}]: a and b are the same slot ({c.a})"])
            a, b = at(c.a, i), at(c.b, i)
            slots[c.a] = [Entry(e.session_id, e.origin, c.reason or e.reason, e.kind) for e in b]
            slots[c.b] = [Entry(e.session_id, e.origin, c.reason or e.reason, e.kind) for e in a]
        else:
            e = take(c.date, c.session, i, "drop")
            dropped.append(Entry(e.session_id, e.origin, c.reason))

    vacated: dict[dt.date, list[Vacated]] = {}
    for pos, entries in slots.items():
        for e in entries:
            if e.origin != pos:
                vacated.setdefault(e.origin.date, []).append(Vacated(e.origin.slot, e.session_id, e.kind, pos, e.reason))
    for e in dropped:
        vacated.setdefault(e.origin.date, []).append(Vacated(e.origin.slot, e.session_id, "dropped", None, e.reason))
    return Applied(slots, vacated)


# ------------------------------------------------------------------ checks for new entries


def positions(c) -> list[Pos]:
    if c.type == "done_late":
        return [c.planned, c.done]
    return [c.from_, c.to] if c.type == "move" else [c.a, c.b] if c.type == "swap" else [c.date]


def new_entries(changes, previous) -> list[tuple[int, object]]:
    """(index, change) for entries not in `previous` (the committed file), matched by content."""
    left = list(previous)
    out = []
    for i, c in enumerate(changes):
        if c in left:
            left.remove(c)
        else:
            out.append((i, c))
    return out


def past_problems(changes, previous, today: dt.date) -> list[str]:
    out = []
    for i, c in new_entries(changes, previous):
        if c.type == "done_late":  # recorded after the fact: the past is the point, the future isn't
            if c.done.date > today:
                out.append(f"changes[{i}]: done {c.done} is after today ({today}); done_late records what already happened")
            continue
        out += [f"changes[{i}]: {p} is before today ({today}); only entries already committed may lie in the past"
                for p in positions(c) if p.date < today]  # fmt: skip
    return out


def committed(path: Path, *, run=subprocess.run) -> tuple:
    """The changes in git HEAD's copy of the file (none if it isn't committed yet)."""
    r = run(["git", "show", f"HEAD:{path.as_posix()}"], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0 or not r.stdout.strip():
        return ()
    return parse(yaml.safe_load(r.stdout))


def main(argv=None, *, run=subprocess.run) -> int:
    from pipeline.plan.expand import expand
    from pipeline.plan.rules import rule_warnings
    from pipeline.plan.schema import PlanLoadError, load_checked, report_load_error

    parser = argparse.ArgumentParser(prog="python -m pipeline.plan.changes", description="Check training/logs/changes.yaml.")
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path)
    parser.add_argument("--changes", type=Path, default=CHANGES_PATH)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)
    try:
        plan, athlete = load_checked(args.plan, args.athlete, changes_path=args.changes)
    except PlanLoadError as e:
        return report_load_error(e)
    changes = plan.changes
    try:
        previous = committed(args.changes, run=run)
    except ChangesError:
        previous = ()
    problems = past_problems(changes, previous, args.today)
    if problems:
        print(f"{args.changes}: REFUSED")
        print("\n".join(f"  {p}" for p in problems))
        return 1
    fresh = new_entries(changes, previous)
    print(f"{args.changes}: OK - {len(changes)} change(s), {len(fresh)} new")
    dates = sorted({p.date for _, c in fresh if c.type != "done_late" for p in positions(c)})  # done is done: no rule check
    if dates:
        days = expand(plan, athlete, dates[0] - dt.timedelta(days=2), dates[-1] + dt.timedelta(days=2))
        for w in rule_warnings(days, athlete, around=set(dates)):
            print(f"  warning: {w}")
    return 0


if __name__ == "__main__":
    from pipeline.plan import changes as _module  # the package's classes, not __main__'s copies

    sys.exit(_module.main())
