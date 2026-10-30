import csv
import datetime as dt
import json
import subprocess

import pytest
import yaml

from pipeline.coach import pending as pd
from pipeline.coach import snapshot
from pipeline.ingest.tidy import apply_body_log
from pipeline.plan import changes as ch
from pipeline.plan.schema import load_checked
from tests.conftest import ATHLETE, ROOT

TODAY = dt.date(2026, 10, 29)
HEADER = "date,session,lift,role,kg,reps,rpe,notes\n"
LOGGED = "2026-10-19,test_bench,bench_press,amrap,100,8,8.5,\n"


@pytest.fixture
def repo(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "changes.yaml").write_text(
        "# Session changes\n\n- { type: done_late, session: upper_me, planned: 2026-10-26 am, done: 2026-10-27 pm, reason: \"skipped Monday\" }\n",
        encoding="utf-8")  # fmt: skip
    (logs / "strength.csv").write_text(HEADER + LOGGED, encoding="utf-8")
    return tmp_path


def run(repo, items, today=TODAY):
    logs = repo / "logs"
    plan, athlete = load_checked(ROOT / "config" / "plan.yaml", ATHLETE, changes_path=logs / "changes.yaml")
    res = pd.apply(items, plan=plan, athlete=athlete, today=today, changes_path=logs / "changes.yaml",
                   log_path=logs / "strength.csv", body_path=logs / "body.csv")  # fmt: skip
    return res, {k: v for k, v in ((i, r["status"] + (": " + r["reason"] if r["reason"] else "")) for i, r in res["items"].items())}


def item(i, kind, payload, created="2026-10-29T10:00:00Z", status="pending"):
    return {"id": i, "kind": kind, "payload": payload, "status": status, "created": created, "key": "x", "source": "coach"}


def change(i, **p):
    return item(i, "session_change", p)


def sets(i, date, session, lift, *rows, created="2026-10-29T10:00:00Z"):
    return item(i, "lift_sets", {"date": date, "session": session, "lift": lift,
                                 "sets": [dict(zip(("role", "kg", "reps", "rpe"), r)) for r in rows]}, created)  # fmt: skip


# ------------------------------------------------------------------ session changes


def test_done_late_is_appended_and_the_file_still_applies(repo):
    res, st = run(repo, [change("a", type="done_late", session="lower_me", planned="2026-10-28 am", done="2026-10-29 pm", reason="busy")])
    assert st == {"a": "applied"} and res["files"] == [str(repo / "logs" / "changes.yaml")]
    text = (repo / "logs" / "changes.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Session changes")  # comments kept
    assert text.rstrip().endswith('- { type: done_late, session: lower_me, planned: 2026-10-28 am, done: 2026-10-29 pm, reason: "busy" }')
    plan, _ = load_checked(ROOT / "config" / "plan.yaml", ATHLETE, changes_path=repo / "logs" / "changes.yaml")
    assert len(plan.changes) == 2


@pytest.mark.parametrize(("payload", "reason"), [
    ({"type": "done_late", "session": "upper_mx", "planned": "2026-10-28 am", "done": "2026-10-29 pm"}, "session 'upper_mx' isn't in the plan"),
    ({"type": "done_late", "session": "z2_60", "planned": "2026-10-28 am", "done": "2026-10-29 pm"}, "can't mark done late z2_60: it isn't at 2026-10-28 am"),
    ({"type": "done_late", "session": "upper_me", "planned": "2026-10-26 am", "done": "2026-10-27 pm"}, "already in the changes log"),
    ({"type": "done_late", "session": "upper_re", "planned": "2026-11-05 am", "done": "2026-11-06 am"}, "is after today"),
    ({"type": "move", "session": "lower_me", "planned": "2026-10-28 am", "done": "2026-10-30 am"}, "is before today"),
    ({"type": "drop", "session": "z2_75", "planned": "2026-11-05 pm"}, "a drop needs a reason"),
    ({"type": "teleport", "session": "z2_75", "planned": "2026-11-05 pm"}, "unknown change type"),
    ({"type": "move", "session": "z2_75", "planned": "2026-11-05", "done": "2026-11-06 pm"}, "isn't 'YYYY-MM-DD am|pm'"),
    ({"type": "move", "session": "z2_75; rm -rf", "planned": "2026-11-05 pm", "done": "2026-11-06 pm"}, "session id missing or malformed"),
])  # fmt: skip
def test_bad_changes_are_rejected_and_nothing_is_written(repo, payload, reason):
    before = (repo / "logs" / "changes.yaml").read_text(encoding="utf-8")
    res, st = run(repo, [item("a", "session_change", payload)])
    assert st["a"].startswith("rejected") and reason in st["a"], st
    assert res["files"] == [] and (repo / "logs" / "changes.yaml").read_text(encoding="utf-8") == before


def test_the_same_change_twice_in_one_batch_is_applied_once(repo):
    p = {"type": "move", "session": "z2_75", "planned": "2026-11-05 pm", "done": "2026-11-06 pm", "reason": "travel"}
    res, st = run(repo, [item("a", "session_change", p, created="2026-10-29T10:00:00Z"), item("b", "session_change", p, created="2026-10-29T11:00:00Z")])
    assert st == {"a": "applied", "b": "rejected: already in the changes log"}
    assert (repo / "logs" / "changes.yaml").read_text(encoding="utf-8").count("z2_75") == 1


def test_a_reason_cant_break_the_yaml(repo):
    nasty = 'late "again"\n- { type: drop, session: z2_60, date: 2026-11-04 pm, reason: x }\t#'
    res, st = run(repo, [change("a", type="move", session="z2_75", planned="2026-11-05 pm", done="2026-11-06 pm", reason=nasty)])
    assert st == {"a": "applied"}
    entries = yaml.safe_load((repo / "logs" / "changes.yaml").read_text(encoding="utf-8"))
    assert len(entries) == 2 and entries[1]["type"] == "move" and "\n" not in entries[1]["reason"]
    assert ch.parse(entries)[1].reason.startswith('late "again"')


# ------------------------------------------------------------------ sets


def read_log(repo):
    with open(repo / "logs" / "strength.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_sets_after_a_done_late_in_the_same_batch_are_valid(repo):
    items = [sets("s", "2026-10-29", "lower_me", "trap_bar_deadlift", ("top", 170, 5, 8), ("backoff", 145, 5, None), created="2026-10-29T09:00:00Z"),
             change("c", type="done_late", session="lower_me", planned="2026-10-28 am", done="2026-10-29 pm", reason="Thursday")]  # fmt: skip
    res, st = run(repo, items)
    assert st == {"c": "applied", "s": "applied"}  # changes first, whatever order they were saved in
    rows = read_log(repo)
    assert [(r["date"], r["lift"], r["role"], r["kg"], r["reps"], r["rpe"]) for r in rows[1:]] == [
        ("2026-10-29", "trap_bar_deadlift", "top", "170", "5", "8"), ("2026-10-29", "trap_bar_deadlift", "backoff", "145", "5", "")]
    assert rows[1]["notes"] == "From Pedestal Coach (saved 2026-10-29)"
    assert set(res["files"]) == {str(repo / "logs" / "changes.yaml"), str(repo / "logs" / "strength.csv")}


@pytest.mark.parametrize(("args", "reason"), [
    (("2026-10-27", "upper_me", "trap_bar_deadlift", ("top", 150, 5, 8)), "trap_bar_deadlift is not part of session upper_me"),
    (("2026-10-29", "lower_me", "trap_bar_deadlift", ("top", 150, 5, 8)), "lower_me is not scheduled on 2026-10-29"),
    (("2026-10-27", "upper_me", "bench_press", ("top", 100, 6, 8.3)), "RPE must be 5-10 in half steps"),
    (("2026-10-27", "upper_me", "bench_press", ("heavy", 100, 6, 8)), "role 'heavy'"),
    (("2026-10-27", "upper_me", "bench_press", ("top", 100, 0, 8)), "reps must be a whole number"),
    (("2026-11-02", "upper_me", "bench_press", ("top", 100, 6, 8)), "is in the future"),
    (("2026-10-27", "upper_me", "benchpress!", ("top", 100, 6, 8)), "malformed"),
    (("2026-10-19", "test_bench", "bench_press", ("amrap", 100, 8, 8.5)), "already in the strength log"),
])  # fmt: skip
def test_bad_sets_are_rejected(repo, args, reason):
    res, st = run(repo, [sets("s", *args)])
    assert st["s"].startswith("rejected") and reason in st["s"], st
    assert len(read_log(repo)) == 1 and res["files"] == []  # only the row that was there


def test_sets_saved_twice_in_one_batch_are_written_once(repo):
    a = sets("a", "2026-10-27", "upper_me", "bench_press", ("top", 100, 6, 8.5), created="2026-10-29T10:00:00Z")
    b = sets("b", "2026-10-27", "upper_me", "bench_press", ("top", 100, 6, 8.5), created="2026-10-29T10:05:00Z")
    _, st = run(repo, [a, b])
    assert st == {"a": "applied", "b": "rejected: already in the strength log"}
    assert len(read_log(repo)) == 2


# ------------------------------------------------------------------ bodyweight and the rest


def test_bodyweight(repo):
    items = [item("w1", "bodyweight", {"date": "2026-10-29", "kg": 115.84}), item("w2", "bodyweight", {"date": "2026-10-29", "kg": 115.8}),
             item("w3", "bodyweight", {"date": "2026-10-30", "kg": 116}), item("w4", "bodyweight", {"date": "2026-10-28", "kg": 15})]  # fmt: skip
    res, st = run(repo, items)
    assert st == {"w1": "applied", "w2": "rejected: already in the body log", "w3": "rejected: 2026-10-30 is in the future", "w4": "rejected: kg must be 30-250"}
    assert (repo / "logs" / "body.csv").read_text(encoding="utf-8") == "date,kg,source,notes\n2026-10-29,115.8,coach,From Pedestal Coach (saved 2026-10-29)\n"


def test_only_pending_items_are_touched(repo):
    items = [item("a", "bodyweight", {"date": "2026-10-29", "kg": 116}, status="applied"),
             {"id": "b", "status": "pending", "kind": "rename_plan", "payload": {}}, {"id": "../etc", "status": "pending"}]  # fmt: skip
    res, st = run(repo, items)
    assert st == {"a": "skipped: status is 'applied'", "b": "rejected: unreadable item"}  # a malformed id isn't reported at all
    assert (res["applied"], res["rejected"], res["skipped"], res["files"]) == (0, 1, 1, [])


def test_read_items_from_a_store_dump(tmp_path):
    (tmp_path / "d1.json").write_text(json.dumps({"kind": "bodyweight", "payload": {"date": "2026-10-29", "kg": 116}, "status": "pending"}), encoding="utf-8")
    (tmp_path / "d2.json").write_text("{not json", encoding="utf-8")
    items = pd.read_items(tmp_path)
    assert items[0]["id"] == "d1" and items[0]["kind"] == "bodyweight"
    assert items[1] == {"id": "d2", "status": "pending", "kind": None, "_unreadable": True}


def test_commit_touches_only_the_logs_and_pushes_main():
    calls = []

    def git(cmd, **kw):
        calls.append(cmd[1:])
        out = {"rev-parse": "main" if "--abbrev-ref" in cmd else "abc1234"}.get(cmd[1], "")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    assert pd.commit(["training/logs/changes.yaml"], "Coach inputs", push=True, run=git) == "abc1234"
    assert ["commit", "-q", "-m", "Coach inputs", "--", "training/logs/changes.yaml"] in calls
    assert ["push", "-q", "origin", "HEAD:main"] in calls


def test_commit_refuses_off_main():
    git = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "feature", "")  # noqa: E731
    with pytest.raises(RuntimeError, match="not on main"):
        pd.commit(["x"], "m", push=True, run=git)


def test_cli_writes_results(repo, tmp_path, capsys):
    items = tmp_path / "items"
    items.mkdir()
    (items / "w.json").write_text(json.dumps({"kind": "bodyweight", "payload": {"date": "2026-10-29", "kg": 116}, "status": "pending"}), encoding="utf-8")
    logs = repo / "logs"
    code = pd.main([str(items), "--today", "2026-10-29", "--plan", str(ROOT / "config" / "plan.yaml"), "--athlete", str(ATHLETE),
                    "--changes", str(logs / "changes.yaml"), "--log", str(logs / "strength.csv"), "--body", str(logs / "body.csv"),
                    "--results", str(tmp_path / "res.json")])  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["applied"] == 1 and out["commit"] is None and out["items"] == {"w": {"status": "applied", "reason": ""}}
    assert json.loads((tmp_path / "res.json").read_text(encoding="utf-8")) == out


# ------------------------------------------------------------------ ingest and the snapshot


def test_body_log_overrides_intervals_weight():
    rows = [{"date": "2026-10-28", "weight_kg": "116.4"}, {"date": "2026-10-29", "weight_kg": "116.4"}]
    body = [{"date": "2026-10-29", "kg": "115.9"}, {"date": "2026-10-29", "kg": "115.8"}]  # the last one for a day wins
    assert apply_body_log(rows, body) == [{"date": "2026-10-28", "weight_kg": "116.4"}, {"date": "2026-10-29", "weight_kg": "115.8"}]


def test_snapshot_document(tmp_path):
    files = {"today.md": "# Brief", "wellness.csv": "date,hrv\n" + "".join(f"2026-10-{d:02d},50\n" for d in range(1, 30)),
             "analysis": "2026-10-25-i1.md\n2026-10-26-i2.md\n2026-10-27-i3.md\n2026-10-28-i4.md\nlatest.json\n"}  # fmt: skip

    def git(cmd, **kw):
        if cmd[1] == "show":
            name = cmd[2].split(":", 1)[1]
            text = files.get(name, "# analysis" if name.startswith("analysis/") else None)
            return subprocess.CompletedProcess(cmd, 0 if text is not None else 128, text or "", "")
        if cmd[1] == "ls-tree":
            return subprocess.CompletedProcess(cmd, 0, files["analysis"], "")
        return subprocess.CompletedProcess(cmd, 0, "f00ba12\n", "")

    (tmp_path / "training").mkdir()
    (tmp_path / "training" / "goals.md").write_text("# Goals", encoding="utf-8")
    doc = snapshot.build({"summary": [{"date": "2026-10-29"}]}, ref="origin/data", now=dt.datetime(2026, 10, 29, 18, tzinfo=dt.timezone.utc),
                         repo=tmp_path, run=git)  # fmt: skip
    assert (doc["exported_at"], doc["data_commit"]) == ("2026-10-29T18:00:00Z", "f00ba12")
    assert sorted(doc["files"]) == ["analysis/2026-10-26-i2.md", "analysis/2026-10-27-i3.md", "analysis/2026-10-28-i4.md", "today.md",
                                    "training/goals.md", "wellness.csv"]  # fmt: skip
    assert doc["files"]["wellness.csv"].count("\n") == 30  # header + 29 rows: under the tail


def test_snapshot_fits_the_store(tmp_path, monkeypatch):
    big = "date,x\n" + "".join(f"2026-10-01,{'y' * 2000}\n" for _ in range(200))
    git = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, big if cmd[1] == "show" and cmd[2].endswith(".csv") else "", "")  # noqa: E731
    monkeypatch.setattr(snapshot, "LIMIT", 300_000)
    doc = snapshot.build({}, ref="origin/data", now=dt.datetime(2026, 10, 29, tzinfo=dt.timezone.utc), repo=tmp_path, run=git)
    assert len(json.dumps(doc)) <= 300_000 and all(v.startswith("date,x\n") for k, v in doc["files"].items() if k.endswith(".csv"))
