"""python -m pipeline.ingest --from 2026-10-14 [--to DATE] [--offline] [--skip-intervals]

Reads the strength log first (and stops on any error, before touching the network),
then fetches intervals.icu activities and wellness, writes the three tidy CSVs and
prints a short summary.
"""

import argparse
import datetime as dt
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

from pipeline.ingest.intervals import IntervalsClient, IntervalsError
from pipeline.ingest.strength import StrengthLogError, latest_top_sets, read_strength_log
from pipeline.ingest.tidy import (
    ACTIVITY_FIELDS,
    STRENGTH_FIELDS,
    WELLNESS_FIELDS,
    activity_row,
    latest_measured_wellness,
    strength_rows,
    upsert_range,
    wellness_row,
    write_csv,
)
from pipeline.plan.schema import PlanLoadError, load_checked, report_load_error
from pipeline.settings import MissingSetting, load_env, require_env


def _wellness_line(r: dict[str, str]) -> str:
    parts = []
    if r["resting_hr"]:
        parts.append(f"resting HR {r['resting_hr']} bpm")
    if r["hrv"]:
        parts.append(f"HRV {r['hrv']} ms")
    if r["sleep_h"]:
        parts.append(f"sleep {r['sleep_h']} h" + (f" (score {r['sleep_score']})" if r["sleep_score"] else ""))
    if r["readiness"]:
        parts.append(f"readiness {r['readiness']}")
    if r["ctl"]:
        parts.append(f"CTL {r['ctl']} / ATL {r['atl']} (form {r['form']})")
    return ", ".join(parts) or "no measurements"


def main(argv=None, *, env=None, transport: httpx.BaseTransport | None = None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.ingest", description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="start", type=dt.date.fromisoformat, required=True, help="first date")
    parser.add_argument("--to", dest="end", type=dt.date.fromisoformat, default=dt.date.today(), help="last date (default: today)")
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path, help="default: athlete.yaml beside the plan")
    parser.add_argument("--log", type=Path, default=Path("training/logs/strength.csv"))
    parser.add_argument("--out", type=Path, default=Path("derived"))
    parser.add_argument("--cache", type=Path, default=Path("cache"))
    parser.add_argument("--offline", action="store_true", help="read intervals.icu responses from the cache")
    parser.add_argument("--skip-intervals", action="store_true", help="only ingest the strength log")
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("--to is before --from")

    try:
        plan, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        return report_load_error(e)

    try:
        log = read_strength_log(args.log, plan, athlete)
    except StrengthLogError as e:
        print(f"{e.path}: INVALID - {len(e.errors)} error(s)")
        for line in e.errors:
            print(f"  {line}")
        return 1
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"Ingest {args.start} to {args.end}")

    if not args.skip_intervals:
        if env is None:
            load_env()
        try:
            api_key = require_env("INTERVALS_ICU_API_KEY", env)
            athlete_id = require_env("INTERVALS_ICU_ATHLETE_ID", env)
        except MissingSetting as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

        try:
            with IntervalsClient(
                api_key, athlete_id, cache_dir=args.cache, offline=args.offline, transport=transport, sleep=sleep
            ) as client:
                raw_activities = client.activities(args.start, args.end)
                raw_wellness = client.wellness(args.start, args.end)
        except IntervalsError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        activities = [activity_row(a) for a in raw_activities]
        wellness = [wellness_row(w) for w in raw_wellness]
        act_path, well_path = args.out / "activities.csv", args.out / "wellness.csv"
        act_total = upsert_range(act_path, ACTIVITY_FIELDS, "id", activities, args.start, args.end)
        well_total = upsert_range(well_path, WELLNESS_FIELDS, "date", wellness, args.start, args.end)

        sports = Counter(a["sport"] for a in activities)
        by_sport = ", ".join(f"{s} {n}" for s, n in sports.most_common()) or "none"
        print(f"Activities: {len(activities)} ({by_sport}) -> {act_path} ({act_total} rows total)")
        stubs = sum(a["detail"] == "stub" for a in activities)
        if stubs:
            print(f"  {stubs} came from Strava without detail (intervals.icu doesn't serve those via the API)")
        print(f"Wellness: {len(wellness)} days -> {well_path} ({well_total} rows total)")
        latest = latest_measured_wellness(wellness)
        if latest:
            print(f"  latest {latest['date']}: {_wellness_line(latest)}")

    strength_path = args.out / "strength.csv"
    rows = strength_rows(log)
    write_csv(strength_path, STRENGTH_FIELDS, rows)
    print(f"Strength: {len(rows)} sets from {args.log} -> {strength_path}")
    tops = latest_top_sets(log)
    width = max((len(lift) for lift in tops), default=0)
    for lift, s in tops.items():
        rpe = f" @ RPE {s.rpe:g}" if s.rpe is not None else ""
        print(f"  {lift:<{width}}  {s.date}  {s.kg:g} kg x {s.reps}{rpe}  ({s.role})  e1RM {s.e1rm_kg:.1f} kg")
    for w in log.warnings:
        print(f"  warning: {args.log} {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
