"""Grade each planned session against what was done (docs/plan-format.md "Planned against actual").

Endurance sessions are matched to an activity on the same day and sport (derived/activities.csv),
in slot order. Their power steps are compared with the activity's WORK intervals from intervals.icu,
in order:

- held:       the share of steps whose average sits inside the band (+-3%) reaches the
              session's steps_held_pct (default: every step)
- faded:      the last work rep averaged under 95% of its band's low end
- off_target: done, but too few steps held (and not faded)
- done:       done, but nothing to grade on power (no power steps, or no interval data)
- missed:     an activity under half the planned duration
- skipped:    no matching activity
- moved:      training/logs/changes.yaml moved it to another slot, where it is graded ("moved to Tue AM")
- dropped:    changes.yaml dropped it (the note gives the reason)

Strength is graded per prescription against the logged sets (derived/strength.csv):
held (weight, reps and RPE cap all met), under (lighter or short reps), over_rpe (above the
cap), logged (a first "choose" weight, or an amrap/test result) or skipped (lift not logged).
"""

import datetime as dt
from dataclasses import dataclass

from pipeline.plan.expand import ENDURANCE_MODALITIES, Day, ResolvedStep, ScheduledSession
from pipeline.prescribe import LogRow, Prescription

BAND_TOLERANCE = 0.03
FADE_PCT = 0.95
MISSED_BELOW = 0.5
FIELDS = ("date", "slot", "session", "modality", "item", "planned", "actual", "grade", "note")


@dataclass
class Grade:
    date: dt.date
    slot: str
    session: str
    modality: str
    item: str
    planned: str
    actual: str
    grade: str
    note: str = ""
    # for the weekly power index: (actual watts / band midpoint) per graded step
    ratios: tuple[float, ...] = ()

    def row(self) -> dict[str, str]:
        return {k: str(getattr(self, k)) for k in FIELDS} | {"date": self.date.isoformat()}


def power_steps(steps: tuple[ResolvedStep, ...]) -> list[ResolvedStep]:
    """Work, test and steady steps with a watts band, repeats unrolled, in order."""
    out = []
    for s in steps:
        if s.kind == "repeat":
            for _ in range(s.count):
                out.extend(power_steps(s.steps))
        elif s.kind in ("work", "test", "steady") and s.target.watts:
            out.append(s)
    return out


def match_activities(planned: list[ScheduledSession], activities: list[dict]) -> dict[int, dict]:
    """index in `planned` -> activity row. Same day and sport; earliest unmatched activity first."""
    sport_of = {"strength": ("strength", "conditioning")}
    used: set[str] = set()
    out = {}
    for k in sorted(range(len(planned)), key=lambda k: (planned[k].date, planned[k].slot[-2:], planned[k].order)):
        p = planned[k]
        wanted = sport_of.get(p.session.modality, (p.session.modality,))
        cands = sorted(
            (a for a in activities if a["date"] == p.date.isoformat() and a["sport"] in wanted and a["id"] not in used),
            key=lambda a: a["start_local"],
        )
        if cands:
            used.add(cands[0]["id"])
            out[k] = cands[0]
    return out


def grade_endurance(p: ScheduledSession, activity: dict | None, intervals: list[dict] | None) -> Grade:
    planned_min = (p.duration_s or 0) / 60
    base = dict(date=p.date, slot=p.slot, session=p.session_id, modality=p.session.modality, item="session",
                planned=f"{planned_min:.0f} min")  # fmt: skip
    if activity is None:
        return Grade(**base, actual="", grade="skipped", note="no matching activity")
    moving = float(activity.get("moving_min") or 0)
    actual = f"{moving:.0f} min, activity {activity['id']}"
    if planned_min and moving < MISSED_BELOW * planned_min:
        return Grade(**base, actual=actual, grade="missed", note=f"under {MISSED_BELOW:.0%} of the planned duration")

    steps = power_steps(p.steps)
    work = [iv for iv in intervals or [] if iv.get("type") == "WORK" and iv.get("average_watts")]
    if not steps:
        return Grade(**base, actual=actual, grade="done", note="no power targets to grade")
    if len(steps) == 1 and not work and activity.get("avg_watts"):
        work = [{"average_watts": float(activity["avg_watts"])}]  # a single steady block: whole-ride average
    if not work:
        return Grade(**base, actual=actual, grade="done", note="no interval data to grade power")

    pairs = list(zip(steps, work))
    held, ratios, detail = 0, [], []
    for step, iv in pairs:
        lo, hi = step.target.watts
        w = float(iv["average_watts"])
        ratios.append(w / ((lo + hi) / 2))
        ok = lo * (1 - BAND_TOLERANCE) <= w <= hi * (1 + BAND_TOLERANCE)
        held += ok
        detail.append(f"{w:.0f}{'' if ok else '!'}")
    need = p.session.targets.steps_held_pct if p.session.targets and p.session.targets.steps_held_pct else 1.0
    share = held / len(steps)
    last_step, last_iv = pairs[-1]
    note = f"{held}/{len(steps)} steps in band; W: {' '.join(detail)}"
    if len(work) < len(steps):
        note += f"; {len(steps) - len(work)} planned steps had no interval"
    base["planned"] = f"{base['planned']}, {len(steps)} steps"
    reps = len(steps) > 1  # fading needs reps; a single steady block is just off target
    if reps and len(pairs) == len(steps) and float(last_iv["average_watts"]) < FADE_PCT * last_step.target.watts[0]:
        grade = "faded"
        note = f"last rep {float(last_iv['average_watts']):.0f} W < 95% of {last_step.target.watts[0]} W; " + note
    elif share >= need:
        grade = "held"
    else:
        grade = "off_target"
    return Grade(**base, actual=actual, grade=grade, note=note, ratios=tuple(ratios))


def grade_strength(rx: Prescription, logged: list[LogRow]) -> Grade:
    sets = [r for r in logged if r.date == rx.date and r.session == rx.session and r.lift == rx.lift and r.role == rx.role]
    planned = f"{rx.sets} x {rx.reps}" + (f" @ {rx.kg:g} kg" if rx.kg is not None else " @ choose") + (
        f" RPE<={rx.rpe_cap:g}" if rx.rpe_cap is not None else ""
    )
    base = dict(date=rx.date, slot=rx.slot, session=rx.session, modality="strength", item=f"{rx.lift} {rx.role}", planned=planned)
    if not sets:
        return Grade(**base, actual="", grade="skipped", note="not logged")
    top = max(r.kg for r in sets)
    actual = f"{len(sets)} x {','.join(str(r.reps) for r in sets)} @ {top:g} kg" + (
        f" RPE {max(r.rpe for r in sets if r.rpe is not None):g}" if any(r.rpe is not None for r in sets) else ""
    )
    if rx.kg is None:
        return Grade(**base, actual=actual, grade="logged", note="first weight logged; it's next week's basis")
    if rx.role in ("amrap", "test"):
        return Grade(**base, actual=actual, grade="logged", note="test result, not graded")
    issues = []
    if top < rx.kg - 1e-6:
        issues.append(f"{rx.kg - top:g} kg lighter")
    if any(r.reps < rx.reps for r in sets):
        issues.append(f"reps under {rx.reps}")
    if len(sets) < rx.sets:
        issues.append(f"{len(sets)} of {rx.sets} sets")
    over = rx.rpe_cap is not None and any(r.rpe is not None and r.rpe > rx.rpe_cap for r in sets)
    if issues:
        return Grade(**base, actual=actual, grade="under", note="; ".join(issues))
    if over:
        return Grade(**base, actual=actual, grade="over_rpe", note=f"above RPE {rx.rpe_cap:g}")
    note = "RPE not logged" if all(r.rpe is None for r in sets) else ""
    return Grade(**base, actual=actual, grade="held", note=note)


def vacated_grades(days: list[Day], sessions: dict, until: dt.date | None = None) -> list[Grade]:
    """One row per planned session that changes.yaml moved away or dropped, on its original date."""
    out = []
    for d in days:
        if until is not None and d.date > until:
            continue
        for v in d.vacated:
            out.append(Grade(date=d.date, slot=v.slot, session=v.session_id, modality=sessions[v.session_id].modality, item="session", planned="",
                             actual="", grade=v.kind, note=v.note(d.date)))  # fmt: skip
    return out


def grade_days(planned: list[ScheduledSession], activities: list[dict], intervals_for, rx: list[Prescription],
               logged: list[LogRow]) -> list[Grade]:  # fmt: skip
    """`intervals_for(activity_id) -> list[dict] | None` fetches WORK/RECOVERY intervals."""
    endurance = [s for s in planned if s.session.modality in ENDURANCE_MODALITIES]
    matched = match_activities(endurance, activities)
    grades = []
    for i, s in enumerate(endurance):
        a = matched.get(i)
        grades.append(grade_endurance(s, a, intervals_for(a["id"]) if a else None))
    grades += [grade_strength(p, logged) for p in rx]
    position = {(s.date, s.session_id): k for k, s in enumerate(planned)}  # plan order: am before pm
    return sorted(grades, key=lambda g: position.get((g.date, g.session), 0))  # stable within a session
