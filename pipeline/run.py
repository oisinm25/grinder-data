"""One entry point for the scheduled runs.

    python -m pipeline.run daily  [--day DATE] [--ignore-window] [--dry-run] [--no-alert]
    python -m pipeline.run weekly [--day DATE]

daily:  ingest (last 14 days) -> readiness -> prescribe (7 days) -> publish (7 days)
        -> review yesterday -> (Sundays: weekly) -> debrief yesterday (derived/debrief.md, .json)
        -> brief (derived/today.md, today.json, with the debrief in it)
weekly: the drift review for the week ending on the latest Sunday (derived/week.md)

The workflow fires a few times each morning; `daily` decides for itself whether to go:
- it exits cleanly outside the window in `athlete.yaml` `schedule` (local time in its
  timezone, end inclusive); `--ignore-window` (a manual workflow_dispatch run) skips this;
- it exits cleanly once derived/today.json for today says "status": "final";
- otherwise it runs. After ingest the day is "final" if today's wellness row has sleep, and
  "provisional" ("no sleep yet") if not; readiness uses whatever HRV and resting HR exist. A
  provisional day runs again at the next slot in the window.

Each step prints one status line; its full output goes to derived/logs/<day>-<mode>.log.
A failed step doesn't stop the others, with one exception: publish is skipped when
prescribe failed, so the calendar keeps its last good weights; publish only sends events
whose content changed. The run exits 1 if anything failed. Secret values are scrubbed from
everything printed or logged.

When a step fails and NTFY_TOPIC is set, one ntfy line names the failed steps (pipeline.notify);
nothing else ever goes to ntfy. Each failed step alerts at most once a day (today.json
keeps the list in "alerts_sent").
"""

import argparse
import contextlib
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline import brief as brief_mod
from pipeline import notify as notify_mod
from pipeline.athlete import load_athlete
from pipeline.brief import day_status, short_readiness
from pipeline.ingest.__main__ import main as ingest_main
from pipeline.ingest.intervals import IntervalsClient
from pipeline.notify import NotifyError
from pipeline.review import debrief as debrief_mod
from pipeline.plan.schema import load_checked
from pipeline.prescribe import main as prescribe_main
from pipeline.publish.__main__ import main as publish_main
from pipeline.review.__main__ import last_sunday
from pipeline.review.__main__ import main as review_main
from pipeline.settings import MissingSetting, load_env, require_env

SECRETS = ("INTERVALS_ICU_API_KEY", "NTFY_TOPIC")
INGEST_DAYS = 14
AHEAD_DAYS = 7


@dataclass
class StepResult:
    name: str
    state: str  # ok | failed | skipped
    status: str
    output: str = ""

    def line(self) -> str:
        return f"[{self.state:<7}] {self.name:<9} {self.status}"


def _last_line(text: str, prefix: str = "") -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and ln.strip().startswith(prefix)]
    return lines[-1] if lines else ""


def read_today(derived: Path) -> dict:
    try:
        return json.loads((derived / "today.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Runner:
    def __init__(self, day: dt.date, *, env=None, transport=None, ntfy_transport=None, sleep=time.sleep,
                 push: bool = True, send: bool = True, derived: Path = Path("derived"), alerts_sent=()):  # fmt: skip
        if env is None:
            load_env()
            env = os.environ
        self.day, self.env, self.transport, self.ntfy_transport = day, env, transport, ntfy_transport
        self.sleep, self.push, self.send, self.derived = sleep, push, send, derived
        self.alerts_sent = list(alerts_sent)  # steps already alerted about today
        self.status, self.status_reason = "provisional", "no sleep yet"
        self.results: list[StepResult] = []

    def scrub(self, text: str) -> str:
        for name in SECRETS:
            value = (self.env.get(name) or "").strip()
            if len(value) >= 4:
                text = text.replace(value, "***")
        return text

    def step(self, name: str, fn: Callable[[], int], status: Callable[[str, int], str]) -> StepResult:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = fn()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        except Exception as e:  # noqa: BLE001 - one step's crash mustn't stop the run
            buf.write(traceback.format_exc())
            code = 1
            buf.write(f"\n{type(e).__name__}: {e}\n")
        out = self.scrub(buf.getvalue())
        if code == 0:
            result = StepResult(name, "ok", self.scrub(status(out, code)) or "done", out)
        else:
            why = _last_line(out, "error:") or _last_line(out) or f"exit {code}"
            result = StepResult(name, "failed", why.removeprefix("error: "), out)
        self.record(result)
        return result

    def skip(self, name: str, why: str) -> StepResult:
        result = StepResult(name, "skipped", why)
        self.record(result)
        return result

    def record(self, result: StepResult) -> None:
        self.results.append(result)
        print(result.line(), flush=True)

    def names(self, state: str) -> list[str]:
        return [r.name for r in self.results if r.state == state and r.name != "alert"]

    # ------------------------------------------------------------------ modes

    def kw(self) -> dict:
        return {"env": self.env, "transport": self.transport, "sleep": self.sleep}

    def daily(self) -> None:
        day, d = self.day, self.derived
        ingest_from = day - dt.timedelta(days=INGEST_DAYS)
        ahead = ["--from", day.isoformat(), "--days", str(AHEAD_DAYS)]

        def ingest_status(out: str, _) -> str:
            acts = re.search(r"^Activities: (\d+) \(([^)]*)\)", out, re.M)
            days = re.search(r"^Wellness: (\d+) days", out, re.M)
            sets = re.search(r"^Strength: (\d+) sets", out, re.M)
            bits = [f"{acts[1]} activities ({acts[2]})" if acts else "", f"{days[1]} wellness days" if days else "",
                    f"{sets[1]} strength sets" if sets else ""]  # fmt: skip
            review = out.count("  review: ")
            return ", ".join(b for b in bits if b) + (f"; {review} to review" if review else "")

        self.step("ingest", lambda: ingest_main(["--from", ingest_from.isoformat(), "--to", day.isoformat(), "--out", str(d)], **self.kw()),
                  ingest_status)  # fmt: skip
        self.status, self.status_reason = day_status(d, day)
        self.step("readiness", self.readiness, lambda out, _: out.strip())
        rx = self.step("prescribe", lambda: prescribe_main(ahead + ["--strength", str(d / "strength.csv"), "--wellness",
                                                                    str(d / "wellness.csv"), "--out", str(d / "prescriptions.csv")]),
                       lambda out, _: _last_line(out, "wrote").split(" to ")[0] or _last_line(out))  # fmt: skip
        if rx.state != "ok":
            self.skip("publish", "prescribe failed, so the calendar keeps its last good version")
        else:
            args = ahead + ["--strength", str(d / "strength.csv"), "--wellness", str(d / "wellness.csv")] + (["--push"] if self.push else [])
            self.step("publish", lambda: publish_main(args, **self.kw()),
                      lambda out, _: _last_line(out, "intervals.icu:") or _last_line(out, "Dry run"))  # fmt: skip
        yesterday = day - dt.timedelta(days=1)
        self.step("review", lambda: review_main(["--day", yesterday.isoformat(), "--derived", str(d)], today=day, **self.kw()),
                  lambda out, _: _last_line(out, "Day ") or _last_line(out, "Week ") or _last_line(out))  # fmt: skip
        if day.weekday() == 6:
            self.weekly()
        self.step("debrief", self.debrief, lambda out, _: _last_line(out))
        self.step("brief", self.brief, lambda out, _: _last_line(out))

    def weekly(self) -> None:
        sunday = last_sunday(self.day)

        def status(out: str, _) -> str:
            flags = [ln.strip()[2:] for ln in out.splitlines() if ln.startswith("  - ")]
            return f"{_last_line(out, 'Week ')}; flags: " + ("; ".join(flags) if flags else "none")

        argv = ["--week-ending", sunday.isoformat(), "--derived", str(self.derived)]
        self.step("weekly", lambda: review_main(argv, today=self.day, **self.kw()), status)

    # ------------------------------------------------------------------ in-process steps

    def readiness(self) -> int:
        path = self.derived / "wellness.csv"
        if not path.exists():
            print(f"error: {path} not found")
            return 1
        with open(path, newline="", encoding="utf-8") as f:
            row = next((r for r in csv.DictReader(f) if r["date"] == self.day.isoformat()), None)
        if row is None or not row.get("readiness"):
            print(f"no call for {self.day} yet" + (" (no HRV or resting HR synced)" if row else " (no wellness row)") + f"; {self.status}")
        else:
            print(f"{row['readiness']}: {short_readiness(row['readiness_reason'])}; {self.status}"
                  + (f" ({self.status_reason})" if self.status_reason else ""))  # fmt: skip
        return 0

    def debrief(self) -> int:
        plan, athlete = load_checked(Path("config/plan.yaml"))  # a PlanLoadError fails the step
        try:
            client = IntervalsClient(require_env("INTERVALS_ICU_API_KEY", self.env), require_env("INTERVALS_ICU_ATHLETE_ID", self.env),
                                     cache_dir=Path("cache"), transport=self.transport, sleep=self.sleep)  # fmt: skip
        except MissingSetting:
            client = None  # the debrief still runs on what's in derived/
        try:
            b = debrief_mod.build(self.day - dt.timedelta(days=1), plan, athlete, self.derived, debrief_mod.Fetch(client))
        finally:
            if client is not None:
                client.close()
        debrief_mod.write(b, self.derived)
        print(f"wrote debrief.md and debrief.json: {b['verdict']}")
        return 0

    def brief(self) -> int:
        plan, athlete = load_checked(Path("config/plan.yaml"))  # a PlanLoadError fails the step
        b = brief_mod.build(plan, athlete, self.day, self.derived, self.names("failed"), self.names("skipped"),
                            status=self.status, status_reason=self.status_reason, alerts_sent=self.alerts_sent)  # fmt: skip
        md, js = brief_mod.write(b, self.derived)
        print(f"wrote {md.name} and {js.name}: {b['status']}, {len(b['sessions'])} sessions, readiness {b['readiness']['colour'] or 'no call'}")
        return 0

    # ------------------------------------------------------------------ wrap-up

    def alert(self) -> None:
        """One ntfy line for steps that failed and haven't been alerted about today."""
        new = [n for n in self.names("failed") if n not in self.alerts_sent]
        if not new:
            return
        if not self.send:
            self.record(StepResult("alert", "skipped", "not sent (--no-alert): " + notify_mod.alert_text(self.day, new)))
            return
        try:
            sent = notify_mod.alert(self.day, new, env=self.env, transport=self.ntfy_transport, sleep=self.sleep)
        except NotifyError as e:
            self.record(StepResult("alert", "failed", str(e)))
            return
        if sent is None:
            self.record(StepResult("alert", "skipped", "alerts off (NTFY_TOPIC not set)"))
            return
        self.alerts_sent += new
        self.record(StepResult("alert", "ok", f"sent: {', '.join(new)} failed"))
        js = self.derived / "today.json"  # remember them, so the next slot doesn't alert again
        b = read_today(self.derived)
        if b.get("date") == self.day.isoformat():
            b["alerts_sent"] = self.alerts_sent
            js.write_text(json.dumps(b, indent=1) + "\n", encoding="utf-8")

    def finish(self, mode: str) -> int:
        ok = sum(r.state == "ok" for r in self.results)
        failed = [r.name for r in self.results if r.state == "failed"]
        skipped = [r.name for r in self.results if r.state == "skipped"]
        summary = f"{mode} {self.day}: {ok} ok" + (f", {len(failed)} failed ({', '.join(failed)})" if failed else "") + (
            f", {len(skipped)} skipped ({', '.join(skipped)})" if skipped else ""
        )
        if mode == "daily":
            summary += f"; {self.status}" + (f" ({self.status_reason})" if self.status_reason else "")
        print(summary)
        logs = self.derived / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / f"{self.day}-{mode}.log", "w", encoding="utf-8") as f:
            f.write(f"# {summary}\n")
            for r in self.results:
                f.write(f"\n## {r.line()}\n{r.output}")
        return 1 if failed else 0


def main(argv=None, *, env=None, transport=None, ntfy_transport=None, sleep=time.sleep, now: dt.datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.run", description="Run the pipeline.")
    parser.add_argument("mode", choices=("daily", "weekly"))
    parser.add_argument("--day", type=dt.date.fromisoformat, help="default: today in the schedule's timezone")
    parser.add_argument("--ignore-window", action="store_true", help="run now, whatever the time (manual runs)")
    parser.add_argument("--dry-run", action="store_true", help="don't write to the intervals.icu calendar")
    parser.add_argument("--no-alert", action="store_true", help="don't send failure alerts (print them)")
    parser.add_argument("--derived", type=Path, default=Path("derived"))
    parser.add_argument("--athlete", type=Path, default=Path("config/athlete.yaml"))
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if env is None:
        load_env()
        env = os.environ

    try:
        schedule = load_athlete(args.athlete).schedule
    except Exception as e:  # noqa: BLE001 - an unreadable athlete.yaml is reported, not raised
        print(f"error: {args.athlete}: {e}".splitlines()[0])
        return 1
    local = now.astimezone(schedule.zone()) if now else dt.datetime.now(schedule.zone())
    day = args.day or local.date()

    if args.mode == "daily":
        start, end = schedule.window
        if not args.ignore_window and not schedule.contains(local):
            print(f"outside the morning window: {local:%H:%M} {schedule.timezone}, window {start}-{end}; nothing to do")
            return 0
        previous = read_today(args.derived)
        if previous.get("date") == day.isoformat() and previous.get("status") == "final":
            print(f"today's brief is already final (generated {previous.get('generated_at', '?')}); nothing to do")
            return 0
        alerts_sent = previous.get("alerts_sent", []) if previous.get("date") == day.isoformat() else []
    else:
        alerts_sent = []

    runner = Runner(day, env=env, transport=transport, ntfy_transport=ntfy_transport, sleep=sleep, push=not args.dry_run,
                    send=not args.no_alert, derived=args.derived, alerts_sent=alerts_sent)  # fmt: skip
    print(f"Pedestal {args.mode} run for {day} ({local:%H:%M} {schedule.timezone})")
    getattr(runner, args.mode)()
    runner.alert()
    return runner.finish(args.mode)


if __name__ == "__main__":
    sys.exit(main())
