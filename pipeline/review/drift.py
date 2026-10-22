"""The Sunday drift check: one record per week, plus flags from training/design-rules.md.

Flags raised:
- load:     done TSS outside the week's weekly_tss, once the week is over (advisory while
            targets are provisional)
- sessions: two or more endurance sessions missed or skipped, or any faded
- recovery: two or more red days, or three or more amber; 7-day HRV 5% under its 28-day
            baseline; 7-day resting HR 3 bpm over its baseline
- bench:    e1RM more than one rep (line/30) under the strength line; at a checkpoint
            that's the cut rule, "next C event is cut first"
- cut rule: falling power and rising fatigue two weeks running (power index = actual /
            target watts across graded steps; fatigue = ATL - CTL)
"""

import datetime as dt
import statistics
from dataclasses import dataclass, field

from pipeline.plan.schema import Plan
from pipeline.prescribe import LogRow, latest_e1rm
from pipeline.review.compliance import Grade

WEEKLY_FIELDS = (
    "week_end", "week_start", "block", "week", "planned_tss", "actual_tss", "tss_target", "held", "faded",
    "off_target", "done", "missed", "skipped", "strength_held", "strength_under", "strength_over_rpe",
    "strength_logged", "strength_skipped", "green", "amber", "red", "no_call", "hrv_7d", "hrv_28d", "rhr_7d",
    "rhr_28d", "ctl", "atl", "form", "power_index", "bench_e1rm", "bench_line", "bench_gap_kg", "flags",
)  # fmt: skip


@dataclass
class WeekReview:
    record: dict[str, str]
    flags: list[str] = field(default_factory=list)


def _f(v) -> float | None:
    return float(v) if v not in (None, "") else None


def _fmt(x, digits=1) -> str:
    return "" if x is None else f"{round(x, digits):g}"


def week_markdown(week: WeekReview, grades: list[Grade], graded_to: dt.date) -> str:
    """derived/week.md: the drift report to read."""
    r = week.record
    start, end = dt.date.fromisoformat(r["week_start"]), dt.date.fromisoformat(r["week_end"])
    partial = f" (graded to {graded_to:%a} {graded_to.day} {graded_to:%b})" if graded_to < end else ""
    out = [f"# Week {start.day} {start:%b} to {end.day} {end:%b %Y}{partial}", ""]
    if r["block"]:
        out += [f"{r['block']}" + (f", week {r['week']}" if r["week"] else ""), ""]
    out += ["## Flags"] + ([f"- {f}" for f in week.flags] or ["None."]) + [""]
    target = f" (target {r['tss_target']})" if r["tss_target"] else ""
    out += ["## Load", f"{r['actual_tss']} TSS done of {r['planned_tss']} planned{target}.", ""]
    counts = ", ".join(f"{k.replace('_', ' ')} {r[k]}" for k in ("held", "faded", "off_target", "done", "missed", "skipped") if r[k] != "0")
    strength = ", ".join(f"{k} {r['strength_' + k]}" for k in ("held", "under", "over_rpe", "logged", "skipped") if r["strength_" + k] != "0")
    out += ["## Sessions", f"Endurance: {counts or 'none graded'}. Strength: {strength or 'none graded'}.", ""]
    misses = [g for g in grades if g.grade not in ("held", "done", "logged", "pending")]
    out += [f"- **{g.grade}** {g.date:%a} {g.session}" + (f" {g.item}" if g.modality == "strength" else "") + (f": {g.note}" if g.note else "")
            for g in misses]  # fmt: skip
    if misses:
        out.append("")
    out += ["## Recovery",
            f"Readiness: green {r['green']}, amber {r['amber']}, red {r['red']}, no call {r['no_call']}.",
            f"HRV 7-day {r['hrv_7d'] or '-'} vs 28-day {r['hrv_28d'] or '-'}; resting HR 7-day {r['rhr_7d'] or '-'} vs 28-day {r['rhr_28d'] or '-'}.",
            f"CTL {r['ctl'] or '-'}, ATL {r['atl'] or '-'}, form {r['form'] or '-'}.", ""]  # fmt: skip
    if r["bench_e1rm"]:
        out += ["## Bench", f"e1RM {r['bench_e1rm']} kg against a line of {r['bench_line']} kg ({float(r['bench_gap_kg']):+g} kg).", ""]
    if r["power_index"]:
        out += [f"Power index (actual / target watts across graded steps): {r['power_index']}.", ""]
    return "\n".join(out)


def strength_line_at(plan: Plan, lift: str, day: dt.date) -> tuple[float, dt.date | None] | None:
    """Linear interpolation of the lift's strength line at `day`, and the checkpoint date if `day` is one."""
    line = next((s for s in plan.strength_lines if s.lift == lift), None)
    if line is None:
        return None
    pts = line.checkpoints
    if day <= pts[0][0]:
        return pts[0][1], pts[0][0] if day == pts[0][0] else None
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if d0 <= day <= d1:
            frac = (day - d0).days / (d1 - d0).days
            return v0 + frac * (v1 - v0), d1 if day == d1 else None
    return pts[-1][1], None


def review_week(plan: Plan, week_start: dt.date, week_end: dt.date, planned_tss: float, grades: list[Grade],
                activities: list[dict], wellness: list[dict], logged: list[LogRow], previous: list[dict],
                graded_to: dt.date | None = None) -> WeekReview:  # fmt: skip
    """`graded_to` < week_end means the week is still running: load isn't judged yet and
    readiness counts only the days so far."""
    graded_to = graded_to or week_end
    complete = graded_to >= week_end
    block = next((b for b in plan.blocks if b.start <= week_end <= b.end), None)
    week = None
    if block and block.weeks:
        week = max((w for w in block.weeks if w.start <= week_end), key=lambda w: w.start, default=None)
    lo, hi = week_start.isoformat(), week_end.isoformat()
    flags: list[str] = []

    # load
    actual_tss = sum(_f(a.get("load")) or 0 for a in activities if lo <= a["date"] <= hi)
    target = week.weekly_tss if week and week.weekly_tss else (block.weekly_tss if block else None)
    provisional = bool(block and block.targets_provisional)
    if target and complete:
        if actual_tss < target[0] or actual_tss > target[1]:
            where = "under" if actual_tss < target[0] else "over"
            flags.append(f"load: {actual_tss:.0f} TSS done, {where} the {target[0]:g}-{target[1]:g} target"
                         + (" (advisory: targets provisional)" if provisional else ""))  # fmt: skip

    # sessions
    endurance = [g for g in grades if g.modality != "strength"]
    strength = [g for g in grades if g.modality == "strength"]
    count = lambda gs, k: sum(g.grade == k for g in gs)  # noqa: E731
    gone = count(endurance, "missed") + count(endurance, "skipped")
    if gone >= 2:
        flags.append(f"sessions: {gone} endurance sessions missed or skipped")
    for g in endurance:
        if g.grade == "faded":
            flags.append(f"sessions: {g.session} on {g.date:%a %d %b} faded ({g.note.split(';')[0]})")

    # recovery
    week_rows = [r for r in wellness if lo <= r["date"] <= hi]
    base_from = (week_start - dt.timedelta(days=28)).isoformat()
    base_rows = [r for r in wellness if base_from <= r["date"] < lo]
    states = [r.get("readiness", "") for r in week_rows]
    green, amber, red = states.count("green"), states.count("amber"), states.count("red")
    no_call = (graded_to - week_start).days + 1 - green - amber - red
    if red >= 2:
        flags.append(f"recovery: {red} red days")
    if amber >= 3:
        flags.append(f"recovery: {amber} amber days")

    def mean(rows, key):
        vals = [_f(r.get(key)) for r in rows if _f(r.get(key)) is not None]
        return statistics.fmean(vals) if vals else None

    hrv7, hrv28 = mean(week_rows, "hrv"), mean(base_rows, "hrv")
    rhr7, rhr28 = mean(week_rows, "resting_hr"), mean(base_rows, "resting_hr")
    if hrv7 is not None and hrv28 is not None and hrv7 < 0.95 * hrv28:
        flags.append(f"recovery: 7-day HRV {hrv7:.0f} is {1 - hrv7 / hrv28:.0%} under its 28-day baseline {hrv28:.0f}")
    if rhr7 is not None and rhr28 is not None and rhr7 > rhr28 + 3:
        flags.append(f"recovery: 7-day resting HR {rhr7:.0f} is {rhr7 - rhr28:.0f} bpm over its baseline {rhr28:.0f}")
    last = max(week_rows, key=lambda r: r["date"], default={})
    ctl, atl = _f(last.get("ctl")), _f(last.get("atl"))
    fatigue = atl - ctl if ctl is not None and atl is not None else None

    # power index
    ratios = [x for g in endurance for x in g.ratios]
    power = statistics.fmean(ratios) if ratios else None

    # bench against its line
    bench = latest_e1rm([r for r in logged if r.lift == "bench_press" and r.date <= week_end])
    line = strength_line_at(plan, "bench_press", week_end)
    gap = None
    if bench and line:
        gap = bench.e1rm - line[0]
        one_rep = line[0] / 30
        checkpoint = next((d for d, _ in (next(s for s in plan.strength_lines if s.lift == "bench_press").checkpoints)
                           if week_start <= d <= week_end), None)  # fmt: skip
        if gap < -one_rep:
            if checkpoint:
                flags.append(f"bench: checkpoint {checkpoint:%d %b} missed by more than one rep (e1RM {bench.e1rm:.1f} vs "
                             f"{line[0]:.1f}); cut rule: the next C event is cut first")  # fmt: skip
            else:
                flags.append(f"bench: e1RM {bench.e1rm:.1f} is {-gap:.1f} kg under the line ({line[0]:.1f})")

    # cut rule: two weeks running of falling power and rising fatigue
    hist = [r for r in previous if r["week_end"] < hi][-2:]
    if len(hist) == 2 and power is not None and fatigue is not None:
        p = [_f(r["power_index"]) for r in hist] + [power]
        f = [(_f(r["atl"]) - _f(r["ctl"])) if _f(r["atl"]) is not None and _f(r["ctl"]) is not None else None for r in hist] + [fatigue]
        if None not in p and None not in f and p[0] > p[1] > p[2] and f[0] < f[1] < f[2]:
            flags.append("cut rule: power falling and fatigue rising two weeks running; the next C event is cut first")

    record = {
        "week_end": hi, "week_start": lo, "block": block.id if block else "", "week": week.label if week else "",
        "planned_tss": f"{planned_tss:.0f}", "actual_tss": f"{actual_tss:.0f}", "tss_target": f"{target[0]:g}-{target[1]:g}" if target else "",
        "held": count(endurance, "held"), "faded": count(endurance, "faded"), "off_target": count(endurance, "off_target"),
        "done": count(endurance, "done"), "missed": count(endurance, "missed"), "skipped": count(endurance, "skipped"),
        "strength_held": count(strength, "held"), "strength_under": count(strength, "under"),
        "strength_over_rpe": count(strength, "over_rpe"), "strength_logged": count(strength, "logged"),
        "strength_skipped": count(strength, "skipped"), "green": green, "amber": amber, "red": red, "no_call": no_call,
        "hrv_7d": _fmt(hrv7), "hrv_28d": _fmt(hrv28), "rhr_7d": _fmt(rhr7), "rhr_28d": _fmt(rhr28),
        "ctl": _fmt(ctl), "atl": _fmt(atl), "form": _fmt(-fatigue if fatigue is not None else None),
        "power_index": _fmt(power, 3), "bench_e1rm": _fmt(bench.e1rm if bench else None),
        "bench_line": _fmt(line[0] if line else None), "bench_gap_kg": _fmt(gap), "flags": " | ".join(flags),
    }  # fmt: skip
    return WeekReview({k: str(v) for k, v in record.items()}, flags)
