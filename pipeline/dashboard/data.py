"""derived/dashboard.json: chart-ready series for the ten roadmap charts (docs/roadmap.md, v1.3).

Built from derived/ (activities, wellness, strength, compliance, debrief, weekly), the plan,
athlete.yaml and the April targets table in training/goals.md. Per-activity extras that need
intervals.icu streams (Grind Load for grinding and sailing, first and last hour power on
rides of 2 h or more) are computed once per activity into derived/activity_metrics.csv.

Charts: fitness (CTL, ATL, form, intervals.icu's CTL, planned CTL path, blocks, events),
weekly_load (by sport against the plan's band), yesterday (the debrief card and interval
bars), bench (e1RM against the checkpoint path, with each checkpoint's status), lifts
(trap-bar and pulling e1RM), grinding (1- and 4-min now against the April targets),
bike (FTP, long-ride duration, late-ride power), readiness (per day, with HRV and resting
HR against baseline), compliance (per week) and body (7-day average bodyweight; DEXA when
it exists).
"""

import csv
import datetime as dt
import json
import re
import statistics
from pathlib import Path

from pipeline.athlete import Athlete
from pipeline.dashboard.load import (
    CORRECTED_SPORTS, GRIND_PER_HARD_EFFORT, GRIND_PER_WORK_MIN, activity_load, daily_loads, planned_loads, run_model, seed,
)  # fmt: skip
from pipeline.ingest.strength import TOP_ROLES, e1rm
from pipeline.plan.expand import expand
from pipeline.plan.schema import Plan
from pipeline.review.debrief import Fetch, grind_load

METRIC_FIELDS = ("id", "date", "sport", "grind_efforts", "grind_work_min", "grind_efforts_85", "grind_source",
                 "first_hour_w", "last_hour_w")  # fmt: skip
LONG_RIDE_MIN = 120
READINESS_DAYS = 42
LIFT_LINES = ("trap_bar_deadlift", "weighted_pullup", "bench_pull")


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _r(x, n=1):
    return None if x is None else round(x, n)


# ------------------------------------------------------------------ per-activity metrics


def needs_metrics(a: dict) -> bool:
    return a["sport"] in CORRECTED_SPORTS or (a["sport"] == "bike" and (_f(a.get("moving_min")) or 0) >= LONG_RIDE_MIN)


def hour_power(streams: dict) -> tuple[float | None, float | None]:
    """Average moving watts in the first and the last hour of a ride (None without power)."""
    t, w = streams.get("time") or [], streams.get("watts") or []
    pairs = [(ti, wi) for ti, wi in zip(t, w) if ti is not None and wi]
    if not pairs or pairs[-1][0] - pairs[0][0] < 2 * 3600:
        return None, None
    start, end = pairs[0][0], pairs[-1][0]
    first = [wi for ti, wi in pairs if ti < start + 3600]
    last = [wi for ti, wi in pairs if ti > end - 3600]
    return round(statistics.fmean(first)), round(statistics.fmean(last))


def update_metrics(derived: Path, activities: list[dict], fetch: Fetch, athlete: Athlete) -> dict[str, dict]:
    """Compute missing per-activity metrics (only for activities whose streams could be fetched)."""
    path = derived / "activity_metrics.csv"
    have = {r["id"]: r for r in _rows(path)}
    for a in activities:
        if a["id"] in have or not needs_metrics(a):
            continue
        streams = fetch.streams(a["id"])
        if not streams:
            continue  # no network, or nothing recorded: try again next run
        row = {"id": a["id"], "date": a["date"], "sport": a["sport"]}
        if a["sport"] in CORRECTED_SPORTS:
            g = grind_load(fetch.intervals(a["id"]) if a["sport"] == "grinding" else None, streams, athlete.lthr_bpm)
            if g:
                row.update(grind_efforts=g["efforts"], grind_work_min=g["work_min"], grind_efforts_85=g["efforts_85"], grind_source=g["source"])
        if a["sport"] == "bike":
            row["first_hour_w"], row["last_hour_w"] = hour_power(streams)
        have[a["id"]] = {k: "" if row.get(k) is None else str(row.get(k)) for k in METRIC_FIELDS}
    if have:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, METRIC_FIELDS)
            w.writeheader()
            w.writerows(sorted(have.values(), key=lambda r: (r["date"], r["id"])))
    return have


# ------------------------------------------------------------------ goals.md targets


def april_targets(goals_md: Path) -> dict[str, dict]:
    """The 'Quality | Now | April target' table in training/goals.md: {quality: {now, target, values}}."""
    if not goals_md.exists():
        return {}
    out = {}
    for line in goals_md.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$", line)
        if not m or m.group(1).lower() in ("quality",) or set(m.group(1)) <= set("-: "):
            continue
        quality, now, target = m.groups()
        values = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", target.split("(")[0])]
        if values:
            out[quality] = {"now": now, "target": target, "values": values}
    return out


# ------------------------------------------------------------------ charts


def fitness(plan, athlete, activities, metrics, wellness, today) -> dict:
    dates = [dt.date.fromisoformat(a["date"]) for a in activities]
    start = min(dates) if dates else today
    loads = daily_loads(activities, metrics)
    ctl0, atl0 = seed(wellness, start)
    pts = run_model(loads, start, today, ctl0, atl0)
    icu = {r["date"]: r for r in wellness}
    last = pts[-1] if pts else None
    planned = run_model(planned_loads(plan, athlete, today + dt.timedelta(days=1), plan.valid_to),
                        today + dt.timedelta(days=1), plan.valid_to, last.ctl if last else ctl0, last.atl if last else atl0)  # fmt: skip
    events = []
    for g in plan.goals:
        if g.date:
            events.append({"date": g.date.isoformat(), "label": g.id, "priority": g.priority})
        elif g.window:
            events.append({"date": g.window[0].isoformat(), "end": g.window[1].isoformat(), "label": g.id, "priority": g.priority})
        elif g.target and re.fullmatch(r"\d{4}-\d{2}", str(g.target.get("month", ""))):
            events.append({"date": f"{g.target['month']}-01", "label": g.id, "priority": g.priority, "month": True})
    return {
        "days": [{"date": p.date.isoformat(), "load": round(p.load), "ctl": _r(p.ctl), "atl": _r(p.atl), "form": _r(p.form),
                  "icu_ctl": _f(icu.get(p.date.isoformat(), {}).get("ctl")), "icu_atl": _f(icu.get(p.date.isoformat(), {}).get("atl"))}
                 for p in pts],  # fmt: skip
        "planned": [{"date": p.date.isoformat(), "ctl": _r(p.ctl), "atl": _r(p.atl)} for p in planned],
        "blocks": [{"id": b.id, "name": b.name, "start": b.start.isoformat(), "end": b.end.isoformat(), "status": b.status,
                    "ctl_end": list(b.ctl_end) if b.ctl_end else None} for b in plan.blocks],  # fmt: skip
        "events": events,
        "seed": {"date": (start - dt.timedelta(days=1)).isoformat(), "ctl": _r(ctl0), "atl": _r(atl0)},
    }


def weekly_load(plan, athlete, activities, metrics, today) -> list[dict]:
    if not activities:
        return []
    first = _monday(min(dt.date.fromisoformat(a["date"]) for a in activities))
    out, monday = [], first
    while monday <= _monday(today) + dt.timedelta(days=7):
        sunday = monday + dt.timedelta(days=6)
        by_sport: dict[str, float] = {}
        for a in activities:
            if monday.isoformat() <= a["date"] <= sunday.isoformat():
                base, corr, _ = activity_load(a, metrics.get(a["id"]))
                by_sport[a["sport"]] = round(by_sport.get(a["sport"], 0) + base + corr)
        planned = round(sum(planned_loads(plan, athlete, monday, sunday).values()))
        band = None
        for b in plan.blocks:
            if b.start <= sunday and monday <= b.end:
                w = max((w for w in b.weeks or [] if w.start <= sunday), key=lambda w: w.start, default=None)
                r = w.weekly_tss if w and w.weekly_tss else b.weekly_tss
                band = list(r) if r else None
        out.append({"week": monday.isoformat(), "by_sport": by_sport, "total": sum(by_sport.values()), "planned": planned,
                    "band": band, "future": monday > today})  # fmt: skip
        monday += dt.timedelta(days=7)
    return out


def _best_by_day(sets: list[dict], lift: str) -> list[dict]:
    best: dict[str, tuple[float, str]] = {}
    for r in sets:
        if r["lift"] != lift or not (r["role"] in TOP_ROLES or r.get("rpe")):
            continue
        val = e1rm(float(r["kg"]), int(r["reps"]), _f(r.get("rpe")))
        if r["date"] not in best or val > best[r["date"]][0]:
            best[r["date"]] = (val, f"{float(r['kg']):g} x {r['reps']}" + (f" @{r['rpe']}" if r.get("rpe") else ""))
    return [{"date": d, "e1rm": round(v, 1), "set": s} for d, (v, s) in sorted(best.items())]


def bench(plan, sets, today) -> dict:
    points = _best_by_day(sets, "bench_press")
    line = next((s for s in plan.strength_lines if s.lift == "bench_press"), None)
    checkpoints = []
    for d, v in line.checkpoints if line else []:
        if d > today:
            status = "upcoming"
        else:
            near = [p for p in points if (d - dt.timedelta(days=10)).isoformat() <= p["date"] <= d.isoformat()]
            if not near:
                status = "no data"
            else:
                status = "met" if max(p["e1rm"] for p in near) >= v - v / 30 else "missed"  # within one rep
        checkpoints.append({"date": d.isoformat(), "e1rm": v, "status": status})
    goal = next((g for g in plan.goals if g.target and g.target.get("lift") == "bench_press"), None)
    return {"points": points, "checkpoints": checkpoints,
            "goal": {"date": goal.date.isoformat() if goal and goal.date else None,
                     "e1rm_needed_kg": goal.target.get("e1rm_needed_kg") if goal else None}}  # fmt: skip


def readiness(wellness, today) -> list[dict]:
    since = (today - dt.timedelta(days=READINESS_DAYS - 1)).isoformat()
    return [{"date": r["date"], "readiness": r.get("readiness") or None, "hrv": _f(r.get("hrv")), "hrv_baseline": _f(r.get("hrv_baseline")),
             "resting_hr": _f(r.get("resting_hr")), "resting_hr_baseline": _f(r.get("resting_hr_baseline")), "sleep_h": _f(r.get("sleep_h"))}
            for r in wellness if since <= r["date"] <= today.isoformat()]  # fmt: skip


def compliance(rows) -> list[dict]:
    weeks: dict[str, dict] = {}
    for r in rows:
        week = _monday(dt.date.fromisoformat(r["date"])).isoformat()
        w = weeks.setdefault(week, {"week": week})
        key = r["grade"] if r["modality"] != "strength" else f"strength_{r['grade']}"
        w[key] = w.get(key, 0) + 1
    return [weeks[k] for k in sorted(weeks)]


def body(wellness) -> dict:
    rows = sorted((r["date"], _f(r.get("weight_kg"))) for r in wellness if _f(r.get("weight_kg")) is not None)
    out = []
    for i, (d, _) in enumerate(rows):
        window = [w for dd, w in rows[: i + 1] if dd >= (dt.date.fromisoformat(d) - dt.timedelta(days=6)).isoformat()]
        out.append({"date": d, "weight_kg": rows[i][1], "avg7_kg": round(statistics.fmean(window), 2)})
    return {"weight": out, "dexa": []}


def build(plan: Plan, athlete: Athlete, derived: Path, today: dt.date, fetch: Fetch, goals_md: Path) -> dict:
    activities = sorted(_rows(derived / "activities.csv"), key=lambda a: (a["date"], a.get("start_local", "")))
    activities = [a for a in activities if a["date"] <= today.isoformat()]
    wellness = sorted(_rows(derived / "wellness.csv"), key=lambda r: r["date"])
    sets = _rows(derived / "strength.csv")
    metrics = update_metrics(derived, activities, fetch, athlete)
    targets = april_targets(goals_md)
    try:
        brief = json.loads((derived / "today.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        brief = {}
    try:
        debrief = json.loads((derived / "debrief.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        debrief = None
    if debrief and debrief.get("date") != (today - dt.timedelta(days=1)).isoformat():
        debrief = None

    rides = [a for a in activities if a["sport"] == "bike"]
    ftp_seen, ftp_changes = None, []
    for a in rides:
        v = _f(a.get("icu_ftp"))
        if v is not None and v != ftp_seen:
            ftp_changes.append({"date": a["date"], "ftp": v})
            ftp_seen = v
    late = [{"date": m["date"], "first_hour_w": _f(m["first_hour_w"]), "last_hour_w": _f(m["last_hour_w"]),
             "ratio": _r(_f(m["last_hour_w"]) / _f(m["first_hour_w"]), 3)} for m in metrics.values()
            if _f(m.get("first_hour_w")) and _f(m.get("last_hour_w"))]  # fmt: skip

    return {
        "date": today.isoformat(),
        "status": brief.get("status") if brief.get("date") == today.isoformat() else None,
        "generated_at": dt.datetime.now(athlete.schedule.zone()).isoformat(timespec="minutes"),
        "load_model": {"ctl_days": 42, "atl_days": 7, "grind_per_work_min": GRIND_PER_WORK_MIN,
                       "grind_per_hard_effort": GRIND_PER_HARD_EFFORT, "doc": "pipeline/dashboard/load.py"},  # fmt: skip
        "today": {"readiness": (brief.get("readiness") or {}) if brief.get("date") == today.isoformat() else {},
                  "sessions": brief.get("sessions", []) if brief.get("date") == today.isoformat() else []},  # fmt: skip
        "charts": {
            "fitness": fitness(plan, athlete, activities, metrics, wellness, today),
            "weekly_load": weekly_load(plan, athlete, activities, metrics, today),
            "yesterday": debrief,
            "bench": bench(plan, sets, today),
            "lifts": {lift: _best_by_day(sets, lift) for lift in LIFT_LINES},
            "grinding": {
                "now": {"60s": athlete.curves.get("grinding_60s").watts if "grinding_60s" in athlete.curves else None,
                        "240s": athlete.curves.get("grinding_240s").watts if "grinding_240s" in athlete.curves else None},
                "target": {"60s": (targets.get("Grinding 1-min") or {}).get("values", [None])[0],
                           "240s": (targets.get("Grinding 4-min") or {}).get("values", [None])[0]},
                "tests": [],  # grinding power isn't recorded yet (roadmap: machine import)
            },  # fmt: skip
            "bike": {
                "ftp_now": athlete.ftp["bike"].watts if "bike" in athlete.ftp else None,
                "ftp_intervals_icu": ftp_changes,
                "ftp_target": (targets.get("Bike FTP") or {}).get("values", [])[:2],
                "long_rides": [{"date": a["date"], "minutes": _f(a["moving_min"]), "name": a["name"]} for a in rides
                               if (_f(a["moving_min"]) or 0) >= LONG_RIDE_MIN],
                "late_ride_power": late,
            },  # fmt: skip
            "readiness": readiness(wellness, today),
            "compliance": compliance(_rows(derived / "compliance.csv")),
            "body": body(wellness),
        },
    }


def write(d: dict, derived: Path) -> Path:
    path = derived / "dashboard.json"
    path.write_text(json.dumps(d, indent=1) + "\n", encoding="utf-8")
    return path
