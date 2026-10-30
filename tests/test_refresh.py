import datetime as dt
import json
import subprocess
from pathlib import Path

from pipeline.dashboard import refresh
from tests.conftest import ATHLETE
from tests.test_export_rows import analysis, dashboard, debrief, today

UTC = dt.timezone.utc
REQUESTED = dt.datetime(2026, 10, 27, 18, 3, 57, tzinfo=UTC)


class FakeGit:
    """Answers the git commands refresh runs. Each `fetch origin data` moves to the next latest.json."""

    def __init__(self, latests, fail=None):
        self.latests, self.fetches, self.calls, self.fail, self.pushed = list(latests), 0, [], fail, None

    def __call__(self, cmd, cwd=None, **kw):
        assert cmd[0] == "git"
        args = cmd[1:]
        self.calls.append(args)
        ok = lambda out="": subprocess.CompletedProcess(cmd, 0, out, "")  # noqa: E731
        if self.fail and args[0] == self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "fatal: could not read from remote repository")
        if args[:2] == ["fetch", "-q"] and args[3] == "data":
            self.fetches += 1
            return ok()
        if args[:2] == ["worktree", "add"]:
            Path(args[4]).mkdir(parents=True)
            return ok()
        if args[0] == "commit":
            self.pushed = {p.name: p.read_text(encoding="utf-8") for p in (Path(cwd) / "requests").glob("*.json")}
            return ok()
        if args[0] == "show":
            name = args[1].split(":", 1)[1]
            if name == "analysis/latest.json":
                i = min(self.fetches, len(self.latests)) - 1
                data = self.latests[i] if i >= 0 else None
            else:
                data = {"dashboard.json": dashboard(), "today.json": today(), "debrief.json": debrief()}.get(name)
            return ok(json.dumps(data)) if data is not None else subprocess.CompletedProcess(cmd, 128, "", "fatal: path does not exist")
        return ok()


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run(tmp_path, git, *extra, clock=None):
    clock = clock or Clock()
    five = REQUESTED + dt.timedelta(minutes=5)
    times = iter([REQUESTED, five, five, REQUESTED + dt.timedelta(minutes=6)])  # requested, exported, snapshot, finished
    code = refresh.main(["--out", str(tmp_path / "rows"), "--snapshot", str(tmp_path / "snapshot.json"), "--poll", "20", "--athlete", str(ATHLETE), *extra],
                        run=git, sleep=clock.sleep, clock=clock, now=lambda: next(times))  # fmt: skip
    return code, clock


def old():
    a = analysis()
    a["generated_at"] = "2026-10-27T17:50+01:00"  # before the request
    return a


def new(status="ok", at="2026-10-27T19:03+01:00"):  # 18:03 UTC: same minute as the request
    a = analysis() if status == "ok" else {"status": status, "activity_id": "i9"}
    a["generated_at"], a["activity_id"] = at, "i9"
    return a


def test_pushes_a_request_waits_and_exports(tmp_path, capsys):
    git = FakeGit([old(), old(), new()])
    code, clock = run(tmp_path, git)
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    datasets = out.pop("datasets")
    assert len(datasets) == 24 and datasets[-7:] == ["week_outline", "fitness_week", "zones_week", "heavy_reps_week", "plan_sessions", "plan_lifts", "plan_days"]
    assert (tmp_path / "rows" / "week_outline.json").exists()
    assert out == {"requested_at": "2026-10-27T18:03:57Z", "finished_at": "2026-10-27T18:09:57Z", "result": "ok",
                   "activity_id": "i9", "status_detail": "", "rows": str(tmp_path / "rows"), "pending": None,
                   "snapshot": str(tmp_path / "snapshot.json")}  # fmt: skip
    snap = json.loads((tmp_path / "snapshot.json").read_text(encoding="utf-8"))
    assert snap["exported_at"] == "2026-10-27T18:08:57Z" and len(snap["rows"]) == 24 and snap["rows"]["summary"][0]["exported_at"]
    assert git.pushed == {"20261027T180357Z.json": '{"activity": "latest", "requested_at": "2026-10-27T18:03:57Z", "by": "claude-chat"}\n'}
    assert ["push", "-q", "origin", "HEAD:refs/heads/requests"] in git.calls
    assert any(c[:2] == ["worktree", "remove"] for c in git.calls)
    assert clock.t == 40  # two polls of 20 s
    summary = json.loads((tmp_path / "rows" / "summary.json").read_text(encoding="utf-8"))[0]
    assert summary["exported_at"] == "2026-10-27T18:08:57Z"


def test_never_pushes_to_main(tmp_path, capsys):
    git = FakeGit([new()])
    run(tmp_path, git)
    pushes = [c for c in git.calls if c[0] == "push"]
    assert pushes == [["push", "-q", "origin", "HEAD:refs/heads/requests"]]


def test_no_new_activity(tmp_path, capsys):
    code, _ = run(tmp_path, FakeGit([new("no_new_activity")]))
    assert code == 0 and json.loads(capsys.readouterr().out)["result"] == "no_new_activity"


def test_error_result_is_reported(tmp_path, capsys):
    e = new("error")
    e["error"] = "activity i9 not found"
    code, _ = run(tmp_path, FakeGit([e]))
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and (out["result"], out["status_detail"]) == ("error", "activity i9 not found")
    assert (tmp_path / "rows" / "summary.json").exists()  # the dashboard still refreshes


def test_times_out_after_12_minutes_and_still_exports(tmp_path, capsys):
    code, clock = run(tmp_path, FakeGit([old()]))
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["result"] == "timed_out" and clock.t == 720
    assert (tmp_path / "rows" / "latest_session.json").exists()


def test_a_failed_push_is_an_error_and_exports_nothing(tmp_path, capsys):
    code, _ = run(tmp_path, FakeGit([new()], fail="push"))
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["result"] == "error" and "git push" in out["status_detail"]
    assert not (tmp_path / "rows").exists()


def test_no_request_just_exports(tmp_path, capsys):
    git = FakeGit([new()])
    code, _ = run(tmp_path, git, "--no-request")
    assert code == 0 and not any(c[0] == "push" for c in git.calls)
    assert json.loads(capsys.readouterr().out)["requested_at"] is None


def test_an_invalid_plan_stops_before_any_request(tmp_path, capsys):
    git = FakeGit([new()])
    (tmp_path / "plan.yaml").write_text("plan_id: [", encoding="utf-8")
    assert refresh.main(["--plan", str(tmp_path / "plan.yaml"), "--athlete", str(ATHLETE)], run=git) != 0
    assert git.calls == []


def test_refuses_an_odd_activity(tmp_path, capsys):
    assert refresh.main(["--activity", "latest; rm -rf /"], run=FakeGit([])) == 2


def test_generated_at_without_an_offset_is_utc():
    assert refresh.generated({"generated_at": "2026-10-27T18:03"}) == dt.datetime(2026, 10, 27, 18, 3, tzinfo=UTC)
    assert refresh.generated(None) is None
