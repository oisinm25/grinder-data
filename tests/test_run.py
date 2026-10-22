import csv
import datetime as dt
import json
import shutil

import httpx
import pytest

from pipeline import run as run_mod
from pipeline.run import main
from tests.conftest import ROOT

MON = dt.date(2026, 10, 26)
KEY = "fake-key-" + "0123456789abcdef"
TOPIC = "pedestal-" + "secret-topic-1234567890"
ENV = {"INTERVALS_ICU_API_KEY": KEY, "INTERVALS_ICU_ATHLETE_ID": "i99999", "NTFY_TOPIC": TOPIC}
FIXTURES = ROOT / "tests" / "fixtures" / "intervals"
ORDER = ["ingest", "readiness", "prescribe", "publish", "review", "brief"]


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """A copy of the repo's config and strength log; the run writes derived/ and exports/ here."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "training", tmp_path / "training")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class Calls:
    def __init__(self):
        self.made = []

    def fake(self, name, code=0, out="", boom=None):
        def step(argv=None, **kw):
            self.made.append((name, argv, kw))
            print(out or f"{name} output")
            if boom:
                raise boom
            return code

        return step


@pytest.fixture
def steps(monkeypatch):
    calls = Calls()
    for name in ("ingest", "prescribe", "publish", "review"):
        monkeypatch.setattr(run_mod, f"{name}_main", calls.fake(name))
    monkeypatch.setattr(run_mod.Runner, "readiness", lambda self: print("green: all fine") or 0)

    def brief(self):
        calls.made.append(("brief", self.names("failed"), {"note": self.missing_note}))
        print("wrote today.md and today.json")
        return 0

    monkeypatch.setattr(run_mod.Runner, "brief", brief)
    return calls


class FakeNtfy:
    def __init__(self, status=200):
        self.status, self.bodies = status, []

    def __call__(self, request):
        self.bodies.append(request.content.decode())
        return httpx.Response(self.status)


def run(*argv, ntfy=None, env=ENV, transport=None):
    return main(["daily", "--day", "2026-10-26", *argv], env=env, transport=transport,
                ntfy_transport=httpx.MockTransport(ntfy or FakeNtfy()), sleep=lambda s: None)  # fmt: skip


def state(workdir):
    return json.loads((workdir / "derived" / "state" / "morning.json").read_text())


def lines(out):
    return [ln.split("]")[1].split()[0] for ln in out.splitlines() if ln.startswith("[")]


# ------------------------------------------------------------------ the steps


def test_all_steps_in_order_and_no_ntfy_when_all_is_well(workdir, steps, capsys):
    ntfy = FakeNtfy()
    assert run(ntfy=ntfy) == 0
    out = capsys.readouterr().out
    assert [c[0] for c in steps.made] == ["ingest", "prescribe", "publish", "review", "brief"]
    assert lines(out) == ORDER
    assert out.rstrip().endswith("daily 2026-10-26: 6 ok")
    assert ntfy.bodies == []  # ntfy is for failures only


def test_step_arguments(workdir, steps):
    run()
    args = {name: argv for name, argv, _ in steps.made if isinstance(argv, list) and argv and argv[0].startswith("--")}
    assert args["ingest"][:4] == ["--from", "2026-10-12", "--to", "2026-10-26"]  # last 14 days
    assert args["prescribe"][:4] == ["--from", "2026-10-26", "--days", "7"]
    assert args["publish"][:4] == ["--from", "2026-10-26", "--days", "7"] and "--push" in args["publish"]
    assert args["review"][:2] == ["--day", "2026-10-25"]  # yesterday


def test_dry_run_does_not_push(workdir, steps):
    run("--dry-run")
    publish = next(argv for name, argv, _ in steps.made if name == "publish")
    assert "--push" not in publish


def test_a_failed_step_does_not_stop_the_others(workdir, steps, monkeypatch, capsys):
    monkeypatch.setattr(run_mod, "ingest_main", steps.fake("ingest", code=1, out="error: intervals.icu returned 503 for GET /x"))
    ntfy = FakeNtfy()
    assert run(ntfy=ntfy) == 1
    out = capsys.readouterr().out
    assert "[failed ] ingest    intervals.icu returned 503 for GET /x" in out
    assert [c[0] for c in steps.made] == ["ingest", "prescribe", "publish", "review", "brief"]
    assert ("brief", ["ingest"], {"note": ""}) in steps.made  # the brief says what failed
    assert ntfy.bodies == ["Pedestal Mon 26 Oct: ingest failed."]
    assert "[ok     ] alert     sent: ingest failed" in out
    assert out.rstrip().endswith("daily 2026-10-26: 6 ok, 1 failed (ingest)")


def test_alert_never_carries_error_detail_or_training_data(workdir, steps, monkeypatch):
    monkeypatch.setattr(run_mod, "prescribe_main", steps.fake("prescribe", code=1, out="error: line 3, kg: 105 x 6 @RPE 8 bench"))
    ntfy = FakeNtfy()
    run(ntfy=ntfy)
    assert ntfy.bodies == ["Pedestal Mon 26 Oct: prescribe failed."]


def test_alert_links_to_the_actions_run(workdir, steps, monkeypatch):
    monkeypatch.setattr(run_mod, "review_main", steps.fake("review", code=1))
    ntfy = FakeNtfy()
    env = ENV | {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7"}
    run(ntfy=ntfy, env=env)
    assert ntfy.bodies == ["Pedestal Mon 26 Oct: review failed. https://github.com/o/r/actions/runs/7"]


def test_publish_is_skipped_when_prescribe_fails(workdir, steps, monkeypatch, capsys):
    monkeypatch.setattr(run_mod, "prescribe_main", steps.fake("prescribe", code=2, out="error: derived/strength.csv not found"))
    assert run() == 1
    out = capsys.readouterr().out
    assert "publish" not in [c[0] for c in steps.made]
    assert "[skipped] publish   prescribe failed, so the calendar keeps its last good version" in out
    assert "1 failed (prescribe), 1 skipped (publish)" in out


def test_a_crash_is_caught(workdir, steps, monkeypatch, capsys):
    monkeypatch.setattr(run_mod, "review_main", steps.fake("review", boom=KeyError("date")))
    assert run() == 1
    assert "[failed ] review    KeyError: 'date'" in capsys.readouterr().out
    assert "brief" in [c[0] for c in steps.made]


def test_no_topic_means_alerts_off(workdir, steps, monkeypatch, capsys):
    monkeypatch.setattr(run_mod, "publish_main", steps.fake("publish", code=1))
    env = {k: v for k, v in ENV.items() if k != "NTFY_TOPIC"}
    assert run(env=env) == 1
    assert "[skipped] alert     alerts off (NTFY_TOPIC not set)" in capsys.readouterr().out


def test_failed_alert_is_reported(workdir, steps, monkeypatch, capsys):
    monkeypatch.setattr(run_mod, "publish_main", steps.fake("publish", code=1))
    run(ntfy=FakeNtfy(status=403))
    assert "[failed ] alert     ntfy refused the alert (403)" in capsys.readouterr().out


def test_secrets_are_scrubbed_from_output_and_log(workdir, steps, monkeypatch, capsys):
    monkeypatch.setattr(run_mod, "ingest_main", steps.fake("ingest", out=f"oops key={KEY} topic={TOPIC}"))
    run()
    out = capsys.readouterr().out
    log = (workdir / "derived" / "logs" / "2026-10-26-daily.log").read_text(encoding="utf-8")
    assert KEY not in out + log and TOPIC not in out + log
    assert "key=*** topic=***" in log


def test_log_has_every_step(workdir, steps):
    run()
    log = (workdir / "derived" / "logs" / "2026-10-26-daily.log").read_text(encoding="utf-8")
    assert log.startswith("# daily 2026-10-26: 6 ok")
    assert "## [ok     ] prescribe" in log and "prescribe output" in log


def test_sunday_daily_includes_the_weekly_review(workdir, steps, capsys):
    assert main(["daily", "--day", "2026-11-01"], env=ENV) == 0
    reviews = [argv for name, argv, _ in steps.made if name == "review"]
    assert reviews == [["--day", "2026-10-31", "--derived", "derived"], ["--week-ending", "2026-11-01", "--derived", "derived"]]
    assert "[ok     ] weekly" in capsys.readouterr().out


def test_weekly_reviews_the_latest_sunday(workdir, steps):
    assert main(["weekly", "--day", "2026-11-01"], env=ENV) == 0
    assert main(["weekly", "--day", "2026-11-03"], env=ENV) == 0
    reviews = [argv for name, argv, _ in steps.made if name == "review"]
    assert reviews == [["--week-ending", "2026-11-01", "--derived", "derived"]] * 2


# ------------------------------------------------------------------ waiting for sleep data, and the day's state


class Wellness:
    """A fake intervals.icu that only answers the wellness check."""

    def __init__(self, row):
        self.row = row

    def __call__(self, request):
        assert request.method == "GET" and request.url.path.endswith("/wellness")
        return httpx.Response(200, json=[self.row] if self.row else [])


IN = {"id": "2026-10-26", "hrv": 44.0, "sleepSecs": 26000}


def waiting_run(row, *extra, ntfy=None):
    return run("--wait-for-sleep", *extra, ntfy=ntfy, transport=httpx.MockTransport(Wellness(row)))


@pytest.mark.parametrize("row", [None, {"id": "2026-10-26", "hrv": None, "sleepSecs": 26000}, {"id": "2026-10-26", "hrv": 44.0}])
def test_waits_without_writing_anything(workdir, steps, capsys, row):
    assert waiting_run(row) == 0
    assert "waiting: today's (2026-10-26) HRV and sleep aren't in" in capsys.readouterr().out
    assert steps.made == [] and not (workdir / "derived").exists()


def test_a_clean_run_marks_the_day_done(workdir, steps):
    assert waiting_run(IN) == 0
    s = state(workdir)
    assert (s["last_done"], s["with_sleep"], s["failed"]) == ("2026-10-26", True, [])


def test_final_runs_without_sleep_and_says_so(workdir, steps, capsys):
    waiting_run(None, "--final")
    assert "(sleep data not in: fallback run)" in capsys.readouterr().out
    note = next(kw["note"] for name, _, kw in steps.made if name == "brief")
    assert note.startswith("no HRV/sleep synced by ")
    assert state(workdir)["with_sleep"] is False and state(workdir)["last_done"] == "2026-10-26"


def test_a_failed_run_retries_and_alerts_once(workdir, steps, monkeypatch):
    monkeypatch.setattr(run_mod, "publish_main", steps.fake("publish", code=1))
    ntfy = FakeNtfy()
    assert waiting_run(IN, ntfy=ntfy) == 1
    s = state(workdir)
    assert "last_done" not in s  # not done: the next hour runs again
    assert (s["alerted_on"], s["alerted"]) == ("2026-10-26", ["publish"])
    waiting_run(IN, ntfy=ntfy)  # next hour, still failing: no second alert
    monkeypatch.setattr(run_mod, "review_main", steps.fake("review", code=1))
    waiting_run(IN, ntfy=ntfy)  # a new failure gets its own alert
    assert ntfy.bodies == ["Pedestal Mon 26 Oct: publish failed.", "Pedestal Mon 26 Oct: review failed."]


def test_the_final_run_marks_done_even_with_failures(workdir, steps, monkeypatch):
    monkeypatch.setattr(run_mod, "publish_main", steps.fake("publish", code=1))
    waiting_run(IN, "--final")
    s = state(workdir)
    assert (s["last_done"], s["failed"]) == ("2026-10-26", ["publish"])


def test_yesterdays_alerts_dont_silence_today(workdir, steps, monkeypatch):
    (workdir / "derived" / "state").mkdir(parents=True)
    (workdir / "derived" / "state" / "morning.json").write_text(json.dumps({"alerted_on": "2026-10-25", "alerted": ["publish"]}))
    monkeypatch.setattr(run_mod, "publish_main", steps.fake("publish", code=1))
    ntfy = FakeNtfy()
    waiting_run(IN, ntfy=ntfy)
    assert ntfy.bodies == ["Pedestal Mon 26 Oct: publish failed."]


def test_no_alert_flag_prints_instead_and_leaves_state_alone(workdir, steps, monkeypatch, capsys):
    monkeypatch.setattr(run_mod, "publish_main", steps.fake("publish", code=1))
    ntfy = FakeNtfy()
    run("--no-alert", ntfy=ntfy)
    assert ntfy.bodies == []
    assert "[skipped] alert     not sent (--no-alert): Pedestal Mon 26 Oct: publish failed." in capsys.readouterr().out
    assert not (workdir / "derived" / "state" / "morning.json").exists()


def test_cannot_ask_without_a_key_so_it_runs_and_reports(workdir, steps):
    run("--wait-for-sleep", env={"NTFY_TOPIC": TOPIC})  # no intervals.icu key: the run's own steps report it
    assert [c[0] for c in steps.made][0] == "ingest"


# ------------------------------------------------------------------ end to end, real steps, fake services


class FakeServices:
    def __init__(self):
        self.calls = []

    def intervals(self, request: httpx.Request):
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/activities"):
            return httpx.Response(200, json=json.loads((FIXTURES / "activities.json").read_text()))
        if path.endswith("/wellness"):
            return httpx.Response(200, json=json.loads((FIXTURES / "wellness.json").read_text()))
        if path.endswith("/intervals"):
            return httpx.Response(200, json={"icu_intervals": []})
        if request.method == "GET":  # events
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=json.loads(request.content or b"[]"))

    def ntfy(self, request: httpx.Request):
        self.calls.append(("NTFY", request.content.decode()))
        return httpx.Response(200)


def test_daily_end_to_end(workdir, capsys):
    svc = FakeServices()
    code = main(["daily", "--day", "2026-10-26"], env=ENV, transport=httpx.MockTransport(svc.intervals),
                ntfy_transport=httpx.MockTransport(svc.ntfy), sleep=lambda s: None)  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.rstrip().endswith("daily 2026-10-26: 6 ok")
    assert ("POST", "/api/v1/athlete/i99999/events/bulk") in svc.calls  # published the week
    assert not [c for c in svc.calls if c[0] == "NTFY"]  # nothing failed, so nothing to ntfy
    d = workdir / "derived"
    md = (d / "today.md").read_text(encoding="utf-8")
    assert md.startswith("# Pedestal brief: Mon 26 Oct 2026") and "| Bench press | top | 1 × 6 |" in md
    brief = json.loads((d / "today.json").read_text(encoding="utf-8"))
    assert brief["date"] == "2026-10-26" and [s["session"] for s in brief["sessions"]] == ["upper_me", "z2_90"]
    for name in ("activities", "wellness", "strength", "prescriptions", "compliance"):
        assert (d / f"{name}.csv").exists()
    with open(d / "prescriptions.csv", newline="", encoding="utf-8") as f:
        assert {r["date"] for r in csv.DictReader(f)} <= {f"2026-10-{n}" for n in range(26, 32)} | {"2026-11-01"}
    assert KEY not in out + md and TOPIC not in out + md


def test_sunday_end_to_end_writes_week_md(workdir):
    svc = FakeServices()
    main(["daily", "--day", "2026-11-01"], env=ENV, transport=httpx.MockTransport(svc.intervals),
         ntfy_transport=httpx.MockTransport(svc.ntfy), sleep=lambda s: None)  # fmt: skip
    week = (workdir / "derived" / "week.md").read_text(encoding="utf-8")
    assert week.startswith("# Week 26 Oct to 1 Nov 2026") and "## Flags" in week and "## Recovery" in week


def test_daily_without_secrets_reports_each_failure(workdir, capsys):
    code = main(["daily", "--day", "2026-10-26"], env={}, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert code == 1
    assert "[failed ] ingest    INTERVALS_ICU_API_KEY is not set" in out
    assert "[skipped] alert     alerts off (NTFY_TOPIC not set)" in out
    assert (workdir / "derived" / "today.md").exists()  # the brief is still written
