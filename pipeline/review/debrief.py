"""The daily debrief: how yesterday actually went, written to derived/debrief.md and debrief.json.

    python -m pipeline.review.debrief [--day 2026-10-23] [--offline | --no-network]

Per session, planned against actual (activities from derived/activities.csv, matched by day
and sport; detail, intervals and HR/power streams from intervals.icu, cached in cache/):

- **Bike, hand-bike, erg**: each planned power step against the activity's WORK intervals, in
  order: `held` inside the band +-3%, `under` below it, `over` above it; the session grade
  (held / faded / off_target / done / missed / skipped, as pipeline.review.compliance grades
  it); NP, IF and TSS from intervals.icu; on steady rides (every power step `steady`) HR drift
  (Pa:HR decoupling): intervals.icu's value, or, when it has none, first-half against
  second-half watts per beat from the streams.
- **Strength**: each logged set against the day's prescription: `hit` (weight and reps met,
  RPE at or under the cap), `under` (lighter or short), `over RPE`, or `logged` (a first
  "choose" weight or a test). The lift's e1RM before and after the day (kg x (1 + (reps +
  (10 - RPE)) / 30), missing RPE = 10), and what the progression rule now prescribes for the
  session's next occurrence within 14 days ("next time").
- **Grinding**: watts per rep against the target, fade (last rep against the first), and
  Grind Load.
- **Sailing** (and other unplanned sessions): duration, HR load, Grind Load.

**Grind Load** (design-rules.md: effort count, work minutes, efforts >= 85% LTHR), with LTHR
from athlete.yaml. With WORK intervals: efforts = the WORK intervals, work minutes = their
moving time, efforts >= 85% = those averaging >= 85% LTHR. Without them, from the HR stream:
an effort is a stretch at >= 80% LTHR lasting >= 20 s (dips under 15 s don't end it); work
minutes = the efforts' total time; efforts >= 85% = efforts averaging >= 85% LTHR. HR lags
short efforts, so this under-reads 1-min grinding reps; it's a count, not a power measure.

**Day context**: that morning's readiness and its reason, sleep, and load done against plan.

**Verdict** (first rule that applies):
1. nothing planned and nothing done: "Rest day, as planned."
2. every planned session missed or skipped: "Missed: <sessions>." (+ "Did <unplanned> instead.")
3. an endurance session faded: "<session> faded in rep N (W vs low W)."
4. a strength set under prescription: "<lift> under prescription: <achieved> vs <prescribed>."
5. an endurance session off target: "<session> off target: k of n steps in band."
6. some sessions missed or skipped: "<done> done; <missed> missed."
7. every graded session held and every set hit: "Held the plan." (+ "<lift> moves up next time.")
8. otherwise: "Done as planned; nothing to grade on power."

**Note for today** (first rule that applies):
1. today is red: "Red today: weights cut to 90%, RPE cap 7; keep it all easy."
2. today is amber: "Amber today: last weights held, RPE cap 8."
3. yesterday faded or was off target, and today has hard work (a power step at >= 90% FTP,
   or a top set): "<yesterday> faded: start today's <session> at the bottom of its band."
4. yesterday faded or was off target, and today is easy: "<yesterday> faded in rep N: today
   stays easy as planned."
5. yesterday's load was > 125% of plan: "Yesterday ran <x>% over plan: keep today's easy work easy."
6. a strength lift progresses next time: "<lift>: <kg> x <reps> next <day>."
7. HR drift > 5% on yesterday's steady ride: "HR drift <x>% on yesterday's ride: start the
   next long ride easier and fuel early."
8. otherwise: "Today: <first session>." or "Rest today."
"""

import argparse
import csv
import datetime as dt
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.athlete import Athlete
from pipeline.ingest.intervals import IntervalsClient, IntervalsError
from pipeline.ingest.strength import e1rm
from pipeline.plan.expand import ENDURANCE_MODALITIES, ScheduledSession, expand
from pipeline.plan.schema import Plan, PlanLoadError, load_checked, report_load_error
from pipeline.prescribe import LogRow, latest_e1rm, load_history, load_readiness, prescribe
from pipeline.review.compliance import BAND_TOLERANCE, grade_endurance, match_activities, power_steps
from pipeline.settings import MissingSetting, load_env, require_env

EFFORT_PCT, HARD_PCT = 0.80, 0.85
EFFORT_MIN_S, EFFORT_GAP_S = 20, 15
DRIFT_NOTE_PCT = 5.0
OVER_PLAN = 1.25


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


def _d(day: dt.date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def _short(summary: str) -> str:
    first = summary.split(" - ")[0]
    return first if len(first) >= 10 else summary


# ------------------------------------------------------------------ data from intervals.icu


@dataclass
class Fetch:
    """Activity detail, intervals and streams; None when unavailable (no network, not synced)."""

    client: IntervalsClient | None = None
    problems: list[str] = field(default_factory=list)

    def _call(self, what, fn):
        if self.client is None:
            return None
        try:
            return fn()
        except IntervalsError as e:
            self.problems.append(f"{what}: {e}")
            return None

    def detail(self, activity_id: str) -> dict | None:
        return self._call(f"activity {activity_id}", lambda: self.client.activity(activity_id))

    def intervals(self, activity_id: str) -> list[dict] | None:
        data = self._call(f"intervals {activity_id}", lambda: self.client.activity_intervals(activity_id))
        return None if data is None else data.get("icu_intervals") or []

    def streams(self, activity_id: str) -> dict[str, list]:
        return self._call(f"streams {activity_id}", lambda: self.client.activity_streams(activity_id)) or {}


# ------------------------------------------------------------------ measures


def hr_efforts(times: list, hr: list, lthr: float) -> list[tuple[float, float]]:
    """(duration_s, average HR) of each stretch at >= 80% LTHR lasting >= 20 s, merging dips < 15 s."""
    floor = EFFORT_PCT * lthr
    efforts, start, last, samples = [], None, None, []
    for t, h in zip(times, hr):
        if h is None or t is None:
            continue
        if h >= floor:
            if start is not None and t - last > EFFORT_GAP_S:
                if last - start >= EFFORT_MIN_S:
                    efforts.append((last - start, statistics.fmean(samples)))
                start, samples = None, []
            if start is None:
                start = t
            last = t
            samples.append(h)
    if start is not None and last - start >= EFFORT_MIN_S:
        efforts.append((last - start, statistics.fmean(samples)))
    return efforts


def grind_load(intervals: list[dict] | None, streams: dict, lthr: float) -> dict | None:
    hard = HARD_PCT * lthr
    work = [iv for iv in intervals or [] if iv.get("type") == "WORK"]
    if work:
        hrs = [_f(iv.get("average_heartrate")) for iv in work]
        return {"source": "intervals", "efforts": len(work),
                "work_min": round(sum(_f(iv.get("moving_time")) or 0 for iv in work) / 60, 1),
                "efforts_85": sum(1 for h in hrs if h is not None and h >= hard)}  # fmt: skip
    if streams.get("heartrate") and streams.get("time"):
        eff = hr_efforts(streams["time"], streams["heartrate"], lthr)
        return {"source": "hr stream", "efforts": len(eff), "work_min": round(sum(d for d, _ in eff) / 60, 1),
                "efforts_85": sum(1 for _, h in eff if h >= hard)}  # fmt: skip
    return None


def drift_from_streams(streams: dict) -> float | None:
    """Pa:HR decoupling %: first-half watts per beat against the second half (moving samples)."""
    pairs = [(w, h) for w, h in zip(streams.get("watts") or [], streams.get("heartrate") or []) if w and h]
    if len(pairs) < 600:  # under ~10 min of paired data says nothing
        return None
    half = len(pairs) // 2
    ef = [statistics.fmean(w for w, _ in part) / statistics.fmean(h for _, h in part) for part in (pairs[:half], pairs[half:])]
    return round((ef[0] - ef[1]) / ef[0] * 100, 1)


def _rep_status(watts: float, band: tuple[int, int]) -> str:
    lo, hi = band
    if watts < lo * (1 - BAND_TOLERANCE):
        return "under"
    return "over" if watts > hi * (1 + BAND_TOLERANCE) else "held"


# ------------------------------------------------------------------ items


def endurance_item(s: ScheduledSession, activity: dict | None, fetch: Fetch, athlete: Athlete) -> dict:
    item = {"kind": "endurance", "slot": s.slot, "session": s.session_id, "modality": s.session.modality,
            "summary": s.session.summary, "planned_min": round((s.duration_s or 0) / 60), "planned_tss": s.tss}  # fmt: skip
    intervals = fetch.intervals(activity["id"]) if activity else None
    grade = grade_endurance(s, activity, intervals)
    item.update(grade=grade.grade, grade_note=grade.note)
    if activity is None:
        return item
    detail = fetch.detail(activity["id"]) or {}
    item["activity"] = {"id": activity["id"], "name": activity["name"], "moving_min": _f(activity["moving_min"])}
    np_w = _f(detail.get("icu_weighted_avg_watts")) or _f(activity.get("np_watts"))
    intensity = _f(detail.get("icu_intensity")) or _f(activity.get("intensity"))
    item.update(np_w=np_w, intensity=round(intensity / 100, 2) if intensity else None,
                tss=_f(detail.get("icu_training_load")) or _f(activity.get("load")))  # fmt: skip

    steps = power_steps(s.steps)
    work = [iv for iv in intervals or [] if iv.get("type") == "WORK" and iv.get("average_watts")]
    if len(steps) == 1 and not work and _f(activity.get("avg_watts")):
        work = [{"average_watts": _f(activity["avg_watts"]), "average_heartrate": _f(activity.get("avg_hr"))}]
    item["reps"] = [{"n": i + 1, "target": step.target.text(), "band": list(step.target.watts),
                     "watts": round(_f(iv["average_watts"])), "hr": _f(iv.get("average_heartrate")),
                     "status": _rep_status(_f(iv["average_watts"]), step.target.watts)}
                    for i, (step, iv) in enumerate(zip(steps, work))]  # fmt: skip
    if steps and all(st.kind == "steady" for st in steps):
        drift = _f(detail.get("decoupling"))
        item["hr_drift_pct"] = round(drift, 1) if drift is not None else drift_from_streams(fetch.streams(activity["id"]))
    if s.session.modality == "grinding":
        if len(item["reps"]) >= 2:
            first, last = item["reps"][0]["watts"], item["reps"][-1]["watts"]
            item["fade_pct"] = round((first - last) / first * 100, 1)
        item["grind_load"] = grind_load(intervals, fetch.streams(activity["id"]), athlete.lthr_bpm)
    return item


def strength_item(s: ScheduledSession, day: dt.date, plan: Plan, athlete: Athlete, history: list[LogRow],
                  readiness: dict) -> dict:  # fmt: skip
    rx = [p for p in prescribe(plan, athlete, history, readiness, day, day) if p.session == s.session_id]
    names = {k: v.name for k, v in athlete.strength.lifts.items()}
    logged = [r for r in history if r.date == day and r.session == s.session_id]
    lifts = []
    for lift in dict.fromkeys(p.lift for p in rx):
        groups = [p for p in rx if p.lift == lift]
        sets = []
        for p in groups:
            mine = sorted((r for r in logged if r.lift == lift and r.role == p.role), key=lambda r: r.set_no)
            planned = f"{p.sets} x {p.reps}" + (f" @ {p.kg:g} kg" if p.kg is not None else " @ choose") + (
                f" RPE<={p.rpe_cap:g}" if p.rpe_cap is not None else "")
            for r in mine:
                achieved = f"{r.kg:g} x {r.reps}" + (f" @{r.rpe:g}" if r.rpe is not None else "")
                if p.kg is None or p.role in ("amrap", "test"):
                    status = "logged"
                elif r.kg < p.kg - 1e-6 or r.reps < p.reps:
                    status = "under"
                elif p.rpe_cap is not None and r.rpe is not None and r.rpe > p.rpe_cap:
                    status = "over RPE"
                else:
                    status = "hit"
                sets.append({"role": p.role, "set": r.set_no, "prescribed": planned, "achieved": achieved, "status": status})
            if not mine:
                sets.append({"role": p.role, "set": None, "prescribed": planned, "achieved": "", "status": "not logged"})
        before = latest_e1rm([r for r in history if r.lift == lift and r.date < day])
        today = [r for r in logged if r.lift == lift]
        after = max((r.e1rm for r in today), default=None)
        lifts.append({"lift": lift, "name": names.get(lift, lift), "sets": sets,
                      "e1rm_before": round(before.e1rm, 1) if before else None,
                      "e1rm_after": round(after, 1) if after else None,
                      "next": next_time(s, lift, day, plan, athlete, history) if today else None})  # fmt: skip
    return {"kind": "strength", "slot": s.slot, "session": s.session_id, "modality": "strength",
            "summary": s.session.summary, "planned_tss": s.tss, "lifts": lifts,
            "grade": "skipped" if not logged else ("under" if any(x["status"] == "under" for lf in lifts for x in lf["sets"]) else "held")}  # fmt: skip


def next_time(s: ScheduledSession, lift: str, day: dt.date, plan: Plan, athlete: Athlete, history: list[LogRow]) -> dict | None:
    """What the session's next occurrence (within 14 days) prescribes for the lift's first group."""
    for d in expand(plan, athlete, day + dt.timedelta(days=1), day + dt.timedelta(days=14)):
        if any(x.session_id == s.session_id for x in d.sessions):
            p = next((p for p in prescribe(plan, athlete, history, {}, d.date, d.date) if p.session == s.session_id and p.lift == lift), None)
            if p is None:
                return None
            return {"date": d.date.isoformat(), "kg": p.kg, "reps": p.reps, "reason": p.reason.split("; readiness")[0]}
    return None


def unplanned_item(activity: dict, fetch: Fetch, athlete: Athlete) -> dict:
    detail = fetch.detail(activity["id"]) or {}
    streams = fetch.streams(activity["id"]) if activity["sport"] in ("sail", "sail_or_other", "grinding", "conditioning") else {}
    item = {"kind": "unplanned", "sport": activity["sport"], "type": activity["type"], "name": activity["name"],
            "moving_min": _f(activity["moving_min"]), "hr_load": _f(detail.get("hr_load")) or _f(activity.get("load")),
            "avg_hr": _f(detail.get("average_heartrate")) or _f(activity.get("avg_hr")), "review": activity.get("review", "")}  # fmt: skip
    intensity = _f(detail.get("icu_intensity"))
    item.update(np_w=_f(detail.get("icu_weighted_avg_watts")), intensity=round(intensity / 100, 2) if intensity else None,
                tss=_f(detail.get("icu_training_load")), hr_drift_pct=round(_f(detail["decoupling"]), 1) if _f(detail.get("decoupling")) is not None else None)  # fmt: skip
    if streams:
        item["grind_load"] = grind_load(None, streams, athlete.lthr_bpm)
    return item


# ------------------------------------------------------------------ verdict and note


def verdict(items: list[dict], planned_any: bool) -> str:
    planned = [i for i in items if i["kind"] != "unplanned"]
    extra = [i for i in items if i["kind"] == "unplanned"]
    if not planned_any and not extra:
        return "Rest day, as planned."
    gone = [i for i in planned if i["grade"] in ("missed", "skipped")]
    if planned and len(gone) == len(planned):
        line = "Missed: " + ", ".join(_short(i["summary"]) for i in gone) + "."
        return line + (f" Did {', '.join(_extra_name(i) for i in extra)} instead." if extra else "")
    for i in planned:
        if i["grade"] == "faded":
            rep = i["reps"][-1] if i.get("reps") else None
            return f"{_short(i['summary'])} faded in rep {rep['n']} ({rep['watts']} W vs {rep['band'][0]} W)." if rep else f"{_short(i['summary'])} faded."
    for i in planned:
        for lift in i.get("lifts", []):
            under = next((x for x in lift["sets"] if x["status"] == "under"), None)
            if under:
                return f"{lift['name']} under prescription: {under['achieved']} vs {under['prescribed']}."
    for i in planned:
        if i["grade"] == "off_target":
            return f"{_short(i['summary'])} off target: {i['grade_note'].split(';')[0]}."
    if gone:
        done = [i for i in planned if i not in gone]
        return f"{', '.join(_short(i['summary']) for i in done)} done; {', '.join(_short(i['summary']) for i in gone)} missed."
    graded = [i for i in planned if i["grade"] in ("held", "faded", "off_target")]
    sets = [x for i in planned for lf in i.get("lifts", []) for x in lf["sets"] if x["status"] in ("hit", "under", "over RPE")]
    if graded or sets:
        if all(i["grade"] == "held" for i in graded) and all(x["status"] == "hit" for x in sets):
            up = next((lf for i in planned for lf in i.get("lifts", []) if _progresses(lf)), None)
            return "Held the plan." + (f" {up['name']} moves up next time." if up else "")
    if not planned and extra:
        return f"Unplanned: {', '.join(_extra_name(i) for i in extra)}."
    return "Done as planned; nothing to grade on power."


def _extra_name(i: dict) -> str:
    mins = f" {i['moving_min']:.0f} min" if i.get("moving_min") else ""
    return f"{i['sport'].replace('_', ' ')}{mins}"


def _progresses(lift: dict) -> bool:
    nxt, top = lift.get("next"), next((x for x in lift["sets"] if x["achieved"]), None)
    if not nxt or nxt.get("kg") is None or not top:
        return False
    return nxt["kg"] > float(top["achieved"].split(" x ")[0])


def note_for_today(items: list[dict], context: dict, today: list[ScheduledSession], today_colour: str | None, athlete: Athlete) -> str:
    if today_colour == "red":
        return "Red today: weights cut to 90%, RPE cap 7; keep it all easy."
    if today_colour == "amber":
        return "Amber today: last weights held, RPE cap 8."
    rough = next((i for i in items if i.get("grade") in ("faded", "off_target")), None)
    if rough:
        hard = next((s for s in today if hard_session(s, athlete)), None)
        if hard:
            return f"{_short(rough['summary'])} faded: start today's {_short(hard.session.summary)} at the bottom of its band."
        rep = f" in rep {rough['reps'][-1]['n']}" if rough.get("reps") else ""
        return f"{_short(rough['summary'])} {'faded' if rough['grade'] == 'faded' else 'was off target'}{rep}: today stays easy as planned."
    done, planned = context.get("load_done"), context.get("load_planned")
    if planned and done and done > OVER_PLAN * planned:
        return f"Yesterday ran {round((done / planned - 1) * 100)}% over plan: keep today's easy work easy."
    for i in items:
        for lift in i.get("lifts", []):
            if _progresses(lift):
                n = lift["next"]
                return f"{lift['name']}: {n['kg']:g} x {n['reps']} next {_d(dt.date.fromisoformat(n['date']))}."
    drift = next((i for i in items if (i.get("hr_drift_pct") or 0) > DRIFT_NOTE_PCT), None)
    if drift:
        return f"HR drift {drift['hr_drift_pct']:g}% on yesterday's ride: start the next long ride easier and fuel early."
    work = [s for s in today if s.session.modality not in ("rest", "mobility")]
    return f"Today: {_short(work[0].session.summary)}." if work else "Rest today."


def hard_session(s: ScheduledSession, athlete: Athlete) -> bool:
    if s.session.modality == "strength":
        return any(g.role == "top" for ex in s.session.exercises or [] for g in ex.groups)
    ftp = athlete.ftp.get(s.session.modality)
    return bool(ftp) and any(st.target.watts[0] >= 0.9 * ftp.watts for st in power_steps(s.steps))


# ------------------------------------------------------------------ build and render


def build(day: dt.date, plan: Plan, athlete: Athlete, derived: Path, fetch: Fetch) -> dict:
    iso = day.isoformat()
    activities = [a for a in _rows(derived / "activities.csv") if a["date"] == iso]
    history = load_history(derived / "strength.csv") if (derived / "strength.csv").exists() else []
    readiness = load_readiness(derived / "wellness.csv")
    wellness = {r["date"]: r for r in _rows(derived / "wellness.csv")}
    days = expand(plan, athlete, day, day)
    sessions = [s for s in (days[0].sessions if days else ()) if s.session.modality not in ("rest", "mobility")]

    endurance = [s for s in sessions if s.session.modality in ENDURANCE_MODALITIES]
    strength = [s for s in sessions if s.session.modality == "strength"]
    matched = match_activities(endurance + strength, activities)
    used = {a["id"] for a in matched.values()}
    items: list[dict] = []
    for k, s in enumerate(endurance + strength):
        if s.session.modality == "strength":
            items.append(strength_item(s, day, plan, athlete, history, readiness))
        else:
            items.append(endurance_item(s, matched.get(k), fetch, athlete))
    items.sort(key=lambda i: (i["slot"][-2:] if "slot" in i else "zz"))
    items += [unplanned_item(a, fetch, athlete) for a in activities if a["id"] not in used]

    w = wellness.get(iso, {})
    load_done = sum(_f(a.get("load")) or 0 for a in activities)
    context = {"readiness": w.get("readiness") or None, "readiness_reason": w.get("readiness_reason", ""),
               "sleep_h": _f(w.get("sleep_h")), "load_done": round(load_done), "load_planned": round(sum(s.tss for s in sessions))}  # fmt: skip
    today = day + dt.timedelta(days=1)
    today_days = expand(plan, athlete, today, today)
    today_sessions = list(today_days[0].sessions) if today_days else []
    return {
        "date": iso,
        "verdict": verdict(items, bool(sessions)),
        "note_for_today": note_for_today(items, context, today_sessions, wellness.get(today.isoformat(), {}).get("readiness") or None, athlete),
        "context": context,
        "items": items,
        "problems": fetch.problems,
    }


def to_markdown(b: dict, heading: str = "#") -> str:
    d = dt.date.fromisoformat(b["date"])
    c = b["context"]
    out = [f"{heading} Debrief: {_d(d)} {d.year}", "", f"**{b['verdict']}**", "", f"Note for today: {b['note_for_today']}", ""]
    ctx = [f"readiness {c['readiness']}" if c["readiness"] else "no readiness call"]
    if c["sleep_h"]:
        ctx.append(f"sleep {c['sleep_h']:g} h")
    ctx.append(f"load {c['load_done']} of {c['load_planned']} planned TSS")
    out += [" · ".join(ctx), ""]
    sub = heading + "#"
    for i in b["items"]:
        if i["kind"] == "unplanned":
            out.append(f"{sub} Unplanned: {i['name'] or i['type']} ({i['sport'].replace('_', ' ')})")
            bits = [f"{i['moving_min']:.0f} min" if i["moving_min"] else "", f"NP {i['np_w']:.0f} W" if i.get("np_w") else "",
                    f"IF {i['intensity']:.2f}" if i.get("intensity") else "", f"TSS {i['tss']:g}" if i.get("tss") else "",
                    f"HR load {i['hr_load']:g}" if i["hr_load"] else "", f"avg HR {i['avg_hr']:.0f}" if i["avg_hr"] else "",
                    f"HR drift {i['hr_drift_pct']:g}%" if i.get("hr_drift_pct") is not None else ""]  # fmt: skip
            out.append(" · ".join(x for x in bits if x) or "no detail")
            if i.get("grind_load"):
                out.append(_grind_md(i["grind_load"]))
            out.append("")
            continue
        out.append(f"{sub} {i['slot'][-2:].upper()}: {i['summary']}")
        if i["kind"] == "strength":
            out.append(f"{i['grade']}")
            out += ["", "| Lift | Set | Prescribed | Achieved | |", "|---|---|---|---|---|"]
            for lift in i["lifts"]:
                for x in lift["sets"]:
                    label = f"{x['role']} {x['set']}" if x["set"] else x["role"]
                    out.append(f"| {lift['name']} | {label} | {x['prescribed']} | {x['achieved'] or '-'} | {x['status']} |")
            out.append("")
            for lift in i["lifts"]:
                if lift["e1rm_after"] is not None:
                    before = f"{lift['e1rm_before']:g}" if lift["e1rm_before"] is not None else "-"
                    change = f" ({lift['e1rm_after'] - lift['e1rm_before']:+.1f})" if lift["e1rm_before"] is not None else ""
                    nxt = lift["next"]
                    nxt_s = (f"; next time {nxt['kg']:g} x {nxt['reps']} on {_d(dt.date.fromisoformat(nxt['date']))} ({nxt['reason']})"
                             if nxt and nxt.get("kg") is not None else "")  # fmt: skip
                    out.append(f"- {lift['name']}: e1RM {before} -> {lift['e1rm_after']:g}{change}{nxt_s}")
            out.append("")
            continue
        head = [i["grade"] + (f": {i['grade_note']}" if i.get("grade_note") else "")]
        if i.get("activity"):
            a = i["activity"]
            head += [f"{a['moving_min']:.0f} of {i['planned_min']} min" if a["moving_min"] else ""]
            head += [f"NP {i['np_w']:.0f} W" if i.get("np_w") else "", f"IF {i['intensity']:.2f}" if i.get("intensity") else "",
                     f"TSS {i['tss']:g}" if i.get("tss") else ""]  # fmt: skip
            if i.get("hr_drift_pct") is not None:
                head.append(f"HR drift {i['hr_drift_pct']:g}%")
            if i.get("fade_pct") is not None:
                head.append(f"fade {i['fade_pct']:g}%")
        out.append(" · ".join(x for x in head if x))
        if i.get("reps"):
            out += ["", "| Rep | Target | Watts | HR | |", "|---|---|---|---|---|"]
            out += [f"| {r['n']} | {r['target']} | {r['watts']} | {r['hr']:.0f} | {r['status']} |" if r["hr"] else
                    f"| {r['n']} | {r['target']} | {r['watts']} | - | {r['status']} |" for r in i["reps"]]  # fmt: skip
        if i.get("grind_load"):
            out.append(_grind_md(i["grind_load"]))
        out.append("")
    if b["problems"]:
        out += [f"_Some detail was unavailable: {'; '.join(b['problems'])}_", ""]
    return "\n".join(out)


def _grind_md(g: dict) -> str:
    return f"Grind Load: {g['efforts']} efforts, {g['work_min']:g} work min, {g['efforts_85']} at >= 85% LTHR ({g['source']})"


def write(b: dict, derived: Path) -> tuple[Path, Path]:
    derived.mkdir(parents=True, exist_ok=True)
    md, js = derived / "debrief.md", derived / "debrief.json"
    md.write_text(to_markdown(b), encoding="utf-8")
    js.write_text(json.dumps(b, indent=1) + "\n", encoding="utf-8")
    return md, js


def main(argv=None, *, env=None, transport=None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.review.debrief", description="Debrief a day.")
    parser.add_argument("--day", type=dt.date.fromisoformat, help="default: yesterday (schedule timezone)")
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path)
    parser.add_argument("--derived", type=Path, default=Path("derived"))
    parser.add_argument("--cache", type=Path, default=Path("cache"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="intervals.icu detail from the cache only")
    mode.add_argument("--no-network", action="store_true", help="only what's in derived/ (no intervals, streams or NP)")
    parser.add_argument("--print", dest="show", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        plan, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        return report_load_error(e)
    day = args.day or dt.datetime.now(athlete.schedule.zone()).date() - dt.timedelta(days=1)

    client = None
    if not args.no_network:
        if env is None:
            load_env()
        try:
            client = IntervalsClient(require_env("INTERVALS_ICU_API_KEY", env), require_env("INTERVALS_ICU_ATHLETE_ID", env),
                                     cache_dir=args.cache, offline=args.offline, transport=transport, sleep=sleep)  # fmt: skip
        except MissingSetting as e:
            print(f"error: {e} (or use --no-network)", file=sys.stderr)
            return 2
    try:
        b = build(day, plan, athlete, args.derived, Fetch(client))
    finally:
        if client is not None:
            client.close()
    md, js = write(b, args.derived)
    if args.show:
        print(md.read_text(encoding="utf-8"))
    print(f"wrote {md} and {js}: {b['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
