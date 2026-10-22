"""python -m pipeline.publish --from DATE [--days 7 | --to DATE] [--push]

Builds one intervals.icu calendar event per planned session (rest days excluded):
endurance sessions carry their steps as workout text with targets in watts from
athlete.yaml; strength sessions carry the day's prescriptions. Every event's
external_id starts `pedestal:`, and on --push only those events are touched: existing
ones are updated, new ones created, and Pedestal events no longer in the plan deleted.
Nothing else on the calendar is changed.

Without --push nothing is sent: the events are written to
exports/intervals_events_<from>_<to>.json for inspection. The .zwo (bike) and .fit
(all endurance) fallback files are written to exports/workouts/ either way.
"""

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

from pipeline.ingest.intervals import IntervalsClient, IntervalsError
from pipeline.plan.expand import ENDURANCE_MODALITIES, ScheduledSession, expand
from pipeline.plan.schema import PlanLoadError, load_checked, report_load_error
from pipeline.prescribe import Prescription, load_history, load_readiness, prescribe
from pipeline.publish.formats import fit_workout, workout_text, zwo
from pipeline.settings import MissingSetting, load_env, require_env

PREFIX = "pedestal:"
EVENT_TYPE = {"handbike": "Handcycle", "run": "Run", "concept2": "Rowing", "grinding": "Workout",
              "strength": "WeightTraining", "mobility": "Workout"}  # fmt: skip
START_HOUR = {"am": 7, "pm": 17}


def event_type(s: ScheduledSession) -> str:
    if s.session.modality == "bike":
        return "Ride" if s.session.environment == "outdoor" else "VirtualRide"
    return EVENT_TYPE[s.session.modality]


def event_name(s: ScheduledSession) -> str:
    name = s.session.summary
    if s.session.modality == "grinding" and not name.lower().startswith("grind"):
        name = f"Grind: {name}"  # ingest maps titles starting "Grind" back to grinding
    return name


def external_id(plan_id: str, s: ScheduledSession) -> str:
    return f"{PREFIX}{plan_id}:{s.date}:{s.slot}:{s.order}"


def _has_provisional(steps) -> bool:
    return any(_has_provisional(st.steps) if st.kind == "repeat" else st.target.provisional for st in steps)


def strength_lines(items: list[Prescription], lift_names: dict[str, str]) -> list[str]:
    lines = []
    for p in items:
        kg = f"{p.kg:g} kg" if p.kg is not None else "choose weight"
        cap = f", RPE<={p.rpe_cap:g}" if p.rpe_cap is not None else ""
        lines.append(f"- {lift_names.get(p.lift, p.lift)} ({p.role}): {p.sets} x {p.reps} @ {kg}{cap}")
        lines.append(f"  {p.reason}")
    return lines


def build_event(plan_id: str, s: ScheduledSession, rx: list[Prescription], lift_names: dict[str, str]) -> dict:
    session = s.session
    lines = [session.summary, ""]
    if session.steps:
        lines += workout_text(s.steps)
        if _has_provisional(s.steps):
            lines += ["", "Watts from a provisional FTP in athlete.yaml."]
    elif session.modality == "strength":
        lines += strength_lines(rx, lift_names) or ["Run python -m pipeline.prescribe for weights."]
    if session.readiness_exempt:
        lines += ["", "Test: readiness-exempt (amber doesn't soften it; red still cuts it)."]
    hour = START_HOUR[s.slot[-2:]] + s.order
    event = {
        "category": "WORKOUT",
        "start_date_local": f"{s.date}T{hour:02d}:00:00",
        "type": event_type(s),
        "name": event_name(s),
        "description": "\n".join(lines).rstrip() + "\n\nPlanned by Pedestal",
        "external_id": external_id(plan_id, s),
    }
    if s.duration_s:
        event["moving_time"] = s.duration_s
    if s.tss:
        event["icu_training_load"] = round(s.tss)
    if session.environment in ("indoor", "outdoor"):
        event["indoor"] = session.environment == "indoor"
    elif event["type"] == "VirtualRide":
        event["indoor"] = True
    return event


def build(plan, athlete, history, readiness, start, end) -> tuple[list[dict], list[ScheduledSession]]:
    lift_names = {k: v.name for k, v in athlete.strength.lifts.items()}
    rx = prescribe(plan, athlete, history, readiness, start, end) if history is not None else []
    events, sessions = [], []
    for day in expand(plan, athlete, start, end):
        for s in day.sessions:
            if s.session.modality == "rest":
                continue
            mine = [p for p in rx if p.date == s.date and p.session == s.session_id]
            events.append(build_event(plan.plan_id, s, mine, lift_names))
            sessions.append(s)
    return events, sessions


def write_files(sessions: list[ScheduledSession], athlete, out_dir: Path) -> list[Path]:
    written = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for s in sessions:
        if s.session.modality not in ENDURANCE_MODALITIES or not s.steps:
            continue
        stem = f"{s.date}_{s.slot}_{s.order}_{s.session_id}"
        fit = out_dir / f"{stem}.fit"
        fit.write_bytes(fit_workout(event_name(s), s.session.modality, s.steps))
        written.append(fit)
        if s.session.modality == "bike":
            path = out_dir / f"{stem}.zwo"
            path.write_text(zwo(event_name(s), s.session.summary, s.steps, athlete.ftp["bike"].watts), encoding="utf-8")
            written.append(path)
    return written


def sync(client: IntervalsClient, events: list[dict], start: dt.date, end: dt.date) -> tuple[int, int, int]:
    """Create, update and delete Pedestal's own events in start..end. Returns (created, updated, deleted)."""
    existing = {e["external_id"]: e for e in client.events(start, end) if str(e.get("external_id") or "").startswith(PREFIX)}
    wanted = {e["external_id"] for e in events}
    new = [e for e in events if e["external_id"] not in existing]
    for e in events:
        if e["external_id"] in existing:
            client.update_event(existing[e["external_id"]]["id"], e)
    if new:
        client.create_events(new)
    stale = [e for eid, e in existing.items() if eid not in wanted]
    for e in stale:
        client.delete_event(e["id"])
    return len(new), len(events) - len(new), len(stale)


def main(argv=None, *, env=None, transport: httpx.BaseTransport | None = None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.publish", description="Publish planned sessions.")
    parser.add_argument("--from", dest="start", type=dt.date.fromisoformat, default=dt.date.today())
    span = parser.add_mutually_exclusive_group()
    span.add_argument("--days", type=int, default=7)
    span.add_argument("--to", dest="end", type=dt.date.fromisoformat)
    parser.add_argument("--push", action="store_true", help="write to the intervals.icu calendar (default: dry run)")
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path)
    parser.add_argument("--strength", type=Path, default=Path("derived/strength.csv"))
    parser.add_argument("--wellness", type=Path, default=Path("derived/wellness.csv"))
    parser.add_argument("--exports", type=Path, default=Path("exports"))
    args = parser.parse_args(argv)

    try:
        plan, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        return report_load_error(e)
    end = args.end or args.start + dt.timedelta(days=args.days - 1)
    history = load_history(args.strength) if args.strength.exists() else None
    events, sessions = build(plan, athlete, history, load_readiness(args.wellness), args.start, end)
    files = write_files(sessions, athlete, args.exports / "workouts")

    print(f"Publish {args.start} to {end}: {len(events)} sessions")
    for e in events:
        load = f"{e['icu_training_load']:>4} TSS" if "icu_training_load" in e else " " * 8
        print(f"  {e['start_date_local'][:16].replace('T', ' ')}  {e['type']:<15} {load}  {e['name']}")
    print(f"Files: {len(files)} in {args.exports / 'workouts'} (.fit for endurance, .zwo for bike)")
    if history is None:
        print(f"  note: {args.strength} not found, so strength events have no weights (run python -m pipeline.ingest)")

    if not args.push:
        payload = args.exports / f"intervals_events_{args.start}_{end}.json"
        payload.write_text(json.dumps(events, indent=1), encoding="utf-8")
        print(f"Dry run: nothing sent. Events written to {payload}; re-run with --push to send them.")
        return 0

    if env is None:
        load_env()
    try:
        api_key = require_env("INTERVALS_ICU_API_KEY", env)
        athlete_id = require_env("INTERVALS_ICU_ATHLETE_ID", env)
    except MissingSetting as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        with IntervalsClient(api_key, athlete_id, transport=transport, sleep=sleep) as client:
            created, updated, deleted = sync(client, events, args.start, end)
    except IntervalsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"intervals.icu: {created} created, {updated} updated, {deleted} stale Pedestal events deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
