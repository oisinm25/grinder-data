"""python -m pipeline.review [--week-ending 2026-11-01 | --day 2026-10-27] [--offline | --no-intervals]

Grades every planned session of the week (Monday to the given Sunday, up to today) into
derived/compliance.csv, appends the week's record to derived/weekly.csv and prints the drift
check. `--day` grades just that day; a Sunday reviews its whole week (so Monday's daily run
completes the week Sunday's run could only partly see). Endurance power is graded from
intervals.icu activity intervals (cached in cache/); `--no-intervals` grades without them.
Run `python -m pipeline.ingest` first.
"""

import argparse
import datetime as dt
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

from pipeline.ingest.intervals import IntervalsClient, IntervalsError
from pipeline.ingest.tidy import read_csv, write_csv
from pipeline.plan.expand import expand
from pipeline.plan.schema import PlanLoadError, load_checked, report_load_error
from pipeline.prescribe import load_history, load_readiness, prescribe
from pipeline.review.compliance import FIELDS, grade_days, vacated_grades
from pipeline.review.drift import WEEKLY_FIELDS, review_week, week_markdown
from pipeline.settings import MissingSetting, load_env, require_env


def last_sunday(today: dt.date) -> dt.date:
    return today - dt.timedelta(days=(today.weekday() + 1) % 7)


def main(argv=None, *, env=None, transport: httpx.BaseTransport | None = None, sleep=time.sleep, today=None) -> int:
    today = today or dt.date.today()
    parser = argparse.ArgumentParser(prog="python -m pipeline.review", description="Planned against actual.")
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--week-ending", dest="end", type=dt.date.fromisoformat, default=last_sunday(today))
    when.add_argument("--day", type=dt.date.fromisoformat, help="grade one day (a Sunday reviews its week)")
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path)
    parser.add_argument("--derived", type=Path, default=Path("derived"))
    parser.add_argument("--cache", type=Path, default=Path("cache"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="activity intervals from the cache only")
    mode.add_argument("--no-intervals", action="store_true", help="grade without activity intervals")
    args = parser.parse_args(argv)
    day_only = args.day is not None and args.day.weekday() != 6
    if args.day is not None:
        args.end = args.day
    if not day_only and args.end.weekday() != 6:
        parser.error("--week-ending must be a Sunday")
    start = args.end if day_only else args.end - dt.timedelta(days=6)
    graded_to = min(args.end, today)
    if graded_to < start:
        print(f"{'day' if day_only else 'week'} {start} to {args.end} hasn't started yet")
        return 0

    try:
        plan, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        return report_load_error(e)
    d = args.derived
    if not (d / "activities.csv").exists() or not (d / "strength.csv").exists():
        print(f"error: {d}/activities.csv and {d}/strength.csv are needed; run python -m pipeline.ingest first", file=sys.stderr)
        return 2

    activities = read_csv(d / "activities.csv")
    wellness = read_csv(d / "wellness.csv")
    logged = load_history(d / "strength.csv")
    days = expand(plan, athlete, start, args.end)
    planned = [s for day in days if day.date <= graded_to for s in day.sessions]
    planned_tss = sum(s.tss for day in days for s in day.sessions)
    rx = prescribe(plan, athlete, logged, load_readiness(d / "wellness.csv"), start, graded_to)

    client = None
    if not args.no_intervals:
        if env is None:
            load_env()
        try:
            client = IntervalsClient(
                require_env("INTERVALS_ICU_API_KEY", env), require_env("INTERVALS_ICU_ATHLETE_ID", env),
                cache_dir=args.cache, offline=args.offline, transport=transport, sleep=sleep,
            )  # fmt: skip
        except MissingSetting as e:
            print(f"error: {e} (or use --no-intervals)", file=sys.stderr)
            return 2
    problems: list[str] = []

    def intervals_for(activity_id: str):
        if client is None:
            return None
        try:
            return client.activity_intervals(activity_id).get("icu_intervals") or []
        except IntervalsError as e:
            problems.append(f"activity {activity_id}: {e}")
            return None

    try:
        grades = grade_days(planned, activities, intervals_for, rx, logged)
    finally:
        if client is not None:
            client.close()
    grades += vacated_grades(days, plan.sessions, graded_to)  # moved or dropped by changes.yaml
    for g in grades:  # today isn't over: nothing done yet is pending, not skipped
        if g.date == today and g.grade == "skipped":
            g.grade, g.note = "pending", "not done yet today"

    kept = [r for r in read_csv(d / "compliance.csv") if not start.isoformat() <= r["date"] <= args.end.isoformat()]
    write_csv(d / "compliance.csv", FIELDS, sorted(kept + [g.row() for g in grades], key=lambda r: r["date"]))
    if day_only:
        counts = Counter(g.grade for g in grades)
        print(f"Day {start:%a %Y-%m-%d}: " + (", ".join(f"{k} {n}" for k, n in counts.most_common()) or "nothing planned"))
        for g in grades:
            if g.grade not in ("held", "done", "logged", "pending"):
                print(f"  {g.grade:<10} {g.session} {g.item if g.modality == 'strength' else ''}".rstrip() + (f": {g.note}" if g.note else ""))
        for p in problems:
            print(f"  note: no intervals for {p}")
        print(f"Wrote {len(grades)} grades to {d / 'compliance.csv'}")
        return 0
    previous = read_csv(d / "weekly.csv")
    week = review_week(plan, start, args.end, planned_tss, grades, activities, wellness, logged, previous, graded_to)
    others = [r for r in previous if r["week_end"] != args.end.isoformat()]
    write_csv(d / "weekly.csv", WEEKLY_FIELDS, sorted(others + [week.record], key=lambda r: r["week_end"]))
    (d / "week.md").write_text(week_markdown(week, grades, graded_to), encoding="utf-8")

    r = week.record
    partial = f" (graded to {graded_to})" if graded_to < args.end else ""
    print(f"Week {start} to {args.end}{partial}  {r['block']} / {r['week']}")
    print(f"Load: {r['planned_tss']} TSS planned, {r['actual_tss']} done" + (f" (target {r['tss_target']})" if r["tss_target"] else ""))
    endurance = Counter(g.grade for g in grades if g.modality != "strength")
    strength = Counter(g.grade for g in grades if g.modality == "strength")
    order = ("held", "faded", "off_target", "done", "missed", "skipped", "pending", "under", "over_rpe", "logged")
    print("Sessions: " + (", ".join(f"{k} {endurance[k]}" for k in order if endurance[k]) or "none graded"))
    print("Strength: " + (", ".join(f"{k} {strength[k]}" for k in order if strength[k]) or "none graded"))
    for g in grades:
        if g.grade not in ("held", "done", "logged", "pending"):
            print(f"  {g.grade:<10} {g.date:%a %d %b}  {g.session} {g.item if g.modality == 'strength' else ''}".rstrip()
                  + (f": {g.note}" if g.note else ""))  # fmt: skip
    print(f"Recovery: green {r['green']}, amber {r['amber']}, red {r['red']}, no call {r['no_call']}; "
          f"HRV 7d {r['hrv_7d'] or '-'} vs 28d {r['hrv_28d'] or '-'}; resting HR 7d {r['rhr_7d'] or '-'} vs 28d {r['rhr_28d'] or '-'}; "
          f"CTL {r['ctl'] or '-'} / ATL {r['atl'] or '-'} (form {r['form'] or '-'})")  # fmt: skip
    if r["bench_e1rm"]:
        print(f"Bench: e1RM {r['bench_e1rm']} vs line {r['bench_line']} ({float(r['bench_gap_kg']):+g} kg)")
    print("Flags:" + (" none" if not week.flags else ""))
    for f in week.flags:
        print(f"  - {f}")
    for p in problems:
        print(f"  note: no intervals for {p}")
    print(f"Wrote {len(grades)} grades to {d / 'compliance.csv'} and the week to {d / 'weekly.csv'} and {d / 'week.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
