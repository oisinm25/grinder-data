"""python -m pipeline.ingest --from 2026-10-14 [--to DATE] [--offline] [--skip-intervals]

Reads the strength log first (and stops on any error, before touching the network),
then fetches intervals.icu activities and wellness, writes the tidy CSVs (activities,
wellness, zones, strength, heavy_reps) and prints a short summary.
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
    HEAVY_FIELDS,
    STRENGTH_FIELDS,
    WELLNESS_FIELDS,
    activity_row,
    heavy_rows,
    add_readiness,
    apply_body_log,
    latest_measured_wellness,
    read_csv,
    strength_rows,
    upsert_range,
    wellness_row,
    write_csv,
)
from pipeline.ingest.zones import ZONE_FIELDS, activity_zone_rows, merge_zone_rows
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
    if r["garmin_readiness"]:
        parts.append(f"Garmin readiness {r['garmin_readiness']}")
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
    parser.add_argument("--body", type=Path, default=Path("training/logs/body.csv"), help="bodyweights that override intervals.icu's")
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

        rules = athlete.readiness.rules
        wellness_from = args.start - dt.timedelta(days=rules.baseline_days)  # so day one has a baseline
        try:
            with IntervalsClient(
                api_key, athlete_id, cache_dir=args.cache, offline=args.offline, transport=transport, sleep=sleep
            ) as client:
                raw_activities = client.activities(args.start, args.end)
                raw_wellness = client.wellness(wellness_from, args.end)
                strength_dates = frozenset(s.date.isoformat() for _, s in log.sets)
                activities = [activity_row(a, strength_dates) for a in raw_activities]
                zone_rows, zone_sources = _zones(args.out / "zones.csv", activities, raw_activities, athlete, client)
        except IntervalsError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        act_path, well_path = args.out / "activities.csv", args.out / "wellness.csv"
        act_total = upsert_range(act_path, ACTIVITY_FIELDS, "id", activities, args.start, args.end)
        body = read_csv(args.body) if args.body.exists() else []
        upsert_range(well_path, WELLNESS_FIELDS, "date", apply_body_log([wellness_row(w) for w in raw_wellness], body), wellness_from, args.end)
        wellness = add_readiness(read_csv(well_path), rules)
        write_csv(well_path, WELLNESS_FIELDS, wellness)

        zones_path = args.out / "zones.csv"
        merged = merge_zone_rows(read_csv(zones_path), zone_rows, {a["id"] for a in activities}, args.start.isoformat(), args.end.isoformat())
        write_csv(zones_path, ZONE_FIELDS, merged)

        sports = Counter(a["sport"] for a in activities)
        by_sport = ", ".join(f"{s} {n}" for s, n in sports.most_common()) or "none"
        print(f"Activities: {len(activities)} ({by_sport}) -> {act_path} ({act_total} rows total)")
        for a in activities:
            if a["review"]:
                print(f"  review: {a['date']} {a['name'] or '(untitled)'!r} [{a['type']}] -> {a['sport']}: {a['review']}")
        stubs = sum(a["detail"] == "stub" for a in activities)
        if stubs:
            print(f"  {stubs} came from Strava without detail (intervals.icu doesn't serve those via the API)")

        print(f"Zones: {sum(zone_sources.values())} activities ({', '.join(f'{k} {n}' for k, n in sorted(zone_sources.items())) or 'none'})"
              f" -> {zones_path}")  # fmt: skip

        in_range = [r for r in wellness if args.start.isoformat() <= r["date"] <= args.end.isoformat()]
        print(f"Wellness: {len(in_range)} days in range -> {well_path} ({len(wellness)} rows incl. baseline)")
        latest = latest_measured_wellness(in_range)
        if latest:
            print(f"  latest {latest['date']}: {_wellness_line(latest)}")
            print(f"  readiness {latest['readiness_reason']}")

    strength_path = args.out / "strength.csv"
    rows = strength_rows(log)
    write_csv(strength_path, STRENGTH_FIELDS, rows)
    print(f"Strength: {len(rows)} sets from {args.log} -> {strength_path}")
    heavy = heavy_rows(rows)
    write_csv(args.out / "heavy_reps.csv", HEAVY_FIELDS, heavy)
    print(f"Heavy reps: {sum(int(r['heavy_reps']) for r in heavy)} reps at >= 90% e1RM in {len(heavy)} sets -> {args.out / 'heavy_reps.csv'}")
    tops = latest_top_sets(log)
    width = max((len(lift) for lift in tops), default=0)
    for lift, s in tops.items():
        rpe = f" @ RPE {s.rpe:g}" if s.rpe is not None else ""
        print(f"  {lift:<{width}}  {s.date}  {s.kg:g} kg x {s.reps}{rpe}  ({s.role})  e1RM {s.e1rm_kg:.1f} kg")
    for w in log.warnings:
        print(f"  warning: {args.log} {w}")
    return 0


def _zones(path: Path, activities: list[dict], raw: list[dict], athlete, client: IntervalsClient):
    """zones.csv rows for this fetch, and how many activities came from each source. An activity whose
    zones were already recomputed from streams keeps those rows (its streams are fetched once)."""
    done = {}
    for r in read_csv(path):
        if r["source"] == "streams":
            done.setdefault(r["activity_id"], []).append(r)

    def streams(aid: str):
        try:
            return client.activity_streams(aid)
        except IntervalsError:
            return None  # offline without a cached copy, or not served: keep intervals.icu's seconds

    rows, sources = [], Counter()
    for row, a in zip(activities, raw):
        got = done.get(row["id"]) or activity_zone_rows(row, a, athlete, streams)
        if got:
            sources["streams" if any(r["source"] == "streams" for r in got) else "icu"] += 1
        rows += got
    return rows, sources


if __name__ == "__main__":
    sys.exit(main())
