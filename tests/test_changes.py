import datetime as dt
import json
import subprocess

import pytest
import yaml

from pipeline import brief
from pipeline.plan import changes as ch
from pipeline.plan.expand import expand, format_days
from pipeline.plan.rules import rule_warnings
from pipeline.plan.schema import PlanLoadError, load_checked, validate_plan
from pipeline.prescribe import LogRow, prescribe
from pipeline.publish.__main__ import build as publish_build
from pipeline.review.compliance import vacated_grades
from pipeline.review.debrief import to_markdown, vacated_item, verdict
from tests.conftest import ATHLETE, ROOT

D = dt.date
MOVE = {"type": "move", "session": "upper_me", "from": "2026-10-26 am", "to": "2026-10-27 am", "reason": "skipped Monday"}
SWAP = {"type": "swap", "a": "2026-10-27 pm", "b": "2026-10-28 pm"}


@pytest.fixture
def plan(plan_data, athlete):
    return validate_plan(plan_data, athlete)


def with_changes(plan, *entries):
    plan._changes = ch.parse(list(entries))
    return plan


def day(plan, athlete, date):
    (d,) = expand(plan, athlete, date, date)
    return d


def ids(d):
    return [(s.slot, s.session_id) for s in d.sessions]


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(("entry", "message"), [
    ({"type": "move", "session": "upper_me", "from": "2026-10-26", "to": "2026-10-27 am"}, "changes[0].from"),
    ({"type": "teleport", "session": "upper_me"}, "changes[0]"),
    ({"type": "drop", "session": "z2_60", "date": "2026-10-28 pm"}, "changes[0].reason"),
    ({"type": "swap", "a": "2026-10-27 pm", "b": "2026-10-28 pm", "extra": 1}, "changes[0].extra"),
])  # fmt: skip
def test_bad_entries_name_the_entry(entry, message):
    with pytest.raises(ch.ChangesError) as e:
        ch.parse([entry])
    assert any(line.startswith(message) for line in e.value.lines), e.value.lines


def test_empty_file_is_no_changes():
    assert ch.parse(None) == ()


# ------------------------------------------------------------------ applying


def test_move_carries_moved_from_and_leaves_a_marker(plan, athlete):
    with_changes(plan, MOVE)
    mon, tue = expand(plan, athlete, D(2026, 10, 26), D(2026, 10, 27))
    assert ids(mon) == [("mon_pm", "z2_90")]
    assert [(v.slot, v.session_id, v.note(mon.date)) for v in mon.vacated] == [("mon_am", "upper_me", "moved to Tue AM: skipped Monday")]
    moved = tue.sessions[2]
    assert (moved.slot, moved.order, moved.session_id) == ("tue_am", 2, "upper_me")  # added to the end of the slot
    assert (str(moved.moved_from), moved.home_slot, moved.moved_note, moved.change_reason) == ("2026-10-26 am", "mon_am", "(moved from Mon)", "skipped Monday")
    assert all(s.moved_from is None for s in tue.sessions[:2])


def test_swap_exchanges_two_slots(plan, athlete):
    with_changes(plan, SWAP)
    tue, wed = expand(plan, athlete, D(2026, 10, 27), D(2026, 10, 28))
    assert ids(tue)[-1] == ("tue_pm", "z2_60") and tue.sessions[-1].moved_note == "(moved from Wed)"
    assert ids(wed)[-1] == ("wed_pm", "mobility_trunk") and wed.sessions[-1].moved_note == "(moved from Tue)"
    assert [v.note(tue.date) for v in tue.vacated] == ["moved to Wed PM"]


def test_changes_apply_in_file_order(plan, athlete):
    third = {"type": "move", "session": "mobility_trunk", "from": "2026-10-27 pm", "to": "2026-10-28 pm"}
    with_changes(plan, MOVE, SWAP, third)
    with pytest.raises(ch.ChangesError) as e:
        expand(plan, athlete, D(2026, 10, 26), D(2026, 10, 28))
    assert e.value.lines == ["changes[2]: can't move mobility_trunk: it isn't at 2026-10-27 pm; after the changes before it, it's at 2026-10-28 pm"]


def test_swapping_back_clears_moved_from(plan, athlete):
    with_changes(plan, SWAP, SWAP)
    tue = day(plan, athlete, D(2026, 10, 27))
    assert all(s.moved_from is None for s in tue.sessions) and tue.vacated == ()


def test_same_day_move_says_am_or_pm(plan, athlete):
    with_changes(plan, {"type": "move", "session": "upper_me", "from": "2026-10-26 am", "to": "2026-10-26 pm"})
    mon = day(plan, athlete, D(2026, 10, 26))
    assert ids(mon) == [("mon_pm", "z2_90"), ("mon_pm", "upper_me")]
    assert mon.sessions[1].moved_note == "(moved from AM)" and mon.vacated[0].note(mon.date) == "moved to PM"


def test_drop(plan, athlete):
    with_changes(plan, {"type": "drop", "session": "z2_60", "date": "2026-10-28 pm", "reason": "travel"})
    wed = day(plan, athlete, D(2026, 10, 28))
    assert ids(wed) == [("wed_am", "lower_me")]
    assert [(v.kind, v.note(wed.date)) for v in wed.vacated] == [("dropped", "dropped: travel")]


@pytest.mark.parametrize(("entry", "message"), [
    ({"type": "move", "session": "nope", "from": "2026-10-26 am", "to": "2026-10-27 am"}, "session 'nope' isn't in the plan"),
    ({"type": "move", "session": "upper_me", "from": "2026-10-26 am", "to": "2027-06-01 am"}, "2027-06-01 am is outside the plan"),
    ({"type": "move", "session": "upper_me", "from": "2026-10-26 am", "to": "2026-11-24 am"}, "2026-11-24 am has no designed week"),
    ({"type": "move", "session": "z2_60", "from": "2026-10-26 am", "to": "2026-10-27 am"}, "can't move z2_60: it isn't at 2026-10-26 am"),
    ({"type": "swap", "a": "2026-10-27 pm", "b": "2026-10-27 pm"}, "a and b are the same slot"),
])  # fmt: skip
def test_changes_that_cant_apply(plan, athlete, entry, message):
    with_changes(plan, entry)
    with pytest.raises(ch.ChangesError) as e:
        expand(plan, athlete, D(2026, 10, 26), D(2026, 10, 27))
    assert message in e.value.lines[0]


def test_expand_without_changes_and_text(plan, athlete):
    with_changes(plan, MOVE)
    assert ("mon_am", "upper_me") in ids(expand(plan, athlete, D(2026, 10, 26), D(2026, 10, 26), changes=())[0])
    text = "\n".join(format_days(plan, expand(plan, athlete, D(2026, 10, 26), D(2026, 10, 27))))
    assert "  am  upper_me               moved to Tue AM: skipped Monday" in text
    assert "Upper max effort - bench top set, back-offs, heavy pulling (moved from Mon)" in text
    assert text.index("moved to Tue AM") < text.index("z2_90")  # in slot order


# ------------------------------------------------------------------ loading and checking


def write_changes(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    return path


def test_load_checked_applies_and_checks_the_file(tmp_path):
    path = write_changes(tmp_path / "changes.yaml", [MOVE])
    plan, athlete = load_checked(ROOT / "config" / "plan.yaml", ATHLETE, changes_path=path)
    assert len(plan.changes) == 1 and ("tue_am", "upper_me") in ids(day(plan, athlete, D(2026, 10, 27)))
    write_changes(path, [MOVE, MOVE])
    with pytest.raises(PlanLoadError) as e:
        load_checked(ROOT / "config" / "plan.yaml", ATHLETE, changes_path=path)
    assert e.value.exit_code == 1 and "changes[1]: can't move upper_me" in e.value.lines[1]


def test_default_path_is_beside_config_and_env_overrides(tmp_path, monkeypatch):
    from pipeline.plan.schema import default_changes_path

    monkeypatch.delenv("PEDESTAL_CHANGES")
    assert default_changes_path(tmp_path / "config" / "plan.yaml") == tmp_path / "training" / "logs" / "changes.yaml"
    assert default_changes_path(tmp_path / "plans" / "new.yaml") is None  # a candidate plan has no changes
    monkeypatch.setenv("PEDESTAL_CHANGES", str(tmp_path / "x.yaml"))
    assert default_changes_path(tmp_path / "config" / "plan.yaml") == tmp_path / "x.yaml"


def test_new_entries_cant_touch_the_past_but_history_stays(plan):
    old = ch.parse([MOVE])
    now = ch.parse([MOVE, SWAP])
    assert ch.past_problems(now, old, D(2026, 10, 28)) == [
        "changes[1]: 2026-10-27 pm is before today (2026-10-28); only entries already committed may lie in the past"]  # fmt: skip
    assert ch.past_problems(now, old, D(2026, 10, 27)) == []  # MOVE is history, so its 26 Oct is fine
    assert [i for i, _ in ch.new_entries(now, old)] == [1]


def test_committed_reads_git_head():
    def git(cmd, **kw):
        assert cmd[:2] == ["git", "show"] and cmd[2] == "HEAD:training/logs/changes.yaml"
        return subprocess.CompletedProcess(cmd, 0, yaml.safe_dump([MOVE]), "")

    assert ch.committed(ch.CHANGES_PATH, run=git) == ch.parse([MOVE])
    missing = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128, "", "fatal")  # noqa: E731
    assert ch.committed(ch.CHANGES_PATH, run=missing) == ()


def test_check_cli(tmp_path, capsys, monkeypatch):
    path = write_changes(tmp_path / "changes.yaml", [MOVE, SWAP])
    no_git = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128, "", "fatal")  # noqa: E731
    args = ["--plan", str(ROOT / "config" / "plan.yaml"), "--athlete", str(ATHLETE), "--changes", str(path)]
    assert ch.main(args + ["--today", "2026-10-26"], run=no_git) == 0
    out = capsys.readouterr().out
    assert "OK - 2 change(s), 2 new" in out
    assert "warning: strength and endurance under 6 h apart: handbike_intervals (Tue 27 Oct AM) and upper_me (Tue 27 Oct AM)" in out
    assert ch.main(args + ["--today", "2026-10-28"], run=no_git) == 1
    assert "REFUSED" in capsys.readouterr().out


# ------------------------------------------------------------------ design rules


def test_rule_warnings(plan, athlete):
    with_changes(plan,
                 {"type": "move", "session": "upper_me", "from": "2026-11-02 am", "to": "2026-10-27 pm"},  # 34 h after Mon's
                 {"type": "move", "session": "handbike_intervals", "from": "2026-10-27 am", "to": "2026-10-30 am"})  # fmt: skip
    plan_data_vo2 = expand(plan, athlete, D(2026, 10, 26), D(2026, 11, 2))
    warnings = rule_warnings(plan_data_vo2, athlete)
    assert "two upper max-effort sessions within 48 h: upper_me (Mon 26 Oct AM) and upper_me (Tue 27 Oct PM)" in warnings
    assert any(w.startswith("strength and endurance under 6 h apart: upper_de (Fri 30 Oct AM) and handbike_intervals") or
               w.startswith("strength and endurance under 6 h apart: handbike_intervals (Fri 30 Oct AM)") for w in warnings)  # fmt: skip
    assert not any("VO2" in w for w in warnings)  # the threshold ride is 92-95%: not VO2
    assert rule_warnings(expand(plan, athlete, D(2026, 10, 26), D(2026, 10, 26)), athlete) == []  # AM strength, PM ride: fine


def test_vo2_rule(plan_data, athlete):
    plan_data["sessions"]["bike_vo2"] = {"modality": "bike", "summary": "VO2 5 x 4", "targets": {"tss": 70},
                                         "steps": [{"kind": "work", "duration_s": 240, "target": {"pct_ftp": [1.08, 1.15]}}]}  # fmt: skip
    plan_data["week_templates"]["accumulation"]["wed_pm"] = "bike_vo2"
    plan = validate_plan(plan_data, athlete)
    warnings = rule_warnings(expand(plan, athlete, D(2026, 10, 26), D(2026, 11, 1)), athlete)
    assert "bike VO2 and hand-bike VO2 within 48 h: handbike_intervals (Tue 27 Oct AM) and bike_vo2 (Wed 28 Oct PM)" in warnings


def test_rule_warnings_only_around_the_changed_dates(plan, athlete):
    with_changes(plan, MOVE)
    days = expand(plan, athlete, D(2026, 10, 24), D(2026, 10, 30))
    assert len(rule_warnings(days, athlete, around={D(2026, 10, 27)})) == 2
    assert rule_warnings(days, athlete, around={D(2026, 10, 30)}) == []


# ------------------------------------------------------------------ the rest of the pipeline


def test_compliance_marks_the_original_slot(plan, athlete):
    with_changes(plan, MOVE, {"type": "drop", "session": "z2_60", "date": "2026-10-28 pm", "reason": "travel"})
    days = expand(plan, athlete, D(2026, 10, 26), D(2026, 10, 28))
    rows = [(g.date.isoformat(), g.slot, g.session, g.modality, g.grade, g.note) for g in vacated_grades(days, plan.sessions)]
    assert rows == [("2026-10-26", "mon_am", "upper_me", "strength", "moved", "moved to Tue AM: skipped Monday"),
                    ("2026-10-28", "wed_pm", "z2_60", "bike", "dropped", "dropped: travel")]  # fmt: skip
    assert vacated_grades(days, plan.sessions, until=D(2026, 10, 27))[0].session == "upper_me"


def test_prescription_follows_the_session_and_its_slot_history(plan, athlete):
    history = [LogRow(D(2026, 10, 26), "upper_me", "bench_press", "top", 105, 6, 8)]  # last Monday's top set
    with_changes(plan, {"type": "move", "session": "upper_me", "from": "2026-11-02 am", "to": "2026-11-03 am"})
    rx = [p for p in prescribe(plan, athlete, history, {D(2026, 11, 3): "amber", D(2026, 11, 2): "green"}, D(2026, 11, 2), D(2026, 11, 3))
          if p.lift == "bench_press" and p.role == "top"]  # fmt: skip
    (p,) = rx
    assert (p.date, p.slot, p.basis, p.readiness) == (D(2026, 11, 3), "tue_am", "last", "amber")  # Tuesday's readiness
    assert "105" in p.reason


def test_publish_touches_only_the_affected_days(plan, athlete):
    before, _ = publish_build(plan, athlete, [], {}, D(2026, 10, 26), D(2026, 11, 1))
    with_changes(plan, MOVE)
    after, _ = publish_build(plan, athlete, [], {}, D(2026, 10, 26), D(2026, 11, 1))
    old = {e["external_id"]: e for e in before}
    new = {e["external_id"]: e for e in after}
    differ = {eid for eid in old.keys() | new.keys() if old.get(eid) != new.get(eid)}
    assert {eid.split(":")[2] for eid in differ} == {"2026-10-26", "2026-10-27"}
    moved = next(e for e in after if e["external_id"].endswith(":2026-10-27:tue_am:2"))
    assert "Moved from Mon 26 Oct AM: skipped Monday." in moved["description"]


def test_debrief_shows_moves(plan, athlete):
    with_changes(plan, MOVE)
    (v,) = day(plan, athlete, D(2026, 10, 26)).vacated
    item = vacated_item(v, D(2026, 10, 26), plan)
    assert (item["kind"], item["grade"], item["grade_note"]) == ("vacated", "moved", "moved to Tue AM: skipped Monday")
    ride = {"kind": "endurance", "slot": "tue_pm", "session": "z2_60", "summary": "Z2 60 min", "grade": "held", "grade_note": "",
            "planned_min": 60, "moved_note": "(moved from Wed)"}  # fmt: skip
    assert verdict([item, ride], True) == "Held the plan."  # the moved-away slot isn't a miss
    md = to_markdown({"date": "2026-10-26", "verdict": "x", "note_for_today": "y",
                      "context": {"readiness": None, "sleep_h": None, "load_done": 0, "load_planned": 0}, "items": [item, ride], "problems": []})  # fmt: skip
    assert "## AM: Upper max effort - bench top set, back-offs, heavy pulling\nmoved to Tue AM: skipped Monday" in md
    assert "## PM: Z2 60 min (moved from Wed)" in md


def test_brief_shows_moves(plan, athlete, tmp_path):
    with_changes(plan, MOVE)
    tue = brief.build(plan, athlete, D(2026, 10, 27), tmp_path)
    upper = next(s for s in tue["sessions"] if s["session"] == "upper_me")
    assert (upper["moved_from"], upper["moved_note"], upper["change_reason"]) == ("2026-10-26 am", "(moved from Mon)", "skipped Monday")
    assert "## AM: Upper max effort - bench top set, back-offs, heavy pulling (moved from Mon)" in brief.to_markdown(tue)
    mon = brief.build(plan, athlete, D(2026, 10, 26), tmp_path)
    assert mon["moved_away"] == [{"slot": "mon_am", "session": "upper_me", "summary": plan.sessions["upper_me"].summary,
                                  "note": "moved to Tue AM: skipped Monday"}]  # fmt: skip
    assert "*AM: Upper max effort - bench top set, back-offs, heavy pulling: moved to Tue AM: skipped Monday.*" in brief.to_markdown(mon)
    json.dumps(tue)  # today.json stays plain JSON


def test_repo_changes_file_is_valid():
    """training/logs/changes.yaml applies to config/plan.yaml."""
    plan, athlete = load_checked(ROOT / "config" / "plan.yaml", ATHLETE, changes_path=ROOT / "training" / "logs" / "changes.yaml")
    expand(plan, athlete, plan.valid_from, plan.valid_to)


# ------------------------------------------------------------------ done_late

LATE = {"type": "done_late", "session": "upper_me", "planned": "2026-10-26 am", "done": "2026-10-27 pm", "reason": "skipped Monday"}


def test_done_late_applies_like_a_move_with_its_own_words(plan, athlete):
    with_changes(plan, LATE)
    mon, tue = expand(plan, athlete, D(2026, 10, 26), D(2026, 10, 27))
    late = next(s for s in tue.sessions if s.session_id == "upper_me")
    assert (late.slot, late.change_kind, late.moved_note, late.home_slot) == ("tue_pm", "done_late", "(done late, planned Mon)", "mon_am")
    assert [(v.kind, v.note(mon.date)) for v in mon.vacated] == [("done_late", "done late: Tue PM (skipped Monday)")]
    rows = [(g.grade, g.note) for g in vacated_grades([mon], plan.sessions)]
    assert rows == [("done_late", "done late: Tue PM (skipped Monday)")]


def test_done_late_may_be_added_after_the_fact_but_not_ahead(plan):
    now = ch.parse([LATE])
    assert ch.past_problems(now, (), D(2026, 10, 29)) == []  # added two days later: fine
    assert ch.past_problems(now, (), D(2026, 10, 26)) == [
        "changes[0]: done 2026-10-27 pm is after today (2026-10-26); done_late records what already happened"]  # fmt: skip


def test_done_late_must_be_later_than_planned(plan, athlete):
    with_changes(plan, {**LATE, "done": "2026-10-26 am"})
    with pytest.raises(ch.ChangesError) as e:
        expand(plan, athlete, D(2026, 10, 26), D(2026, 10, 27))
    assert "must be after planned" in e.value.lines[0]


def test_done_late_prescription_uses_the_monday_history(plan, athlete):
    history = [LogRow(D(2026, 10, 26), "upper_me", "bench_press", "top", 105, 6, 8)]
    with_changes(plan, {**LATE, "planned": "2026-11-02 am", "done": "2026-11-03 pm"})
    (p,) = [p for p in prescribe(plan, athlete, history, {}, D(2026, 11, 3), D(2026, 11, 3)) if p.lift == "bench_press" and p.role == "top"]
    assert (p.slot, p.basis) == ("tue_pm", "last")


def test_check_cli_with_done_late_skips_rule_warnings(tmp_path, capsys):
    path = write_changes(tmp_path / "changes.yaml", [LATE])
    no_git = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128, "", "fatal")  # noqa: E731
    args = ["--plan", str(ROOT / "config" / "plan.yaml"), "--athlete", str(ATHLETE), "--changes", str(path), "--today", "2026-10-29"]
    assert ch.main(args, run=no_git) == 0
    out = capsys.readouterr().out
    assert "OK - 1 change(s), 1 new" in out and "warning" not in out
