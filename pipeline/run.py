"""One entry point for the scheduled runs.

    python -m pipeline.run daily  [--day DATE] [--wait-for-sleep [--final]] [--dry-run] [--no-alert]
    python -m pipeline.run weekly [--day DATE]

daily:  ingest (last 14 days) -> readiness -> prescribe (7 days) -> publish (7 days)
        -> review yesterday -> (Sundays: weekly) -> brief (derived/today.md, today.json)
weekly: the drift review for the week ending on the latest Sunday (derived/week.md)

`--wait-for-sleep` (the hourly workflow): first ask intervals.icu, read-only, whether today's
HRV and sleep have arrived. If not, stop without writing anything; the next hour tries again.
`--final` runs regardless (the end of the morning window).

Each step prints one status line; its full output goes to derived/logs/<day>-<mode>.log.
A failed step doesn't stop the others, with one exception: publish is skipped when
prescribe failed, so the calendar keeps its last good weights. The run exits 1 if
anything failed. Secret values are scrubbed from everything printed or logged.

When a step fails and NTFY_TOPIC is set, one ntfy line names the failed steps (pipeline.notify);
nothing else ever goes to ntfy. derived/state/morning.json records the day as done once a
run has no failures (or was the final one), so the later hours skip (pipeline.gate); a run with
failures retries the next hour, alerting only about steps it hasn't alerted about today.
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
from pipeline.brief import short_readiness
from pipeline.ingest.__main__ import main as ingest_main
from pipeline.ingest.intervals import IntervalsClient, IntervalsError
from pipeline.notify import NotifyError
from pipeline.plan.schema import PlanLoadError, load_checked
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


def sleep_data_in(day: dt.date, env, transport=None, sleep=time.sleep) -> bool | None:
    """Has intervals.icu got today's HRV and sleep? None when it can't be asked (no key set)."""
    try:
        key, athlete_id = require_env("INTERVALS_ICU_API_KEY", env), require_env("INTERVALS_ICU_ATHLETE_ID", env)
    except MissingSetting:
        return None
    try:
        with IntervalsClient(key, athlete_id, transport=transport, sleep=sleep) as client:
            rows = client.wellness(day, day)
    except IntervalsError:
        return False  # can't tell: wait for the next hour (the final run goes regardless)
    row = next((r for r in rows if str(r.get("id", ""))[:10] == day.isoformat()), None)
    return bool(row and row.get("hrv") is not None and row.get("sleepSecs"))


def _last_line(text: str, prefix: str = "") -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and ln.strip().startswith(prefix)]
    return lines[-1] if lines else ""


def read_state(derived: Path) -> dict:
    try:
        return json.loads((derived / "state" / "morning.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Runner:
    def __init__(self, day: dt.date, *, env=None, transport=None, ntfy_transport=None, sleep=time.sleep,
                 push: bool = True, send: bool = True, derived: Path = Path("derived"), missing_note: str = ""):  # fmt: skip
        if env is None:
            load_env()
            env = os.environ
        self.day, self.env, self.transport, self.ntfy_transport = day, env, transport, ntfy_transport
        self.sleep, self.push, self.send, self.derived = sleep, push, send, derived
        self.missing_note = missing_note  # readiness reason when sleep data never arrived
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
            print(f"no call for {self.day} yet (no HRV synced)" if row else f"no wellness row for {self.day} yet")
        else:
            print(f"{row['readiness']}: {short_readiness(row['readiness_reason'])}")
        return 0

    def brief(self) -> int:
        plan, athlete = load_checked(Path("config/plan.yaml"))  # a PlanLoadError fails the step
        b = brief_mod.build(plan, athlete, self.day, self.derived, self.names("failed"), self.names("skipped"), self.missing_note)
        md, js = brief_mod.write(b, self.derived)
        print(f"wrote {md.name} and {js.name}: {len(b['sessions'])} sessions, readiness {b['readiness']['colour'] or 'no call'}")
        return 0

    # ------------------------------------------------------------------ wrap-up

    def alert(self, state: dict) -> list[str]:
        """One ntfy line for steps that failed and haven't been alerted about today. Returns the steps alerted."""
        failed = self.names("failed")
        already = state.get("alerted", []) if state.get("alerted_on") == self.day.isoformat() else []
        new = [n for n in failed if n not in already]
        if not new:
            return []
        if not self.send:
            self.record(StepResult("alert", "skipped", "not sent (--no-alert): " + notify_mod.alert_text(self.day, new)))
            return []
        try:
            sent = notify_mod.alert(self.day, new, env=self.env, transport=self.ntfy_transport, sleep=self.sleep)
        except NotifyError as e:
            self.record(StepResult("alert", "failed", str(e)))
            return []
        if sent is None:
            self.record(StepResult("alert", "skipped", "alerts off (NTFY_TOPIC not set)"))
            return []
        self.record(StepResult("alert", "ok", f"sent: {', '.join(new)} failed"))
        return new

    def finish(self, mode: str) -> int:
        ok = sum(r.state == "ok" for r in self.results)
        failed = [r.name for r in self.results if r.state == "failed"]
        skipped = [r.name for r in self.results if r.state == "skipped"]
        summary = f"{mode} {self.day}: {ok} ok" + (f", {len(failed)} failed ({', '.join(failed)})" if failed else "") + (
            f", {len(skipped)} skipped ({', '.join(skipped)})" if skipped else ""
        )
        print(summary)
        logs = self.derived / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / f"{self.day}-{mode}.log", "w", encoding="utf-8") as f:
            f.write(f"# {summary}\n")
            for r in self.results:
                f.write(f"\n## {r.line()}\n{r.output}")
        return 1 if failed else 0

    def save_state(self, state: dict, alerted: list[str], with_sleep: bool, final: bool) -> None:
        """Mark the day done (no failures, or the final run), and remember today's alerts."""
        today = self.day.isoformat()
        if state.get("alerted_on") != today:
            state = {k: v for k, v in state.items() if k not in ("alerted", "alerted_on")}
        if alerted:
            state["alerted_on"] = today
            state["alerted"] = sorted(set(state.get("alerted", [])) | set(alerted))
        if final or not self.names("failed"):
            state.update(last_done=today, done_at=dt.datetime.now().strftime("%H:%M"), with_sleep=with_sleep,
                         failed=self.names("failed"))  # fmt: skip
        path = self.derived / "state" / "morning.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None, *, env=None, transport=None, ntfy_transport=None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.run", description="Run the pipeline.")
    parser.add_argument("mode", choices=("daily", "weekly"))
    parser.add_argument("--day", type=dt.date.fromisoformat, default=dt.date.today(), help="default: today (local, or TZ)")
    parser.add_argument("--dry-run", action="store_true", help="don't write to the intervals.icu calendar")
    parser.add_argument("--no-alert", action="store_true", help="don't send failure alerts (print them)")
    parser.add_argument("--derived", type=Path, default=Path("derived"))
    parser.add_argument("--wait-for-sleep", action="store_true", help="stop unless today's HRV and sleep are in")
    parser.add_argument("--final", action="store_true", help="with --wait-for-sleep: run even without them")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if env is None:
        load_env()
        env = os.environ

    with_sleep = True
    if args.mode == "daily" and args.wait_for_sleep:
        with_sleep = sleep_data_in(args.day, env, transport, sleep) is not False
        if not with_sleep and not args.final:
            print(f"waiting: today's ({args.day}) HRV and sleep aren't in intervals.icu yet; the next hour tries again")
            return 0
    missing = "" if with_sleep else f"no HRV/sleep synced by {dt.datetime.now():%H:%M}"

    runner = Runner(args.day, env=env, transport=transport, ntfy_transport=ntfy_transport, sleep=sleep,
                    push=not args.dry_run, send=not args.no_alert, derived=args.derived, missing_note=missing)  # fmt: skip
    print(f"Pedestal {args.mode} run for {args.day}" + ("" if with_sleep else " (sleep data not in: fallback run)"))
    getattr(runner, args.mode)()
    state = read_state(args.derived)
    alerted = runner.alert(state)
    code = runner.finish(args.mode)
    if args.mode == "daily" and runner.send:
        runner.save_state(state, alerted, with_sleep, final=args.final or not args.wait_for_sleep)
    return code


if __name__ == "__main__":
    sys.exit(main())
