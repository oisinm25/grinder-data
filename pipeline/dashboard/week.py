"""The "This week" rows for the Pedestal dashboard (export_rows): the week outline, fitness over
the same 7 days, time in zones and heavy reps for last week and this week so far.

Inputs are the data branch's tidy CSVs (as lists of dicts), dashboard.json's fitness chart and
the plan. Everything is per day around `today`: today-3 to today+3.
"""

import datetime as dt
import re

from pipeline.athlete import Athlete
from pipeline.brief import short_readiness
from pipeline.plan.expand import expand
from pipeline.plan.schema import Plan

HALF = 3  # days either side of today
GRADE_WORDS = {"on_target": "held", "held": "held", "faded": "faded", "off_target": "off target", "under": "off target",
               "missed": "missed", "skipped": "missed", "done": "done", "logged": "done"}  # fmt: skip
UNGRADED = ("rest", "mobility")  # the review doesn't grade these


def short_summary(summary: str) -> str:
    """'Threshold 2 x 20 min at 92-95% FTP, 5 min easy between' -> 'Threshold 2x20 min';
    'Upper max effort - bench top set, ...' -> 'Upper max effort'; 'TEST - 20-min bike test ...' -> 'Test: 20-min bike test ...'."""
    s = summary.strip()
    test = s.upper().startswith("TEST - ")
    if test:
        s = s[7:]
    s = re.split(r" - |, | at | \(", s, maxsplit=1)[0]
    s = re.sub(r"(\d) x (\d)", r"\1x\2", s)
    return ("Test: " + s) if test else s


def _f(v):
    return float(v) if v not in ("", None) else None


def session_grade(items: list[dict]) -> str | None:
    """One word for a planned session from its compliance.csv rows (one per item; strength has one per lift)."""
    grades = [r["grade"] for r in items]
    if not grades:
        return None
    if all(g == "skipped" for g in grades):
        return "missed"
    words = [GRADE_WORDS.get(g, g.replace("_", " ")) for g in grades if g != "skipped"]
    for w in ("off target", "faded", "missed", "held", "done"):
        if w in words:
            return w
    return words[0]


def _actual_tss(items: list[dict], modality: str, date: str, activities: list[dict]) -> float | None:
    ids = {m for r in items for m in re.findall(r"activity (i?\d+)", r.get("actual") or "")}
    if modality == "strength":
        ids |= {a["id"] for a in activities if a["date"] == date and a["sport"] == "strength"}
    loads = [_f(a.get("load")) for a in activities if a["id"] in ids]
    loads = [x for x in loads if x is not None]
    return round(sum(loads)) if loads else None


def week_outline(today: dt.date, plan: Plan, athlete: Athlete, wellness: list[dict], compliance: list[dict],
                 activities: list[dict]) -> list[dict]:  # fmt: skip
    """One row per planned session (rest days and days outside the plan: one row with no session),
    today-3..today+3. Readiness for past days and today; grade and actual TSS for past days."""
    days = {d.date: d for d in expand(plan, athlete, today - dt.timedelta(days=HALF), today + dt.timedelta(days=HALF))}
    well = {r["date"]: r for r in wellness}
    rows = []
    for i in range(-HALF, HALF + 1):
        day = today + dt.timedelta(days=i)
        iso, d = day.isoformat(), days.get(today + dt.timedelta(days=i))
        w = well.get(iso, {}) if day <= today else {}
        colour = w.get("readiness") or None
        reason = w.get("readiness_reason", "")
        base = {"date": iso, "weekday": f"{day:%a}", "offset": i, "when": "past" if i < 0 else "today" if i == 0 else "future",
                "readiness": colour if day <= today else None,
                "readiness_reason": (short_readiness(reason) if colour and colour != "none" else reason) if day <= today else "",
                "block": d.block.id if d else None, "block_name": d.block.name if d else None,
                "week": d.week.label if d and d.week else None}  # fmt: skip
        sessions = [s for s in (d.sessions if d else ()) if s.session.modality != "rest"]
        vacated = list(getattr(d, "vacated", ()) or ()) if d else []  # slots training/logs/changes.yaml emptied
        if not sessions and not vacated:
            rows.append({**base, "slot": None, "order": None, "session": None, "modality": None, "moved_note": "", "note": "",
                         "summary": "Design pending" if d and d.design_pending else "Rest" if d else "Outside the plan",
                         "planned_tss": None, "grade": None, "actual_tss": None, "status": "rest"})  # fmt: skip
            continue
        day_rows = []
        for s in sessions:
            items = [r for r in compliance if r["date"] == iso and r["session"] == s.session_id and r["slot"] == s.slot]
            past = day < today
            grade = session_grade(items) if past else None
            moved = getattr(s, "moved_note", "") or ""  # "(moved from Mon)"
            day_rows.append({**base, "slot": s.slot.split("_")[-1].upper(), "order": s.order, "session": s.session_id,
                             "modality": s.session.modality, "summary": short_summary(s.session.summary),
                             "moved_note": moved, "note": moved,
                             "planned_tss": round(s.tss) if s.tss else None, "grade": grade,
                             "actual_tss": _actual_tss(items, s.session.modality, iso, activities) if past else None,
                             "status": (grade or ("not graded" if s.session.modality in UNGRADED else "not graded yet")) if past else "planned"})  # fmt: skip
        for v in vacated:
            sess = plan.sessions.get(v.session_id)
            day_rows.append({**base, "slot": v.slot.split("_")[-1].upper(), "order": -1, "session": v.session_id,
                             "modality": sess.modality if sess else None, "summary": short_summary(sess.summary) if sess else v.session_id,
                             "moved_note": "", "note": v.note(day), "planned_tss": None, "grade": None, "actual_tss": None,
                             "status": v.kind})  # fmt: skip
        rows += sorted(day_rows, key=lambda r: (r["slot"] != "AM", r["order"]))
    return rows


def fitness_week(today: dt.date, fitness: dict) -> list[dict]:
    """CTL, ATL and form for today-3..today+3: actual up to today, projected from the plan's load after."""
    actual = {r["date"]: r for r in fitness.get("days", [])}
    planned = {r["date"]: r for r in fitness.get("planned", [])}
    rows = []
    for i in range(-HALF, HALF + 1):
        iso = (today + dt.timedelta(days=i)).isoformat()
        r, kind = (actual.get(iso), "actual") if i <= 0 else (planned.get(iso), "projected")
        if r is None:
            rows.append({"date": iso, "offset": i, "kind": kind, "ctl": None, "atl": None, "form": None})
            continue
        ctl, atl = r.get("ctl"), r.get("atl")
        form = r.get("form") if r.get("form") is not None else (round(ctl - atl, 1) if ctl is not None and atl is not None else None)
        rows.append({"date": iso, "offset": i, "kind": kind, "ctl": ctl, "atl": atl, "form": form})
    return rows


def _weeks(today: dt.date) -> list[tuple[str, dt.date, dt.date]]:
    monday = today - dt.timedelta(days=today.weekday())
    return [("last", monday - dt.timedelta(days=7), monday - dt.timedelta(days=1)), ("this", monday, today)]


def zones_week(today: dt.date, zones: list[dict]) -> list[dict]:
    """Seconds per zone per kind and sport, last Mon-Sun week and this week so far (all 7 zones listed)."""
    rows = []
    for label, lo, hi in _weeks(today):
        inside = [z for z in zones if lo.isoformat() <= z["date"] <= hi.isoformat()]
        for sport, kind in sorted({(z["sport"], z["kind"]) for z in inside}, key=lambda k: (k[1] != "power", k[0])):
            mine = [z for z in inside if z["sport"] == sport and z["kind"] == kind]
            total = sum(int(z["seconds"]) for z in mine)
            for zone in range(1, 8):
                secs = sum(int(z["seconds"]) for z in mine if int(z["zone"]) == zone)
                rows.append({"week": label, "week_start": lo.isoformat(), "week_end": hi.isoformat(), "sport": sport, "kind": kind,
                             "zone": zone, "seconds": secs, "hours": round(secs / 3600, 2),
                             "share": round(secs / total, 3) if total else 0.0,
                             "activities": len({z["activity_id"] for z in mine}),
                             "sources": "+".join(sorted({z.get("source", "") for z in mine}))})  # fmt: skip
    return rows


def heavy_reps_week(today: dt.date, heavy: list[dict]) -> list[dict]:
    """Per lift: reps at >= 90% e1RM, the sets they came in, and all work/top/test sets, last week and this week so far."""
    rows = []
    for label, lo, hi in _weeks(today):
        inside = [h for h in heavy if lo.isoformat() <= h["date"] <= hi.isoformat()]
        for lift in sorted({h["lift"] for h in inside}):
            mine = [h for h in inside if h["lift"] == lift]
            heavy_sets = [h for h in mine if int(h["heavy_reps"]) > 0]
            rows.append({"week": label, "week_start": lo.isoformat(), "week_end": hi.isoformat(), "lift": lift,
                         "heavy_reps": sum(int(h["heavy_reps"]) for h in mine), "heavy_sets": len(heavy_sets), "sets": len(mine),
                         "top_pct": round(max(float(h["pct_of_e1rm"]) for h in mine), 3),
                         "heavy_detail": "; ".join(f"{h['date'][5:]} {float(h['kg']):g} x {h['reps']} ({float(h['pct_of_e1rm']):.0%})" for h in heavy_sets)})  # fmt: skip
    return rows


def build(today: dt.date, plan: Plan, athlete: Athlete, fitness: dict, tables: dict[str, list[dict]]) -> dict[str, list[dict]]:
    return {
        "week_outline": week_outline(today, plan, athlete, tables.get("wellness.csv", []), tables.get("compliance.csv", []),
                                     tables.get("activities.csv", [])),  # fmt: skip
        "fitness_week": fitness_week(today, fitness),
        "zones_week": zones_week(today, tables.get("zones.csv", [])),
        "heavy_reps_week": heavy_reps_week(today, tables.get("heavy_reps.csv", [])),
    }
