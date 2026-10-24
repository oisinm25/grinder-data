import csv
import datetime as dt
import json
import shutil

from zoneinfo import ZoneInfo

import httpx
import pytest

from pipeline import run as run_mod
from pipeline.run import main
from tests.conftest import ROOT

REAL_BRIEF = run_mod.Runner.brief  # kept before the steps fixture swaps it for a fake
MON = dt.date(2026, 10, 26)
KEY = "fake-key-" + "0123456789abcdef"
TOPIC = "pedestal-" + "secret-topic-1234567890"
ENV = {"INTERVALS_ICU_API_KEY": KEY, "INTERVALS_ICU_ATHLETE_ID": "i99999", "NTFY_TOPIC": TOPIC}
FIXTURES = ROOT / "tests" / "fixtures" / "intervals"
ORDER = ["ingest", "readiness", "prescribe", "publish", "review", "debrief", "brief", "dashboard"]


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
        calls.made.append(("brief", self.names("failed"), {"status": self.status, "reason": self.status_reason}))
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


MADRID = ZoneInfo("Europe/Madrid")
IN_WINDOW = dt.datetime(2026, 10, 26, 6, 45, tzinfo=MADRID)


def run(*argv, ntfy=None, env=ENV, transport=None, now=IN_WINDOW):
    return main(["daily", "--day", "2026-10-26", *argv], env=env, transport=transport,
                ntfy_transport=httpx.MockTransport(ntfy or FakeNtfy()), sleep=lambda s: None, now=now)  # fmt: skip


def today_json(workdir, **fields):
    (workdir / "derived").mkdir(exist_ok=True)
    (workdir / "derived" / "today.json").write_text(json.dumps({"date": "2026-10-26", **fields}))


def wellness(workdir, sleep_h=""):
    (workdir / "derived").mkdir(exist_ok=True)
    (workdir / "derived" / "wellness.csv").write_text(f"date,readiness,readiness_reason,sleep_h\n2026-10-26,green,green: fine,{sleep_h}\n")


def lines(out):
    return [ln.split("]")[1].split()[0] for ln in out.splitlines() if ln.startswith("[")]


# ------------------------------------------------------------------ the steps


def test_all_steps_in_order_and_no_ntfy_when_all_is_well(workdir, steps, capsys):
    ntfy = FakeNtfy()
    assert run(ntfy=ntfy) == 0
    out = capsys.readouterr().out
    assert [c[0] for c in steps.made] == ["ingest", "prescribe", "publish", "review", "brief"]
    assert lines(out) == ORDER
    assert out.rstrip().endswith("daily 2026-10-26: 8 ok; provisional (no sleep yet)")
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
    assert ("brief", ["ingest"], {"status": "provisional", "reason": "no sleep yet"}) in steps.made  # the brief says what failed
    assert ntfy.bodies == ["Pedestal Mon 26 Oct: ingest failed."]
    assert "[ok     ] alert     sent: ingest failed" in out
    assert out.rstrip().endswith("daily 2026-10-26: 8 ok, 1 failed (ingest); provisional (no sleep yet)")


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
    assert log.startswith("# daily 2026-10-26: 8 ok")
    assert "## [ok     ] prescribe" in log and "prescribe output" in log


def test_sunday_daily_includes_the_weekly_review(workdir, steps, capsys):
    assert main(["daily", "--day", "2026-11-01", "--ignore-window"], env=ENV) == 0
    reviews = [argv for name, argv, _ in steps.made if name == "review"]
    assert reviews == [["--day", "2026-10-31", "--derived", "derived"], ["--week-ending", "2026-11-01", "--derived", "derived"]]
    assert "[ok     ] weekly" in capsys.readouterr().out


def test_weekly_reviews_the_latest_sunday(workdir, steps):
    assert main(["weekly", "--day", "2026-11-01"], env=ENV) == 0
    assert main(["weekly", "--day", "2026-11-03"], env=ENV) == 0
    reviews = [argv for name, argv, _ in steps.made if name == "review"]
    assert reviews == [["--week-ending", "2026-11-01", "--derived", "derived"]] * 2


# ------------------------------------------------------------------ the morning window and the day's status


UTC = dt.timezone.utc


@pytest.mark.parametrize(
    ("now", "runs"),
    [
        (dt.datetime(2026, 10, 26, 6, 29, tzinfo=MADRID), False),  # just before the window
        (dt.datetime(2026, 10, 26, 6, 30, tzinfo=MADRID), True),
        (dt.datetime(2026, 10, 26, 7, 0, tzinfo=MADRID), True),  # the end is inclusive
        (dt.datetime(2026, 10, 26, 7, 1, tzinfo=MADRID), False),
        (dt.datetime(2026, 10, 24, 4, 30, tzinfo=UTC), True),  # summer time: 04:30 UTC is 06:30 CEST
        (dt.datetime(2026, 10, 26, 5, 30, tzinfo=UTC), True),  # winter time: 05:30 UTC is 06:30 CET
        (dt.datetime(2026, 10, 26, 4, 30, tzinfo=UTC), False),  # winter: 04:30 UTC is only 05:30 CET
        (dt.datetime(2026, 10, 26, 6, 0, tzinfo=UTC), True),  # winter: the last cron slot, 07:00 CET
    ],
)
def test_window(workdir, steps, capsys, now, runs):
    assert main(["daily"], env=ENV, now=now, ntfy_transport=httpx.MockTransport(FakeNtfy())) == 0
    out = capsys.readouterr().out
    assert bool(steps.made) is runs
    if not runs:
        assert "outside the morning window" in out and "window 06:30-07:00" in out
        assert not (workdir / "derived").exists()  # nothing written


def test_default_day_is_the_local_date(workdir, steps, capsys):
    main(["daily"], env=ENV, now=dt.datetime(2026, 10, 24, 4, 45, tzinfo=UTC))  # 06:45 in Madrid on the 24th
    assert "Pedestal daily run for 2026-10-24 (06:45 Europe/Madrid)" in capsys.readouterr().out


def test_manual_run_ignores_the_window(workdir, steps):
    late = dt.datetime(2026, 10, 26, 22, 0, tzinfo=MADRID)
    assert run("--ignore-window", now=late) == 0
    assert [c[0] for c in steps.made][0] == "ingest"


def test_exits_once_today_is_final(workdir, steps, capsys):
    today_json(workdir, status="final", generated_at="2026-10-26T06:31")
    assert run() == 0
    assert "today's brief is already final (generated 2026-10-26T06:31)" in capsys.readouterr().out
    assert steps.made == []


def test_runs_again_while_provisional_or_on_a_new_day(workdir, steps):
    today_json(workdir, status="provisional")
    run()
    assert steps.made
    steps.made.clear()
    (workdir / "derived" / "today.json").write_text(json.dumps({"date": "2026-10-25", "status": "final"}))  # yesterday's
    run()
    assert steps.made


def test_final_when_todays_sleep_is_in(workdir, steps, capsys):
    wellness(workdir, sleep_h="7.2")
    run()
    brief = next(kw for name, _, kw in steps.made if name == "brief")
    assert brief == {"status": "final", "reason": ""}
    assert capsys.readouterr().out.rstrip().endswith("daily 2026-10-26: 8 ok; final")


def test_provisional_without_sleep_but_readiness_still_runs(workdir, steps, capsys):
    wellness(workdir, sleep_h="")
    run()
    brief = next(kw for name, _, kw in steps.made if name == "brief")
    assert brief == {"status": "provisional", "reason": "no sleep yet"}
    out = capsys.readouterr().out
    assert out.rstrip().endswith("daily 2026-10-26: 8 ok; provisional (no sleep yet)")
    assert [c[0] for c in steps.made] == ["ingest", "prescribe", "publish", "review", "brief"]


def test_each_failed_step_alerts_once_a_day(workdir, steps, monkeypatch):
    monkeypatch.setattr(run_mod.Runner, "brief", REAL_BRIEF)  # the real brief writes today.json
    monkeypatch.setattr(run_mod, "publish_main", steps.fake("publish", code=1))
    ntfy = FakeNtfy()
    assert run(ntfy=ntfy) == 1
    assert json.loads((workdir / "derived" / "today.json").read_text())["alerts_sent"] == ["publish"]
    run(ntfy=ntfy)  # the next slot, still failing: no second alert
    monkeypatch.setattr(run_mod, "review_main", steps.fake("review", code=1))
    run(ntfy=ntfy)  # a new failure gets its own alert
    assert ntfy.bodies == ["Pedestal Mon 26 Oct: publish failed.", "Pedestal Mon 26 Oct: review failed."]


def test_yesterdays_alerts_dont_silence_today(workdir, steps, monkeypatch):
    (workdir / "derived").mkdir()
    (workdir / "derived" / "today.json").write_text(json.dumps({"date": "2026-10-25", "alerts_sent": ["publish"]}))
    monkeypatch.setattr(run_mod, "publish_main", steps.fake("publish", code=1))
    ntfy = FakeNtfy()
    run(ntfy=ntfy)
    assert ntfy.bodies == ["Pedestal Mon 26 Oct: publish failed."]


def test_no_alert_flag_prints_instead(workdir, steps, monkeypatch, capsys):
    monkeypatch.setattr(run_mod, "publish_main", steps.fake("publish", code=1))
    ntfy = FakeNtfy()
    run("--no-alert", ntfy=ntfy)
    assert ntfy.bodies == []
    assert "[skipped] alert     not sent (--no-alert): Pedestal Mon 26 Oct: publish failed." in capsys.readouterr().out


def test_unreadable_athlete_file_is_reported(workdir, steps, capsys):
    (workdir / "config" / "athlete.yaml").write_text("schema_version: 1\n")
    assert run() == 1
    assert capsys.readouterr().out.startswith("error: config")
    assert steps.made == []


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
    code = main(["daily", "--day", "2026-10-26", "--ignore-window"], env=ENV, transport=httpx.MockTransport(svc.intervals),
                ntfy_transport=httpx.MockTransport(svc.ntfy), sleep=lambda s: None)  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.rstrip().endswith("daily 2026-10-26: 8 ok; provisional (no sleep yet)")
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
    main(["daily", "--day", "2026-11-01", "--ignore-window"], env=ENV, transport=httpx.MockTransport(svc.intervals),
         ntfy_transport=httpx.MockTransport(svc.ntfy), sleep=lambda s: None)  # fmt: skip
    week = (workdir / "derived" / "week.md").read_text(encoding="utf-8")
    assert week.startswith("# Week 26 Oct to 1 Nov 2026") and "## Flags" in week and "## Recovery" in week


def test_daily_without_secrets_reports_each_failure(workdir, capsys):
    code = main(["daily", "--day", "2026-10-26", "--ignore-window"], env={}, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert code == 1
    assert "[failed ] ingest    INTERVALS_ICU_API_KEY is not set" in out
    assert "[skipped] alert     alerts off (NTFY_TOPIC not set)" in out
    assert (workdir / "derived" / "today.md").exists()  # the brief is still written
