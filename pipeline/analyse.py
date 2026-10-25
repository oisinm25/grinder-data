"""On-demand workout analysis (docs/roadmap.md, v1.4).

    python -m pipeline.analyse --activity latest|<id> [--no-ingest] [--offline]

1. Ingests the last 3 days (activities, wellness, strength log) into derived/.
2. With `--activity latest`: if the newest activity is the one derived/analysis/latest.json last
   analysed (status ok), writes status `no_new_activity` and stops. An explicit id always runs.
3. Breakdown, reusing the daily debrief (pipeline.review.debrief, which grades through
   pipeline.review.compliance and prescribes through pipeline.prescribe): the planned session
   it matches (same day and sport), intervals against their targets, NP, IF, TSS, HR drift;
   strength sets against the prescription and e1RM change; grinding reps, fade, Grind Load.
   Plus power-curve points (best average watts over 5 s, 1, 4, 5, 20 and 60 min) from the
   watts stream, gaps over 5 s counted as zero.
4. Fitness impact, with the v1.3 load model (pipeline.dashboard.load): the session's load;
   CTL, ATL and form at the end of the day without it and with it; projected form tomorrow
   (the end-of-day values: form going into tomorrow) and going into the next key session
   (the next planned test, top-set strength session, power step at >= 90% FTP, or ride of
   3 h or more), running the model over the plan's daily load; and the share of the week's
   `weekly_tss` done so far.
5. New bests (power-curve points against derived/analysis/bests.json, kept since the first
   analysis; e1RM against every logged set) and **anchor flags**: a 20-min best x 0.95 more
   than 3% above that modality's FTP in athlete.yaml, or a grinding 1- or 4-min best more than
   3% above its curve point. Flags only: it never edits athlete.yaml or the plan.
6. Writes derived/analysis/<date>-<activity_id>.json and .md, and latest.json (status ok,
   no_new_activity or error). The workflow commits derived/analysis/ to the data branch.
7. Never publishes to the calendar and never touches today.json (nor anything but
   derived/analysis/ and what ingest writes).
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

from pipeline.athlete import Athlete
from pipeline.dashboard import data as dash_data
from pipeline.dashboard.load import activity_load, daily_loads, planned_loads, run_model, seed
from pipeline.ingest.__main__ import main as ingest_main
from pipeline.ingest.intervals import IntervalsClient, IntervalsError
from pipeline.ingest.strength import TOP_ROLES, e1rm
from pipeline.ingest.tidy import activity_row
from pipeline.plan.expand import ENDURANCE_MODALITIES, expand
from pipeline.plan.schema import Plan, PlanLoadError, load_checked
from pipeline.prescribe import load_history, load_readiness
from pipeline.review.compliance import match_activities
from pipeline.review.debrief import Fetch, endurance_item, hard_session, strength_item, unplanned_item
from pipeline.settings import MissingSetting, load_env, require_env

INGEST_DAYS = 3
CURVE_SECONDS = (5, 60, 240, 300, 1200, 3600)
FTP_FROM_20MIN = 0.95
FLAG_PCT = 3.0
LONG_RIDE_S = 3 * 3600
SECRETS = ("INTERVALS_ICU_API_KEY", "NTFY_TOPIC")


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


def _secs(s: int) -> str:
    return f"{s} s" if s < 60 else f"{s // 60} min"


# ------------------------------------------------------------------ measures


def power_curve(streams: dict) -> dict[int, int]:
    """Best average watts over each of CURVE_SECONDS, from a 1 Hz rebuild of the watts stream."""
    t, w = streams.get("time") or [], streams.get("watts") or []
    pairs = [(ti, wi or 0) for ti, wi in zip(t, w) if ti is not None]
    if len(pairs) < 2:
        return {}
    series: list[float] = []
    for (t0, w0), (t1, _) in zip(pairs, pairs[1:]):
        gap = max(int(t1 - t0), 1)
        series += [w0] + [w0 if gap <= 5 else 0] * (gap - 1)  # short gaps carry the value; pauses count as 0
    series.append(pairs[-1][1])
    prefix = [0.0]
    for v in series:
        prefix.append(prefix[-1] + v)
    out = {}
    for s in CURVE_SECONDS:
        if len(series) >= s:
            out[s] = round(max(prefix[i + s] - prefix[i] for i in range(len(series) - s + 1)) / s)
    return out


def anchor_flags(sport: str, curve: dict[int, int], athlete: Athlete) -> list[dict]:
    flags = []
    ftp = athlete.ftp.get(sport)
    if ftp and 1200 in curve:
        est = curve[1200] * FTP_FROM_20MIN
        pct = (est / ftp.watts - 1) * 100
        if pct > FLAG_PCT:
            flags.append({"anchor": f"ftp.{sport}", "current_w": ftp.watts, "suggested_w": round(est), "change_pct": round(pct, 1),
                          "evidence": f"20-min best {curve[1200]} W x {FTP_FROM_20MIN}"})  # fmt: skip
    if sport == "grinding":
        for secs, key in ((60, "grinding_60s"), (240, "grinding_240s")):
            point = athlete.curves.get(key)
            if point and secs in curve and (curve[secs] / point.watts - 1) * 100 > FLAG_PCT:
                flags.append({"anchor": f"curves.{key}", "current_w": point.watts, "suggested_w": curve[secs],
                              "change_pct": round((curve[secs] / point.watts - 1) * 100, 1), "evidence": f"{_secs(secs)} best {curve[secs]} W"})  # fmt: skip
    return flags


# ------------------------------------------------------------------ selecting the activity


def pick_activity(choice: str, activities: list[dict], fetch: Fetch, history: list) -> dict | None:
    if choice == "latest":
        return max(activities, key=lambda a: (a["start_local"], a["id"]), default=None)
    found = next((a for a in activities if a["id"] == choice), None)
    if found is None:  # older than the ingest window: fetch it on its own
        detail = fetch.detail(choice)
        if detail:
            found = activity_row(detail, frozenset(r.date.isoformat() for r in history))
    return found


# ------------------------------------------------------------------ analysis


def breakdown(activity: dict, plan: Plan, athlete: Athlete, derived: Path, fetch: Fetch) -> dict:
    day = dt.date.fromisoformat(activity["date"])
    days = expand(plan, athlete, day, day)
    planned = [s for s in (days[0].sessions if days else ()) if s.session.modality in ENDURANCE_MODALITIES + ("strength",)]
    match = match_activities(planned, [activity])
    session = planned[next(iter(match))] if match else None
    history = load_history(derived / "strength.csv") if (derived / "strength.csv").exists() else []
    if session is None:
        item = unplanned_item(activity, fetch, athlete)
    elif session.session.modality == "strength":
        item = strength_item(session, day, plan, athlete, history, load_readiness(derived / "wellness.csv"))
    else:
        item = endurance_item(session, activity, fetch, athlete)
    detail = fetch.detail(activity["id"]) or {}
    intensity = _f(detail.get("icu_intensity")) or _f(activity.get("intensity"))
    item.setdefault("np_w", _f(detail.get("icu_weighted_avg_watts")) or _f(activity.get("np_watts")))
    item.setdefault("intensity", round(intensity / 100, 2) if intensity else None)
    item.setdefault("tss", _f(detail.get("icu_training_load")) or _f(activity.get("load")))
    if item.get("hr_drift_pct") is None and _f(detail.get("decoupling")) is not None:
        item["hr_drift_pct"] = round(_f(detail["decoupling"]), 1)
    streams = fetch.streams(activity["id"]) if activity["sport"] in ("bike", "handbike", "grinding", "concept2") else {}
    item["power_curve"] = {str(k): v for k, v in power_curve(streams).items()}
    return item


def fitness(activity: dict, plan: Plan, athlete: Athlete, derived: Path, fetch: Fetch) -> dict:
    day = dt.date.fromisoformat(activity["date"])
    activities = [a for a in _rows(derived / "activities.csv") if a["date"] <= activity["date"]]
    if activity["id"] not in {a["id"] for a in activities}:
        activities.append(activity)
    metrics = dash_data.update_metrics(derived, activities, fetch, athlete)
    base, corr, source = activity_load(activity, metrics.get(activity["id"]))
    session_load = base + corr
    wellness = _rows(derived / "wellness.csv")
    start = min(dt.date.fromisoformat(a["date"]) for a in activities)
    ctl0, atl0 = seed(wellness, start)
    loads = daily_loads(activities, metrics)
    after = run_model(loads, start, day, ctl0, atl0)[-1]
    without = dict(loads)
    without[day] = without.get(day, 0) - session_load
    before = run_model(without, start, day, ctl0, atl0)[-1]

    key = next_key_session(plan, athlete, day)
    proj = None
    if key:
        ahead = run_model(planned_loads(plan, athlete, day + dt.timedelta(days=1), key[0] - dt.timedelta(days=1)),
                          day + dt.timedelta(days=1), key[0] - dt.timedelta(days=1), after.ctl, after.atl)  # fmt: skip
        state = ahead[-1] if ahead else after
        proj = {"date": key[0].isoformat(), "session": key[1], "summary": key[2], "form": round(state.form, 1)}

    monday = day - dt.timedelta(days=day.weekday())
    week_done = sum(v for d, v in loads.items() if monday <= d <= day)
    band, label = None, None
    for b in plan.blocks:
        if b.start <= day <= b.end:
            w = max((w for w in b.weeks or [] if w.start <= day), key=lambda w: w.start, default=None)
            band = list(w.weekly_tss if w and w.weekly_tss else b.weekly_tss or []) or None
            label = w.label if w else b.id
    return {
        "session_load": round(session_load, 1), "load_source": source, "grind_correction": corr,
        "before": {"ctl": round(before.ctl, 1), "atl": round(before.atl, 1), "form": round(before.form, 1)},
        "after": {"ctl": round(after.ctl, 1), "atl": round(after.atl, 1), "form": round(after.form, 1)},
        "projections": {"form_tomorrow": round(after.form, 1), "next_key_session": proj},
        "week": {"start": monday.isoformat(), "label": label, "done_tss": round(week_done),
                 "band": band, "share_pct": [round(week_done / band[1] * 100), round(week_done / band[0] * 100)] if band and band[0] else None},
    }  # fmt: skip


def next_key_session(plan: Plan, athlete: Athlete, day: dt.date) -> tuple[dt.date, str, str] | None:
    for d in expand(plan, athlete, day + dt.timedelta(days=1), plan.valid_to):
        for s in d.sessions:
            long_ride = s.session.modality == "bike" and (s.duration_s or 0) >= LONG_RIDE_S
            if s.session.readiness_exempt or long_ride or hard_session(s, athlete):
                return d.date, s.session_id, s.session.summary
    return None


def bests(activity: dict, item: dict, derived: Path, history_sets: list) -> tuple[list[dict], dict]:
    """New bests in this activity, and the updated record (not saved here)."""
    path = derived / "analysis" / "bests.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {"since": activity["date"], "power": {}}
    found = []
    sport = record["power"].setdefault(activity["sport"], {})
    for secs, watts in (item.get("power_curve") or {}).items():
        prev = sport.get(secs)
        if prev is None or watts > prev["watts"]:
            found.append({"kind": "power", "sport": activity["sport"], "duration": _secs(int(secs)), "watts": watts,
                          "previous": prev, "first": prev is None})  # fmt: skip
            sport[secs] = {"watts": watts, "date": activity["date"], "activity": activity["id"]}
    for lift in item.get("lifts", []):
        today = [r for r in history_sets if r.lift == lift["lift"] and r.date.isoformat() == activity["date"]]
        earlier = [r for r in history_sets if r.lift == lift["lift"] and r.date.isoformat() < activity["date"] and (r.role in TOP_ROLES or r.rpe)]
        if today:
            best = max(e1rm(r.kg, r.reps, r.rpe) for r in today)
            prev = max((e1rm(r.kg, r.reps, r.rpe) for r in earlier), default=None)
            if prev is None or best > prev:
                found.append({"kind": "e1rm", "lift": lift["lift"], "name": lift["name"], "e1rm": round(best, 1),
                              "previous": round(prev, 1) if prev else None, "first": prev is None})  # fmt: skip
    return found, record


# ------------------------------------------------------------------ output


def to_markdown(r: dict) -> str:
    a, item, fit = r["activity"], r["breakdown"], r["fitness"]
    day = dt.date.fromisoformat(a["date"])
    title = item.get("summary") or a["name"] or a["type"]
    out = [f"# Workout analysis: {_d(day)} {day.year}", "", f"**{title}**" + (f" ({a['name']})" if a["name"] and a["name"] != title else ""), ""]
    for flag in r["anchor_flags"]:
        out += [f"> **Anchor flag: {flag['anchor']}**: {flag['evidence']} = {flag['suggested_w']} W, {flag['change_pct']:+g}% against the "
                f"{flag['current_w']:g} W anchor. Pedestal never changes `config/athlete.yaml`; update it yourself if you agree.", ""]  # fmt: skip
    out.append("## Breakdown")
    head = [item.get("grade", "unplanned"), f"{_f(a['moving_min']):.0f} min" if _f(a.get("moving_min")) else "",
            f"NP {item['np_w']:.0f} W" if item.get("np_w") else "", f"IF {item['intensity']:.2f}" if item.get("intensity") else "",
            f"TSS {item['tss']:g}" if item.get("tss") else "",
            f"HR drift {item['hr_drift_pct']:g}%" if item.get("hr_drift_pct") is not None else ""]  # fmt: skip
    out += [" · ".join(x for x in head if x), ""]
    if item.get("grade_note"):
        out += [item["grade_note"], ""]
    if item.get("reps"):
        out += ["| Rep | Target | Watts | HR | |", "|---|---|---|---|---|"]
        out += [f"| {x['n']} | {x['target']} | {x['watts']} | {x['hr']:.0f} | {x['status']} |" if x.get("hr") else
                f"| {x['n']} | {x['target']} | {x['watts']} | - | {x['status']} |" for x in item["reps"]] + [""]  # fmt: skip
    if item.get("fade_pct") is not None:
        out += [f"Fade across reps: {item['fade_pct']:g}%", ""]
    if item.get("grind_load"):
        g = item["grind_load"]
        out += [f"Grind Load: {g['efforts']} efforts, {g['work_min']:g} work min, {g['efforts_85']} at >= 85% LTHR ({g['source']})", ""]
    for lift in item.get("lifts", []):
        out += [f"**{lift['name']}**", ""] + [f"- {x['role']} {x['set'] or ''}: {x['prescribed']} -> {x['achieved'] or '-'} ({x['status']})" for x in lift["sets"]]
        if lift.get("e1rm_after") is not None:
            change = f" ({lift['e1rm_after'] - lift['e1rm_before']:+.1f})" if lift.get("e1rm_before") is not None else ""
            out.append(f"- e1RM {lift['e1rm_before'] if lift.get('e1rm_before') is not None else '-'} -> {lift['e1rm_after']:g}{change}")
        out.append("")
    if item.get("power_curve"):
        out += ["Power curve: " + " · ".join(f"{_secs(int(k))} {v} W" for k, v in item["power_curve"].items()), ""]
    b, af, p, w = fit["before"], fit["after"], fit["projections"], fit["week"]
    out += ["## Fitness impact", "",
            f"Session load {fit['session_load']:g} TSS ({fit['load_source']}" + (f", +{fit['grind_correction']:g} Grind Load" if fit["grind_correction"] else "") + ").",
            f"CTL {b['ctl']:g} -> {af['ctl']:g} · ATL {b['atl']:g} -> {af['atl']:g} · form {b['form']:g} -> {af['form']:g}.",
            f"Form going into tomorrow: {p['form_tomorrow']:g}."]  # fmt: skip
    if p["next_key_session"]:
        k = p["next_key_session"]
        out.append(f"Going into the next key session, {k['summary']} on {_d(dt.date.fromisoformat(k['date']))}, with the planned load: form {k['form']:g}.")
    if w["band"]:
        out.append(f"Week {w['label'] or ''} (from {_d(dt.date.fromisoformat(w['start']))}): {w['done_tss']} of {w['band'][0]:g}-{w['band'][1]:g} TSS done "
                   f"({w['share_pct'][0]}-{w['share_pct'][1]}%).")  # fmt: skip
    out.append("")
    out.append("## New bests")
    if r["bests"]:
        for x in r["bests"]:
            if x["kind"] == "power":
                prev = "first recorded" if x["first"] else f"was {x['previous']['watts']} W on {x['previous']['date']}"
                out.append(f"- {x['sport']} {x['duration']}: {x['watts']} W ({prev})")
            else:
                prev = "first recorded" if x["first"] else f"was {x['previous']:g}"
                out.append(f"- {x['name']} e1RM {x['e1rm']:g} kg ({prev})")
    else:
        out.append("None.")
    out += ["", f"_Generated {r['generated_at']}. Power bests are tracked since {r['bests_since']}._", ""]
    return "\n".join(out)


def write_latest(out_dir: Path, payload: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")


def analyse(choice: str, plan: Plan, athlete: Athlete, derived: Path, fetch: Fetch, now: dt.datetime) -> dict:
    out_dir = derived / "analysis"
    history = load_history(derived / "strength.csv") if (derived / "strength.csv").exists() else []
    activities = [a for a in _rows(derived / "activities.csv") if a["date"] <= now.date().isoformat()]
    generated = now.isoformat(timespec="minutes")
    activity = pick_activity(choice, activities, fetch, history)
    try:
        last = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    if activity is None or (choice == "latest" and last.get("status") == "ok" and last.get("activity_id") == activity["id"]):
        payload = {"status": "no_new_activity", "generated_at": generated, "activity_id": last.get("activity_id"),
                   "requested": choice, "last_analysis": last.get("files", {}).get("md")}  # fmt: skip
        if activity is None and choice != "latest":
            payload = {"status": "error", "generated_at": generated, "activity_id": choice, "error": f"activity {choice} not found"}
        write_latest(out_dir, payload)
        return payload

    item = breakdown(activity, plan, athlete, derived, fetch)
    fit = fitness(activity, plan, athlete, derived, fetch)
    found, record = bests(activity, item, derived, history)
    flags = anchor_flags(activity["sport"], {int(k): v for k, v in item.get("power_curve", {}).items()}, athlete)
    stem = f"{activity['date']}-{activity['id']}"
    result = {
        "status": "ok", "generated_at": generated, "activity_id": activity["id"],
        "activity": {k: activity.get(k) for k in ("id", "date", "start_local", "sport", "type", "name", "moving_min")},
        "breakdown": item, "fitness": fit, "projections": fit["projections"], "bests": found,
        "bests_since": record.get("since"), "anchor_flags": flags,
        "files": {"json": f"analysis/{stem}.json", "md": f"analysis/{stem}.md"},
    }  # fmt: skip
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    (out_dir / f"{stem}.md").write_text(to_markdown(result), encoding="utf-8")
    (out_dir / "bests.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    write_latest(out_dir, result)
    return result


def _scrub(text: str, env) -> str:
    for name in SECRETS:
        v = (env.get(name) or "").strip()
        if len(v) >= 4:
            text = text.replace(v, "***")
    return text


def main(argv=None, *, env=None, transport=None, sleep=time.sleep, now: dt.datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.analyse", description="Analyse one workout now.")
    parser.add_argument("--activity", default="latest", help='"latest" or an intervals.icu activity id')
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path)
    parser.add_argument("--derived", type=Path, default=Path("derived"))
    parser.add_argument("--cache", type=Path, default=Path("cache"))
    parser.add_argument("--no-ingest", action="store_true", help="use derived/ as it is")
    parser.add_argument("--offline", action="store_true", help="intervals.icu detail and streams from the cache only")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if not re.fullmatch(r"latest|[A-Za-z0-9_-]{1,40}", args.activity):
        parser.error("--activity must be 'latest' or an activity id")
    if env is None:
        load_env()
        env = os.environ
    out_dir = args.derived / "analysis"
    try:
        plan, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        write_latest(out_dir, {"status": "error", "generated_at": dt.datetime.now().isoformat(timespec="minutes"), "error": e.lines[0]})
        print("\n".join(e.lines))
        return 1
    now = now.astimezone(athlete.schedule.zone()) if now else dt.datetime.now(athlete.schedule.zone())
    client = None
    try:
        if not args.no_ingest:
            start = now.date() - dt.timedelta(days=INGEST_DAYS - 1)
            code = ingest_main(["--from", start.isoformat(), "--to", now.date().isoformat(), "--out", str(args.derived)],
                               env=env, transport=transport, sleep=sleep)  # fmt: skip
            if code != 0:
                raise RuntimeError(f"ingest failed (exit {code})")
        try:
            client = IntervalsClient(require_env("INTERVALS_ICU_API_KEY", env), require_env("INTERVALS_ICU_ATHLETE_ID", env),
                                     cache_dir=args.cache, offline=args.offline, transport=transport, sleep=sleep)  # fmt: skip
        except MissingSetting:
            client = None
        result = analyse(args.activity, plan, athlete, args.derived, Fetch(client), now)
    except (IntervalsError, RuntimeError, OSError, ValueError, KeyError) as e:
        message = _scrub(f"{type(e).__name__}: {e}", env)
        write_latest(out_dir, {"status": "error", "generated_at": now.isoformat(timespec="minutes"), "activity_id": args.activity, "error": message})
        print(_scrub(traceback.format_exc(), env), file=sys.stderr)
        print(f"error: {message}")
        return 1
    finally:
        if client is not None:
            client.close()
    if result["status"] == "ok":
        flags = "; ".join(f"{f['anchor']} -> {f['suggested_w']} W" for f in result["anchor_flags"]) or "none"
        print(f"analysed {result['activity_id']} ({result['activity']['date']}): wrote {result['files']['md']}; anchor flags: {flags}")
    else:
        print(f"{result['status']}: " + (result.get("error") or f"newest activity {result.get('activity_id')} is already analysed"))
    return 0 if result["status"] != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
