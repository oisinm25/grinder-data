"""python -m pipeline.dashboard [--day DATE] [--offline | --no-network]

Writes derived/dashboard.json, derived/dashboard.html and derived/email/ (PNG charts,
dashboard_email.html, manifest.json). Run after ingest (and debrief, for the yesterday card).
"""

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

from pipeline.dashboard import data, html, mail
from pipeline.ingest.intervals import IntervalsClient
from pipeline.plan.schema import PlanLoadError, load_checked, report_load_error
from pipeline.review.debrief import Fetch
from pipeline.settings import MissingSetting, load_env, require_env


def build_all(plan, athlete, derived: Path, today: dt.date, fetch: Fetch, goals_md: Path) -> tuple[dict, dict]:
    d = data.build(plan, athlete, derived, today, fetch, goals_md)
    data.write(d, derived)
    (derived / "dashboard.html").write_text(html.render(d), encoding="utf-8")
    return d, mail.write(d, derived)


def main(argv=None, *, env=None, transport=None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.dashboard", description="Build the dashboard.")
    parser.add_argument("--day", type=dt.date.fromisoformat, help="default: today (schedule timezone)")
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path)
    parser.add_argument("--derived", type=Path, default=Path("derived"))
    parser.add_argument("--cache", type=Path, default=Path("cache"))
    parser.add_argument("--goals", type=Path, default=Path("training/goals.md"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--no-network", action="store_true", help="skip new per-activity metrics (streams)")
    args = parser.parse_args(argv)
    try:
        plan, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        return report_load_error(e)
    today = args.day or dt.datetime.now(athlete.schedule.zone()).date()
    client = None
    if not args.no_network:
        if env is None:
            load_env()
        try:
            client = IntervalsClient(require_env("INTERVALS_ICU_API_KEY", env), require_env("INTERVALS_ICU_ATHLETE_ID", env),
                                     cache_dir=args.cache, offline=args.offline, transport=transport, sleep=sleep)  # fmt: skip
        except MissingSetting:
            client = None
    try:
        d, manifest = build_all(plan, athlete, args.derived, today, Fetch(client), args.goals)
    finally:
        if client is not None:
            client.close()
    last = (d["charts"]["fitness"]["days"] or [{}])[-1]
    print(f"wrote dashboard.json, dashboard.html and email/ ({len(manifest['images'])} charts): "
          f"CTL {last.get('ctl', '-')}, ATL {last.get('atl', '-')}, form {last.get('form', '-')}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
