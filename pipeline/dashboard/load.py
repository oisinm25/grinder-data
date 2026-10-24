"""Pedestal's load model: daily load, CTL, ATL and form.

**Daily load** is the sum over the day's activities (derived/activities.csv) of:

- power TSS (`power_load`) when the activity had power, else hrTSS (`hr_load`), else
  intervals.icu's `icu_training_load`, else 0;
- plus, for grinding and sailing activities, a **Grind Load correction**, because hrTSS
  under-reads short upper-body efforts: `GRIND_PER_WORK_MIN` TSS per Grind Load work minute
  plus `GRIND_PER_HARD_EFFORT` TSS per effort at >= 85% LTHR (Grind Load as defined in
  pipeline.review.debrief, from derived/activity_metrics.csv). Without metrics, no correction.

**CTL** (fitness) and **ATL** (fatigue) are exponentially weighted averages of daily load
with time constants of 42 and 7 days, the standard form:

    CTL_today = CTL_yesterday + (load_today - CTL_yesterday) x (1 - exp(-1/42))
    ATL_today = ATL_yesterday + (load_today - ATL_yesterday) x (1 - exp(-1/7))

and **form** = CTL - ATL (same day). Pedestal's history starts when ingest started, so the model
is seeded from intervals.icu's own CTL and ATL on the day before its first day (from
derived/wellness.csv) rather than ramping up from zero. intervals.icu's CTL is kept alongside
for comparison.

The **planned CTL path** runs the same model forward from today with the plan's daily TSS:
the sessions' `targets.tss` in designed blocks, and `weekly_tss` midpoint / 7 per day in
design-pending ones.
"""

import datetime as dt
import math
from dataclasses import dataclass

from pipeline.athlete import Athlete
from pipeline.plan.expand import expand
from pipeline.plan.schema import Plan

CTL_DAYS, ATL_DAYS = 42, 7
GRIND_PER_WORK_MIN = 1.0
GRIND_PER_HARD_EFFORT = 2.0
CORRECTED_SPORTS = ("grinding", "sail", "sail_or_other")


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def activity_load(a: dict, metrics: dict | None = None) -> tuple[float, float, str]:
    """(base load, Grind Load correction, source) for one activities.csv row."""
    for field, source in (("power_load", "power"), ("hr_load", "hr"), ("load", "intervals.icu")):
        v = _f(a.get(field))
        if v is not None:
            base = v
            break
    else:
        base, source = 0.0, "none"
    correction = 0.0
    if a.get("sport") in CORRECTED_SPORTS and metrics:
        work, hard = _f(metrics.get("grind_work_min")), _f(metrics.get("grind_efforts_85"))
        correction = (work or 0) * GRIND_PER_WORK_MIN + (hard or 0) * GRIND_PER_HARD_EFFORT
    return base, round(correction, 1), source


def daily_loads(activities: list[dict], metrics: dict[str, dict]) -> dict[dt.date, float]:
    out: dict[dt.date, float] = {}
    for a in activities:
        base, corr, _ = activity_load(a, metrics.get(a["id"]))
        day = dt.date.fromisoformat(a["date"])
        out[day] = out.get(day, 0.0) + base + corr
    return out


@dataclass(frozen=True)
class Point:
    date: dt.date
    load: float
    ctl: float
    atl: float

    @property
    def form(self) -> float:
        return self.ctl - self.atl


def run_model(loads: dict[dt.date, float], start: dt.date, end: dt.date, ctl0: float = 0.0, atl0: float = 0.0) -> list[Point]:
    """CTL/ATL for every day start..end; ctl0/atl0 are the values on the day before start."""
    kc, ka = 1 - math.exp(-1 / CTL_DAYS), 1 - math.exp(-1 / ATL_DAYS)
    ctl, atl, out, day = ctl0, atl0, [], start
    while day <= end:
        load = loads.get(day, 0.0)
        ctl += (load - ctl) * kc
        atl += (load - atl) * ka
        out.append(Point(day, load, ctl, atl))
        day += dt.timedelta(days=1)
    return out


def seed(wellness: list[dict], start: dt.date) -> tuple[float, float]:
    """intervals.icu's CTL and ATL on the day before `start` (or the nearest earlier day); 0 without."""
    before = [r for r in wellness if r["date"] < start.isoformat() and _f(r.get("ctl")) is not None]
    if not before:
        return 0.0, 0.0
    r = max(before, key=lambda r: r["date"])
    return _f(r["ctl"]), _f(r.get("atl")) or 0.0


def planned_loads(plan: Plan, athlete: Athlete, start: dt.date, end: dt.date) -> dict[dt.date, float]:
    out: dict[dt.date, float] = {}
    for d in expand(plan, athlete, start, end):
        if d.design_pending:
            r = d.block.weekly_tss
            out[d.date] = (r[0] + r[1]) / 2 / 7 if r else 0.0
        else:
            out[d.date] = sum(s.tss for s in d.sessions)
    return out
