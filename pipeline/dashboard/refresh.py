"""python -m pipeline.dashboard.refresh [--pending DIR] [--activity latest] [--timeout 720] [--no-request]

The git half of "Refresh Pedestal" (README.md), in order:

1. With --pending: apply the items Pedestal Coach saved (pipeline.coach.pending) to
   training/logs/changes.yaml, strength.csv and body.csv, commit only those files and push main.
   Each item's outcome goes to --pending-results for Claude to mark in Coach's store.
2. Push requests/<UTC timestamp>.json to the `requests` branch and wait for origin/data's
   analysis/latest.json to have a generated_at at or after the request.
3. Run export_rows from origin/data (and the plan in config/, changes log applied) into
   derived/rows/, one file per dataset, and build Coach's snapshot document (--snapshot).

Prints one JSON line: {requested_at, finished_at, result, activity_id, status_detail, rows,
datasets, pending, snapshot}, where result is ok, no_new_activity, error or timed_out. Writing the
rows to the dashboard and the snapshot to Coach is done by Claude with the Artifact tools. Never
touches the plan, athlete.yaml or the calendar; on main it only ever commits the three logs.
"""

import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pipeline.dashboard import export_rows
from pipeline.plan.schema import PlanLoadError, default_changes_path, load_checked, report_load_error

UTC = dt.timezone.utc


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path | None = None, *, run=subprocess.run) -> str:
    r = run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip().splitlines()[-1:] or ''}")
    return r.stdout


def _stamp(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def push_request(activity: str, now: dt.datetime, *, run=subprocess.run) -> str:
    """Commit requests/<YYYYmmddTHHMMSSZ>.json on top of origin/requests in a scratch worktree and push it."""
    name = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    body = json.dumps({"activity": activity, "requested_at": _stamp(now), "by": "claude-chat"}) + "\n"
    _git(["fetch", "-q", "origin", "requests"], run=run)
    with tempfile.TemporaryDirectory(prefix="pedestal-request-") as tmp:
        wt = Path(tmp) / "wt"
        _git(["worktree", "add", "-q", "--detach", str(wt), "origin/requests"], run=run)
        try:
            (wt / "requests").mkdir(exist_ok=True)
            (wt / "requests" / f"{name}.json").write_text(body, encoding="utf-8")
            _git(["add", f"requests/{name}.json"], cwd=wt, run=run)
            _git(["commit", "-q", "-m", f"request: analyse {activity} ({name})"], cwd=wt, run=run)
            _git(["push", "-q", "origin", "HEAD:refs/heads/requests"], cwd=wt, run=run)
        finally:
            _git(["worktree", "remove", "--force", str(wt)], run=run)
    return name


def generated(latest: dict | None) -> dt.datetime | None:
    """analysis/latest.json's generated_at as an aware datetime (minute precision; naive = UTC)."""
    raw = (latest or {}).get("generated_at")
    if not raw:
        return None
    t = dt.datetime.fromisoformat(raw)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def read_latest(*, run=subprocess.run) -> dict | None:
    _git(["fetch", "-q", "origin", "data"], run=run)
    r = run(["git", "show", "origin/data:analysis/latest.json"], capture_output=True, text=True, encoding="utf-8")
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def wait_for_analysis(requested: dt.datetime, timeout_s: float, poll_s: float, *, run=subprocess.run,
                      sleep=time.sleep, clock=time.monotonic) -> dict | None:  # fmt: skip
    """The first latest.json generated at or after the request's minute (generated_at has no seconds), or None."""
    floor = requested.astimezone(UTC).replace(second=0, microsecond=0)
    deadline = clock() + timeout_s
    while True:
        latest = read_latest(run=run)
        t = generated(latest)
        if t and t >= floor:
            return latest
        if clock() >= deadline:
            return None
        sleep(min(poll_s, max(0.0, deadline - clock())))


def main(argv=None, *, run=subprocess.run, sleep=time.sleep, clock=time.monotonic, now=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.dashboard.refresh", description=__doc__.split("\n\n")[1])
    parser.add_argument("--activity", default="latest", help='"latest" or an intervals.icu activity id')
    parser.add_argument("--timeout", type=float, default=720, help="seconds to wait for the analysis (default 720 = 12 min)")
    parser.add_argument("--poll", type=float, default=20, help="seconds between checks of origin/data (default 20)")
    parser.add_argument("--no-request", action="store_true", help="skip the request and the wait: just export origin/data")
    parser.add_argument("--out", type=Path, default=Path("derived/rows"))
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"), help="for the week rows (default: config/plan.yaml)")
    parser.add_argument("--athlete", type=Path, help="default: athlete.yaml beside the plan")
    parser.add_argument("--pending", type=Path, help="Coach's pending items (a directory of <id>.json): applied, committed and pushed first")
    parser.add_argument("--pending-results", type=Path, default=Path("derived/coach_pending_results.json"))
    parser.add_argument("--no-push", action="store_true", help="commit the applied items but don't push main")
    parser.add_argument("--changes", type=Path, help="default: training/logs/changes.yaml beside config/ (PEDESTAL_CHANGES overrides)")
    parser.add_argument("--log", type=Path, default=Path("training/logs/strength.csv"))
    parser.add_argument("--body", type=Path, default=Path("training/logs/body.csv"))
    parser.add_argument("--snapshot", type=Path, default=Path("derived/coach_snapshot.json"), help="Coach's snapshot document")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)
    if not (args.activity == "latest" or args.activity.replace("_", "").replace("-", "").isalnum()) or len(args.activity) > 40:
        print(f"refresh: refusing activity {args.activity!r}: 'latest' or an activity id", file=sys.stderr)
        return 2
    now = now or (lambda: dt.datetime.now(UTC))
    out = {"requested_at": None, "finished_at": None, "result": None, "activity_id": None, "status_detail": "", "rows": None, "datasets": [],
           "pending": None, "snapshot": None}  # fmt: skip
    load = lambda: load_checked(args.plan, args.athlete, **({"changes_path": args.changes} if args.changes else {}))  # noqa: E731
    try:
        plan, athlete = load()
    except PlanLoadError as e:
        return report_load_error(e)

    if args.pending:  # 1. Coach's items into the logs, committed and pushed before anything reads main
        from pipeline.coach import pending

        items = pending.read_items(args.pending) if args.pending.exists() else []
        changes = args.changes or default_changes_path(args.plan) or Path("training/logs/changes.yaml")
        res = pending.apply(items, plan=plan, athlete=athlete, today=args.today, changes_path=changes, log_path=args.log, body_path=args.body)
        res["commit"] = None
        try:
            if res["files"]:
                res["commit"] = pending.commit(res["files"], pending.message(res, items), push=not args.no_push, run=run)
        except RuntimeError as e:
            res["error"] = str(e)
        args.pending_results.parent.mkdir(parents=True, exist_ok=True)
        args.pending_results.write_text(json.dumps(res) + "\n", encoding="utf-8")
        out["pending"] = {k: res.get(k) for k in ("applied", "rejected", "skipped", "commit", "error")} | {"results": str(args.pending_results)}
        if res.get("error"):
            out["result"], out["status_detail"], out["finished_at"] = "error", "pending items: " + res["error"], _stamp(now())
            print(json.dumps(out))
            return 1
        try:
            plan, athlete = load()  # with the new changes
        except PlanLoadError as e:
            return report_load_error(e)

    requested = now()
    try:
        if args.no_request:
            latest = read_latest(run=run)
            out["result"] = (latest or {}).get("status", "error") if latest else "error"
        else:
            push_request(args.activity, requested, run=run)
            out["requested_at"] = _stamp(requested)
            latest = wait_for_analysis(requested, args.timeout, args.poll, run=run, sleep=sleep, clock=clock)
            out["result"] = latest.get("status", "error") if latest else "timed_out"
        if latest:
            out["activity_id"] = latest.get("activity_id")
            out["status_detail"] = latest.get("error", "")
        s = export_rows.read_sources(ref="origin/data", git=run)
        rows = export_rows.build(s["dashboard.json"], s["today.json"], s["debrief.json"], s["analysis/latest.json"],
                                  request=s["analysis/request"], now=now(), plan=plan, athlete=athlete, tables=s["tables"])  # fmt: skip
        export_rows.write(rows, args.out)
        out["rows"], out["datasets"] = str(args.out), list(rows)
        from pipeline.coach import snapshot

        doc = snapshot.build(rows, ref="origin/data", now=now(), run=run)
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        out["snapshot"] = str(args.snapshot)
    except (GitError, FileNotFoundError, json.JSONDecodeError) as e:
        out["result"], out["status_detail"] = "error", str(e)
    out["finished_at"] = _stamp(now())
    print(json.dumps(out))
    return 0 if out["result"] in ("ok", "no_new_activity") else 1


if __name__ == "__main__":
    sys.exit(main())
