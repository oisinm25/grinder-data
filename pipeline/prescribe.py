"""Strength prescriptions: kg and reps per group for every strength session in a date range.

Basis, first that applies (docs/plan-format.md "Progression rules"):

1. last   - the same lift in the same session and slot, within 14 days
2. start  - the exercise's `start_kg`: a deliberate design choice for a slot's first
            session, so it beats a calculated e1RM
3. e1rm   - the lift's latest e1RM from derived/strength.csv
4. choose - "choose a weight at the RPE cap and log it"

e1RM = kg x (1 + (reps + (10 - RPE)) / 30); a missing RPE counts as 10 (and the reason
says so). The latest e1RM comes from the most recent top/amrap/test set of the lift,
falling back to the most recent set with an RPE, then any set.

Rules with a `last` basis:
- linear: every rep done at or below max_rpe -> last + increment; a miss -> repeat;
  `reset_after_misses` misses in a row -> drop `reset_pct`
- double: same weight, aim for one more rep than the weakest set; every set at the top
  of the range -> + increment, back to the bottom
- rpe, pct_e1rm: the e1RM of those last sets, then the rule's formula
- no rule: repeat the last weight

With an `e1rm` basis, linear, double and by-feel lifts start at the `rpe` formula,
e1RM / (1 + (reps + (10 - RPE)) / 30), at the rule's max_rpe or the group's rpe_cap
(whichever is lower); rpe uses its target_rpe and pct_e1rm its pct. `wave` is defined on
e1RM, so it skips `last`: week k of the block takes pcts[k % n] of the e1RM as of the
block start, plus e1rm_step_kg per completed cycle (`start_kg`, if given, sets only the
lift's very first session in that slot, since a wave never builds on last week's weight).

Backoffs are pct_of_top x the suggested top. Readiness (derived/wellness.csv) then
applies: green as is; amber holds last weight, no increment, RPE cap 8; red is 90% of last
weight, RPE cap 7 ("last" = the `last` basis weight, else the suggestion itself).
`readiness_exempt` sessions ignore amber; red still applies. Weights round to the
athlete's rounding (2.5 kg).

    python -m pipeline.prescribe --from 2026-10-26 --days 7
"""

import argparse
import csv
import datetime as dt
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.athlete import Athlete
from pipeline.ingest.strength import TOP_ROLES, e1rm
from pipeline.plan.expand import expand
from pipeline.plan.schema import (
    Block,
    Double,
    Exercise,
    Group,
    Linear,
    PctE1rm,
    Plan,
    PlanLoadError,
    RpeRule,
    Wave,
    load_checked,
    report_load_error,
)

BASIS_WINDOW_DAYS = 14
FIELDS = ("date", "session", "lift", "role", "sets", "reps", "kg", "rpe_cap", "basis", "reason")


@dataclass(frozen=True)
class LogRow:
    date: dt.date
    session: str
    lift: str
    role: str
    kg: float
    reps: int
    rpe: float | None
    set_no: int = 1

    @property
    def e1rm(self) -> float:
        return e1rm(self.kg, self.reps, self.rpe)


@dataclass
class Prescription:
    date: dt.date
    session: str
    slot: str
    lift: str
    role: str
    sets: int
    reps: int
    kg: float | None
    rpe_cap: float | None
    basis: str  # last | e1rm | start | choose | top
    reason: str
    readiness: str = ""  # green | amber | red | "" (not known)
    last_kg: float | None = field(default=None, repr=False)

    def row(self) -> dict[str, str]:
        return {
            "date": self.date.isoformat(),
            "session": self.session,
            "lift": self.lift,
            "role": self.role,
            "sets": str(self.sets),
            "reps": str(self.reps),
            "kg": "" if self.kg is None else f"{self.kg:g}",
            "rpe_cap": "" if self.rpe_cap is None else f"{self.rpe_cap:g}",
            "basis": self.basis,
            "reason": self.reason,
        }


# ------------------------------------------------------------------ inputs


def load_history(path: Path) -> list[LogRow]:
    """derived/strength.csv, as written by `python -m pipeline.ingest`."""
    with open(path, newline="", encoding="utf-8") as f:
        return [
            LogRow(
                dt.date.fromisoformat(r["date"]),
                r["session"],
                r["lift"],
                r["role"],
                float(r["kg"]),
                int(r["reps"]),
                float(r["rpe"]) if r["rpe"] else None,
                int(r.get("set_no") or 1),
            )
            for r in csv.DictReader(f)
        ]


def load_readiness(path: Path) -> dict[dt.date, str]:
    """date -> green | amber | red from derived/wellness.csv (blank days left out)."""
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {dt.date.fromisoformat(r["date"]): r["readiness"] for r in csv.DictReader(f) if r.get("readiness")}


# ------------------------------------------------------------------ helpers


def round_to(kg: float, step: float) -> float:
    return round(step * math.floor(kg / step + 0.5), 4)


def rpe_weight(e1rm_kg: float, reps: int, rpe: float) -> float:
    return e1rm_kg / (1 + (reps + (10 - rpe)) / 30)


def _day(d: dt.date) -> str:
    return f"{d.day} {d:%b}"


def describe_set(r: LogRow) -> str:
    rpe = f" @{r.rpe:g}" if r.rpe is not None else " (RPE not logged, treated as 10)"
    return f"{r.kg:g}x{r.reps}{rpe} on {_day(r.date)}"


def target_reps(g: Group) -> int:
    return g.reps if isinstance(g.reps, int) else g.reps[0]


def rpe_cap_for(g: Group, rule) -> float | None:
    caps = [g.rpe_cap]
    if isinstance(rule, (Linear, Double)):
        caps.append(rule.max_rpe)
    if isinstance(rule, RpeRule):
        caps.append(rule.target_rpe)
    caps = [c for c in caps if c is not None]
    return min(caps) if caps else None


def latest_e1rm(rows: list[LogRow]) -> LogRow | None:
    """The best-e1RM set on the latest date with a top/amrap/test set, else with a logged RPE, else any."""
    pool = [r for r in rows if r.role in TOP_ROLES] or [r for r in rows if r.rpe is not None] or rows
    if not pool:
        return None
    latest = max(r.date for r in pool)
    return max((r for r in pool if r.date == latest), key=lambda r: r.e1rm)


def last_exposure(rows: list[LogRow], role: str) -> list[LogRow]:
    """The sets of the latest date in `rows`, of `role` if any were logged with it."""
    if not rows:
        return []
    latest = max(r.date for r in rows)
    day = [r for r in rows if r.date == latest]
    return sorted([r for r in day if r.role == role] or day, key=lambda r: r.set_no)


class Prescriber:
    def __init__(self, plan: Plan, athlete: Athlete, history: list[LogRow], readiness: dict[dt.date, str]):
        self.plan, self.athlete = plan, athlete
        self.history = sorted(history, key=lambda r: (r.date, r.set_no))
        self.readiness = readiness
        self.step = athlete.strength.rounding_kg
        self._slots: dict[dt.date, dict[str, str]] = {}

    def slot_of(self, r: LogRow) -> str | None:
        """The plan's slot for r.session on r.date (None if it wasn't scheduled that day). A session
        moved by changes.yaml keeps its home slot, so its history stays with the same session and slot."""
        if r.date not in self._slots:
            self._slots[r.date] = {s.session_id: s.home_slot for d in expand(self.plan, self.athlete, r.date, r.date) for s in d.sessions}
        return self._slots[r.date].get(r.session)

    def block_of(self, d: dt.date) -> Block:
        return next(b for b in self.plan.blocks if b.start <= d <= b.end)

    # ------------------------------------------------------------------ main loop

    def run(self, start: dt.date, end: dt.date) -> list[Prescription]:
        out = []
        for day in expand(self.plan, self.athlete, start, end):
            for s in day.sessions:
                if s.session.modality != "strength":
                    continue
                status = self.readiness.get(day.date, "")
                for ex in s.session.exercises:
                    out.extend(self.exercise(day.date, s.session_id, s.slot, ex, status, s.session.readiness_exempt, s.home_slot))
        return out

    def exercise(self, date, sid, slot, ex: Exercise, status: str, exempt: bool, home: str | None = None) -> list[Prescription]:
        """`slot` is where the session is today; `home` the plan's slot for it (different when moved),
        whose history is the basis. Readiness (`status`) is today's."""
        rule = self.plan.progressions.get(ex.progression) if ex.progression else None
        prior = [r for r in self.history if r.lift == ex.lift and r.date < date]
        since = date - dt.timedelta(days=BASIS_WINDOW_DAYS)
        home = home or slot
        same = [r for r in prior if r.session == sid and r.date >= since and self.slot_of(r) in (home, None)]
        same_all = [r for r in prior if r.session == sid and self.slot_of(r) in (home, None)]

        results: list[Prescription | None] = [None] * len(ex.groups)
        top: Prescription | None = None
        for i, g in enumerate(ex.groups):  # everything but backoffs first
            if g.role == "backoff":
                continue
            p = self.group(date, sid, slot, ex, g, rule, prior, same, same_all)
            self.apply_readiness(p, status, exempt)
            results[i] = p
            if g.role == "top" and top is None:
                top = p
        for i, g in enumerate(ex.groups):
            if g.role == "backoff":
                results[i] = self.backoff(date, sid, slot, ex, g, top, status)
        return results

    # ------------------------------------------------------------------ one group

    def group(self, date, sid, slot, ex, g: Group, rule, prior, same, same_all) -> Prescription:
        reps = target_reps(g)
        cap = rpe_cap_for(g, rule)

        def made(kg, basis, reason, reps=reps, last=None, cap=cap):
            kg = None if kg is None else round_to(kg, self.step)
            return Prescription(date, sid, slot, ex.lift, g.role, g.sets, reps, kg, cap, basis, reason, last_kg=last)

        last = last_exposure(same, g.role)
        e1 = latest_e1rm(prior)

        if isinstance(rule, Wave):
            return self.wave(date, sid, ex, g, rule, prior, same_all, made)

        if last and isinstance(rule, Linear):
            return self.linear(g, rule, last, same_all, made)
        if last and isinstance(rule, Double):
            return self.double(g, rule, last, made)
        if last and isinstance(rule, (RpeRule, PctE1rm)):
            src = max(last, key=lambda r: r.e1rm)
            return self.from_e1rm(g, rule, src.e1rm, f"e1RM {src.e1rm:.0f} from last {describe_set(src)}", made, "last", last_kg=max(r.kg for r in last))
        if last and rule is None:
            w = max(r.kg for r in last)
            return made(w, "last", f"no progression rule: repeat last {describe_set(last[0])} -> {w:g} x {reps}", last=w)

        r0 = rule.rep_range[0] if isinstance(rule, Double) else reps
        if ex.start_kg is not None:  # the plan's deliberate first weight beats a calculated e1RM
            return made(ex.start_kg, "start",
                        f"start_kg {ex.start_kg:g} x {r0}: no {sid} history in this slot within {BASIS_WINDOW_DAYS} days",
                        reps=r0)  # fmt: skip

        if e1 is not None and (rule is not None or cap is not None):
            return self.from_e1rm(g, rule, e1.e1rm, f"e1RM {e1.e1rm:.0f} from {describe_set(e1)}", made, "e1rm")

        at = f"RPE {cap:g}" if cap is not None else "a weight you can move well"
        return made(None, "choose", f"no history, e1RM or start_kg: choose a weight for {r0} at {at} and log it", reps=r0)

    def from_e1rm(self, g, rule, e1rm_kg, source, made, basis, last_kg=None) -> Prescription:
        if isinstance(rule, PctE1rm):
            kg = round_to(rule.pct * e1rm_kg, self.step)
            return made(kg, basis, f"{source} -> {rule.pct:.0%} = {kg:g} x {target_reps(g)}", last=last_kg)
        reps = rule.rep_range[0] if isinstance(rule, Double) else target_reps(g)
        at = rule.target_rpe if isinstance(rule, RpeRule) else rpe_cap_for(g, rule)
        kg = round_to(rpe_weight(e1rm_kg, reps, at), self.step)
        return made(kg, basis, f"{source} -> {kg:g} x {reps} @RPE {at:g}", reps=reps, last=last_kg)

    def linear(self, g, rule: Linear, last, same_all, made) -> Prescription:
        reps = target_reps(g)
        w = max(r.kg for r in last)
        desc = describe_set(max(last, key=lambda r: r.kg)) + (f" ({len(last)} sets)" if len(last) > 1 else "")

        def miss_why(rows):
            why = []
            if any(r.reps < reps for r in rows):
                why.append(f"under {reps} reps")
            if any(r.rpe is None for r in rows):
                why.append("RPE not logged, treated as 10")
            if any(r.rpe is not None and r.rpe > rule.max_rpe for r in rows):
                why.append(f"above RPE {rule.max_rpe:g}")
            return why

        why = miss_why(last)
        if not why:
            kg = w + rule.increment_kg
            return made(kg, "last", f"last {desc}: every rep at or below RPE {rule.max_rpe:g} -> +{rule.increment_kg:g} = {round_to(kg, self.step):g} x {reps}", last=w)

        misses = 0
        for d in sorted({r.date for r in same_all}, reverse=True):
            if miss_why([r for r in same_all if r.date == d and r.role == g.role] or [r for r in same_all if r.date == d]):
                misses += 1
            else:
                break
        if misses >= rule.reset_after_misses:
            kg = w * (1 - rule.reset_pct)
            return made(kg, "last", f"last {desc}: {misses} misses in a row -> reset -{rule.reset_pct:.0%} = {round_to(kg, self.step):g} x {reps}", last=w)
        return made(w, "last", f"last {desc}: miss ({', '.join(why)}) -> repeat {w:g} x {reps}", last=w)

    def double(self, g, rule: Double, last, made) -> Prescription:
        lo, hi = rule.rep_range
        w = max(r.kg for r in last)
        done = [r.reps for r in last]
        shown = f"{w:g} x {','.join(map(str, done))} on {_day(last[0].date)}"
        if len(done) >= g.sets and min(done) >= hi:
            kg = w + rule.increment_kg
            return made(kg, "last", f"last {shown}: every set reached {hi} -> +{rule.increment_kg:g} = {round_to(kg, self.step):g} x {lo}", reps=lo, last=w)
        weakest = min(done)
        aim = max(lo, min(weakest + 1, hi))
        short = f" ({len(done)} of {g.sets} sets logged)" if len(done) < g.sets else ""
        return made(w, "last", f"last {shown}{short}: weakest {weakest} -> same weight, aim {aim}", reps=aim, last=w)

    def wave(self, date, sid, ex, g, rule: Wave, prior, same_all, made) -> Prescription:
        block = self.block_of(date)
        before_block = [r for r in prior if r.date < block.start]
        ref = latest_e1rm(before_block) or latest_e1rm(prior)
        reps = target_reps(g)
        # A wave never builds on last week's weight, so start_kg only sets its very first session.
        if ex.start_kg is not None and not same_all:
            return made(ex.start_kg, "start", f"start_kg {ex.start_kg:g} x {reps}: first {sid} session for this lift")
        if ref is None:
            return made(None, "choose", f"wave needs an e1RM; none logged: choose a fast weight for {reps} and log it")
        k = sum(
            1
            for d in expand(self.plan, self.athlete, block.start, date - dt.timedelta(days=1))
            for s in d.sessions
            if s.session_id == sid and any(e.lift == ex.lift for e in s.session.exercises or [])
        )
        n = len(rule.pcts)
        pct, cycles = rule.pcts[k % n], k // n
        base = ref.e1rm + cycles * rule.e1rm_step_kg
        step_note = f" + {cycles * rule.e1rm_step_kg:g} after {cycles} cycle(s)" if cycles else ""
        kg = round_to(pct * base, self.step)
        return made(kg, "e1rm", f"wave week {k % n + 1} of {n}: {pct:.0%} of e1RM {ref.e1rm:.0f}{step_note} (from {describe_set(ref)}) -> {kg:g} x {reps}")

    # ------------------------------------------------------------------ readiness and backoffs

    def apply_readiness(self, p: Prescription, status: str, exempt: bool) -> None:
        p.readiness = status
        if status == "amber" and exempt:
            p.reason += "; amber ignored (readiness-exempt test)"
            return
        if p.kg is None:
            if status in ("amber", "red"):
                cap = 8 if status == "amber" else 7
                p.rpe_cap = min(p.rpe_cap or 10, cap)
                p.reason += f"; {status}: RPE cap {cap}"
            return
        if status == "amber":
            held = p.last_kg if p.last_kg is not None else p.kg
            p.kg = round_to(held, self.step)
            p.rpe_cap = min(p.rpe_cap or 10, 8)
            src = f"hold last {held:g}" if p.last_kg is not None else f"hold {held:g}"
            p.reason += f"; amber: {src}, no increment, RPE cap 8 -> {p.kg:g}"
        elif status == "red":
            base = p.last_kg if p.last_kg is not None else p.kg
            p.kg = round_to(0.9 * base, self.step)
            p.rpe_cap = min(p.rpe_cap or 10, 7)
            p.reason += f"; red: 90% of {'last ' if p.last_kg is not None else ''}{base:g} -> {p.kg:g}, RPE cap 7"
        elif not status:
            p.reason += "; readiness not known yet"

    def backoff(self, date, sid, slot, ex, g: Group, top: Prescription | None, status: str) -> Prescription:
        reps = target_reps(g)
        if top is None or top.kg is None:
            return Prescription(date, sid, slot, ex.lift, g.role, g.sets, reps, None, g.rpe_cap, "top",
                                f"{g.pct_of_top:.0%} of whatever top set you choose", readiness=status)  # fmt: skip
        kg = round_to(g.pct_of_top * top.kg, self.step)
        return Prescription(date, sid, slot, ex.lift, g.role, g.sets, reps, kg, g.rpe_cap, "top",
                            f"{g.pct_of_top:.0%} of top {top.kg:g} -> {kg:g} x {reps}", readiness=status)  # fmt: skip


def prescribe(plan, athlete, history, readiness, start, end) -> list[Prescription]:
    return Prescriber(plan, athlete, history, readiness).run(start, end)


# ------------------------------------------------------------------ output


def write_prescriptions(path: Path, items: list[Prescription], start: dt.date, end: dt.date) -> int:
    """Replace the rows dated start..end, keep the rest. Returns the total."""
    lo, hi = start.isoformat(), end.isoformat()
    kept = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            kept = [r for r in csv.DictReader(f) if not lo <= r["date"] <= hi]
    rows = sorted(kept + [p.row() for p in items], key=lambda r: r["date"])  # stable: keeps session order
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
    return len(rows)


def format_prescriptions(items: list[Prescription]) -> list[str]:
    lines = []
    width = max((len(p.lift) for p in items), default=0)
    key = None
    for p in items:
        if (p.date, p.session) != key:
            key = (p.date, p.session)
            state = p.readiness or "not known yet"
            lines.append(f"{p.date:%a %Y-%m-%d}  {p.session} ({p.slot})  readiness: {state}")
        kg = f"{p.kg:g} kg" if p.kg is not None else "choose"
        cap = f"RPE<={p.rpe_cap:g}" if p.rpe_cap is not None else ""
        lines.append(f"  {p.lift:<{width}}  {p.role:<7} {p.sets} x {p.reps:<3} {kg:>9}  {cap:<9} {p.reason}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.prescribe", description="Suggest strength weights by date.")
    parser.add_argument("--from", dest="start", type=dt.date.fromisoformat, default=dt.date.today(), help="first date (default: today)")
    span = parser.add_mutually_exclusive_group()
    span.add_argument("--days", type=int, default=7)
    span.add_argument("--to", dest="end", type=dt.date.fromisoformat)
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path, help="default: athlete.yaml beside the plan")
    parser.add_argument("--strength", type=Path, default=Path("derived/strength.csv"))
    parser.add_argument("--wellness", type=Path, default=Path("derived/wellness.csv"))
    parser.add_argument("--out", type=Path, default=Path("derived/prescriptions.csv"))
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    try:
        plan, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        return report_load_error(e)
    if not args.strength.exists():
        print(f"error: {args.strength} not found; run python -m pipeline.ingest first", file=sys.stderr)
        return 2

    end = args.end or args.start + dt.timedelta(days=args.days - 1)
    items = prescribe(plan, athlete, load_history(args.strength), load_readiness(args.wellness), args.start, end)
    if not items:
        print(f"no strength sessions between {args.start} and {end}")
        return 0
    print("\n".join(format_prescriptions(items)))
    total = write_prescriptions(args.out, items, args.start, end)
    print(f"\nwrote {len(items)} prescriptions to {args.out} ({total} rows total)")
    if not args.wellness.exists():
        print(f"note: {args.wellness} not found, so no readiness was applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
