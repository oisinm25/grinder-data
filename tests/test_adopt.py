import copy
import datetime as dt
import shutil

import pytest
import yaml

from pipeline.common import load_yaml
from pipeline.plan.adopt import main
from tests.conftest import ATHLETE, ROOT


TODAY = "2026-10-24"  # Saturday: block0's test week is still running


@pytest.fixture
def repo(tmp_path):
    """A scratch copy of config/plan.yaml as the current plan, plus an empty plans/ dir."""
    (tmp_path / "config").mkdir()
    shutil.copyfile(ROOT / "config" / "plan.yaml", tmp_path / "config" / "plan.yaml")
    (tmp_path / "plans").mkdir()
    return tmp_path


@pytest.fixture
def plan():
    return copy.deepcopy(load_yaml(ROOT / "config" / "plan.yaml"))


def write(repo, data, name="new.yaml"):
    p = repo / "plans" / name
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


def adopt(repo, path, *extra, today=TODAY):
    return main([str(path), "--current", str(repo / "config" / "plan.yaml"), "--athlete", str(ATHLETE),
                 "--plans", str(repo / "plans"), "--derived", str(repo / "derived"), "--today", today, *extra])  # fmt: skip


def long_ride_sunday(plan):
    acc2 = plan["blocks"][1]["weeks"][1]
    acc2["swap"] = {"sat_am": None, "sun_am": "long_ride_3h30", "tue_am": ["handbike_intervals", "grinding_repeats_10"]}
    return plan


# ------------------------------------------------------------------ refusals


def test_invalid_plan_is_refused_naming_the_field(repo, plan, capsys):
    plan["sessions"]["z2_60"]["modality"] = "cycling"
    assert adopt(repo, write(repo, plan)) == 1
    out = capsys.readouterr().out
    assert out.startswith("INVALID") and "sessions.z2_60.modality" in out


def test_changing_a_past_day_is_refused(repo, plan, capsys):
    plan["week_templates"]["re_entry"]["wed_pm"] = "z2_75"  # Wed 14 Oct, already done
    assert adopt(repo, write(repo, plan)) == 1
    out = capsys.readouterr().out
    assert "REFUSED: this changes the past" in out
    assert "Wed 14 Oct 2026: sessions change from wed_am lower_reentry, wed_pm z2_60 to wed_am lower_reentry, wed_pm z2_75" in out
    assert "Calendar" not in out  # no preview for a refused plan


def test_editing_a_session_that_already_ran_is_refused(repo, plan, capsys):
    plan["sessions"]["test_bench"]["summary"] = "TEST - bench, edited afterwards"
    assert adopt(repo, write(repo, plan)) == 1
    assert "Mon 19 Oct 2026: session test_bench (mon_am) is edited, and it already ran on that day" in capsys.readouterr().out


def test_editing_an_ended_block_is_refused(repo, plan, capsys):
    plan["blocks"][0]["weekly_tss"] = [260, 410]
    assert adopt(repo, write(repo, plan), today="2026-10-27") == 1
    assert "block block0 ended Sun 25 Oct and can't be edited" in capsys.readouterr().out


def test_editing_a_started_week_is_refused(repo, plan, capsys):
    plan["blocks"][1]["weeks"][0]["weekly_tss"] = [600, 700]
    assert adopt(repo, write(repo, plan), today="2026-10-27") == 1
    assert "block block1 week acc1 started Mon 26 Oct and can't be edited" in capsys.readouterr().out


def test_moving_a_started_block_is_refused(repo, plan, capsys):
    plan["valid_from"] = dt.date(2026, 10, 13)
    plan["blocks"][0]["start"] = dt.date(2026, 10, 13)
    plan["blocks"][0]["weeks"][0]["start"] = dt.date(2026, 10, 13)
    assert adopt(repo, write(repo, plan)) == 1
    out = capsys.readouterr().out
    assert "block block0 moves its start from Wed 14 Oct to Tue 13 Oct, before today" in out
    assert "valid_from moves from 2026-10-14 to 2026-10-13, but the plan has already started" in out


def test_changing_a_past_checkpoint_is_refused(repo, plan, capsys):
    plan["strength_lines"][0]["checkpoints"][0][1] = 130.0
    assert adopt(repo, write(repo, plan)) == 1
    assert "bench_press checkpoint Mon 19 Oct 2026 is in the past: 131.5 -> 130" in capsys.readouterr().out


def test_adding_a_past_goal_is_refused(repo, plan, capsys):
    plan["goals"].append({"id": "late-entry", "priority": "C", "date": dt.date(2026, 10, 20), "description": "after the fact"})
    assert adopt(repo, write(repo, plan)) == 1
    assert "goal late-entry is dated Tue 20 Oct 2026, before today" in capsys.readouterr().out


def test_dropping_the_started_part_of_the_same_plan_is_refused(repo, plan, capsys):
    plan["blocks"] = plan["blocks"][1:]
    plan["valid_from"] = dt.date(2026, 10, 26)
    assert adopt(repo, write(repo, plan)) == 1
    assert "valid_from moves from 2026-10-14 to 2026-10-26" in capsys.readouterr().out


def test_a_new_macro_plan_starting_later_is_allowed(repo, plan, capsys):
    plan["plan_id"] = "winter-2026-27-v2"
    plan["blocks"] = plan["blocks"][1:]
    plan["valid_from"] = dt.date(2026, 10, 26)
    assert adopt(repo, write(repo, plan)) == 0
    out = capsys.readouterr().out
    assert "REFUSED" not in out and "new plan: winter-2026-27 -> winter-2026-27-v2" in out
    assert "removed block0 (Re-entry and test)" in out


def test_refused_with_yes_writes_nothing(repo, plan):
    before = (repo / "config" / "plan.yaml").read_bytes()
    plan["week_templates"]["re_entry"]["wed_pm"] = "z2_75"
    assert adopt(repo, write(repo, plan), "--yes") == 1
    assert (repo / "config" / "plan.yaml").read_bytes() == before
    assert not (repo / "plans" / "archive").exists() and not (repo / "plans" / "HISTORY.md").exists()


def test_same_plan_is_nothing_to_adopt(repo, plan, capsys):
    assert adopt(repo, write(repo, plan), "--yes") == 0
    out = capsys.readouterr().out
    assert "No changes from the current plan." in out and "nothing to adopt" in out
    assert not (repo / "plans" / "archive").exists()


# ------------------------------------------------------------------ the diff


def test_long_ride_moved_to_sunday_in_acc2(repo, plan, capsys):
    assert adopt(repo, write(repo, long_ride_sunday(plan))) == 0
    out = capsys.readouterr().out
    assert "week of Mon 2 Nov (block1 acc2): 13 -> 12 sessions, TSS 720 -> 694 (target 675-775)" in out
    assert "Sat 7 Nov: sat_am long_ride_3h30 removed" in out
    assert "Sun 8 Nov: sun_am handbike_easy_40 -> long_ride_3h30" in out
    assert "week of Mon 9 Nov" not in out  # only acc2 changes
    assert "no calendar changes in the next 7 days (Sat 24 Oct to Fri 30 Oct)" in out
    assert out.rstrip().endswith("Dry run: nothing changed. Re-run with --yes to adopt it.")


def test_calendar_preview_when_the_change_is_this_week(repo, plan, capsys):
    adopt(repo, write(repo, long_ride_sunday(plan)), today="2026-11-02")
    out = capsys.readouterr().out
    assert "delete Sat 7 Nov 07:00 Ride: Long ride 3 h 30, Z2, fuel 60-90 g carb/h" in out
    assert "update Sun 8 Nov 07:00 Ride: Long ride 3 h 30, Z2, fuel 60-90 g carb/h" in out


def test_strength_rule_changes_per_lift(repo, plan, capsys):
    plan["progressions"]["bench_top_linear"]["increment_kg"] = 5.0
    plan["sessions"]["upper_re"]["exercises"][1]["progression"] = "pull_double"  # bench_pull: accessory -> pull
    plan["sessions"]["upper_re"]["exercises"][1]["groups"][0]["reps"] = [5, 8]
    adopt(repo, write(repo, plan))
    out = capsys.readouterr().out
    assert "rule bench_top_linear: increment_kg 2.5 -> 5 (used by bench_press in upper_me)" in out
    assert "bench_pull in upper_re: accessory_double -> pull_double" in out
    assert "edited session upper_re" in out


def test_blocks_and_library_changes(repo, plan, capsys):
    plan["blocks"][4]["weekly_tss"] = [550, 700]
    plan["blocks"][4]["name"] = "Strength intensification II"
    plan["sessions"]["z2_120"] = {"modality": "bike", "summary": "Z2 2 h", "targets": {"tss": 92},
                                  "steps": [{"kind": "steady", "duration_s": 7200, "target": {"pct_ftp": [0.65, 0.72]}}]}  # fmt: skip
    plan["strength_lines"][0]["checkpoints"][1][1] = 133.0
    adopt(repo, write(repo, plan))
    out = capsys.readouterr().out
    assert "block2: weekly_tss 500-650 -> 550-700" in out
    assert "block2: renamed 'Strength intensification' -> 'Strength intensification II'" in out
    assert "added session z2_120" in out
    assert "bench_press: checkpoints from today: Sun 29 Nov 133" in out


def test_markdown_for_the_pull_request(repo, plan, capsys):
    adopt(repo, write(repo, long_ride_sunday(plan)), "--format", "markdown")
    out = capsys.readouterr().out
    assert out.startswith("### Plan check: ")
    assert "**Sessions by week (from today)**" in out and "  - Sun 8 Nov: sun_am handbike_easy_40 -> long_ride_3h30" in out


# ------------------------------------------------------------------ adopting


def test_yes_archives_copies_and_records(repo, plan, capsys):
    old = (repo / "config" / "plan.yaml").read_bytes()
    new = write(repo, long_ride_sunday(plan))
    assert adopt(repo, new, "--yes") == 0
    archived = repo / "plans" / "archive" / "winter-2026-27-2026-10-24.yaml"
    assert archived.read_bytes() == old
    assert (repo / "config" / "plan.yaml").read_bytes() == new.read_bytes()
    history = (repo / "plans" / "HISTORY.md").read_text(encoding="utf-8")
    assert "## 2026-10-24: winter-2026-27" in history and "winter-2026-27-2026-10-24.yaml" in history
    assert "Sessions by week (from today): 3 change(s)" in history
    assert "Adopted:" in capsys.readouterr().out

    plan["blocks"][1]["weeks"][2]["weekly_tss"] = [710, 810]  # a second change the same day
    assert adopt(repo, write(repo, plan, "again.yaml"), "--yes") == 0
    assert "block1 week acc3 (Mon 9 Nov): weekly_tss 700-800 -> 710-810" in capsys.readouterr().out
    assert (repo / "plans" / "archive" / "winter-2026-27-2026-10-24-2.yaml").exists()
    assert (repo / "plans" / "HISTORY.md").read_text(encoding="utf-8").count("## 2026-10-24") == 2


# ------------------------------------------------------------------ template and workflow


def test_template_is_a_valid_plan():
    from pipeline.plan.schema import load_checked

    plan, _ = load_checked(ROOT / "plans" / "TEMPLATE.yaml", ATHLETE)
    assert plan.plan_id == "example-spring-2027"


def test_plan_check_workflow_has_minimal_permissions():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "plan-check.yml").read_text(encoding="utf-8"))
    assert wf[True] == {"pull_request": {"paths": ["plans/**"]}}  # never pull_request_target
    assert wf["permissions"] == {"contents": "read", "pull-requests": "write"}
    steps = " ".join(str(s) for s in wf["jobs"]["check"]["steps"])
    assert "pipeline.plan.adopt" in steps and "--yes" not in steps  # it never adopts
