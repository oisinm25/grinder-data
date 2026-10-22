import csv
import datetime as dt
import subprocess
import sys

import pytest

from pipeline.plan.expand import expand, week_summaries
from pipeline.plan.schema import validate_plan
from tests.conftest import ROOT

D = dt.date


@pytest.fixture
def plan(plan_data, athlete):
    return validate_plan(plan_data, athlete)


def day(plan, athlete, date):
    (d,) = expand(plan, athlete, date, date)
    return d


def ids(d):
    return [(s.slot, s.session_id) for s in d.sessions]


def step(scheduled, *path):
    node = scheduled.steps[path[0]]
    for i in path[1:]:
        node = node.steps[i]
    return node


# ------------------------------------------------------------------ calendar


def test_block1_monday_from_template(plan, athlete):
    d = day(plan, athlete, D(2026, 10, 26))
    assert (d.block.id, d.week.label) == ("block1", "acc1")
    assert ids(d) == [("mon_am", "upper_me"), ("mon_pm", "z2_90")]


def test_slot_list_keeps_order(plan, athlete):
    d = day(plan, athlete, D(2026, 10, 27))
    assert ids(d) == [("tue_am", "handbike_intervals"), ("tue_am", "grinding_repeats_8"), ("tue_pm", "mobility_trunk")]
    assert [s.order for s in d.sessions] == [0, 1, 0]


def test_swaps_apply_to_their_week_only(plan, athlete):
    assert ids(day(plan, athlete, D(2026, 11, 7))) == [("sat_am", "long_ride_3h30")]  # acc2
    assert ids(day(plan, athlete, D(2026, 10, 31))) == [("sat_am", "long_ride_3h")]  # acc1, template
    assert ids(day(plan, athlete, D(2026, 11, 3)))[1] == ("tue_am", "grinding_repeats_10")
    assert ids(day(plan, athlete, D(2026, 11, 13)))[1] == ("fri_pm", "bike_threshold_3x15")  # acc3
    assert ids(day(plan, athlete, D(2026, 11, 6)))[1] == ("fri_pm", "bike_threshold_2x20")  # acc2, template


def test_null_swap_drops_the_slot(plan_data, athlete):
    plan_data["blocks"][1]["weeks"][0]["swap"] = {"sun_am": None}
    plan = validate_plan(plan_data, athlete)
    d = day(plan, athlete, D(2026, 11, 1))
    assert d.sessions == () and not d.design_pending


def test_swap_can_fill_an_empty_slot(plan_data, athlete):
    plan_data["blocks"][1]["weeks"][0]["swap"] = {"sat_pm": "mobility_trunk"}
    plan = validate_plan(plan_data, athlete)
    assert ids(day(plan, athlete, D(2026, 10, 31))) == [("sat_am", "long_ride_3h"), ("sat_pm", "mobility_trunk")]


def test_week_boundary_switches_template(plan, athlete):
    sun, mon = expand(plan, athlete, D(2026, 10, 18), D(2026, 10, 19))
    assert (sun.week.label, ids(sun)) == ("re-entry", [("sun_am", "rest")])
    assert (mon.week.label, ids(mon)[0]) == ("test-week", ("mon_am", "test_bench"))


def test_block_starting_midweek_uses_that_weekday(plan, athlete):
    d = day(plan, athlete, D(2026, 10, 14))  # a Wednesday
    assert ids(d) == [("wed_am", "lower_reentry"), ("wed_pm", "z2_60")]


def test_design_pending_day_has_no_sessions(plan, athlete):
    d = day(plan, athlete, D(2026, 11, 24))
    assert d.block.id == "athx" and d.design_pending and d.week is None and d.sessions == ()


def test_range_is_clipped_to_plan(plan, athlete):
    days = expand(plan, athlete, D(2026, 10, 1), D(2026, 10, 15))
    assert [d.date for d in days] == [D(2026, 10, 14), D(2026, 10, 15)]
    assert expand(plan, athlete, D(2027, 5, 1), D(2027, 5, 7)) == []
    assert len(expand(plan, athlete, plan.valid_from, plan.valid_to)) == (plan.valid_to - plan.valid_from).days + 1


# ------------------------------------------------------------------ target resolution


def test_bike_pct_ftp_resolves_to_watts(plan, athlete):
    z2 = day(plan, athlete, D(2026, 10, 26)).sessions[1]
    steady = step(z2, 1)
    assert steady.target.watts == (195, 216)  # 0.65-0.72 x 300 W
    assert not steady.target.provisional


def test_handbike_uses_handbike_ftp_and_is_provisional(plan, athlete):
    hb = day(plan, athlete, D(2026, 10, 27)).sessions[0]
    work = step(hb, 1, 0)
    assert work.target.watts == (265, 277)  # 1.05-1.10 x 252 W = 264.6-277.2
    assert work.target.provisional
    assert work.target.text() == "265-277 W*"


def test_pct_curve_resolves_half_up(plan, athlete):
    grind = day(plan, athlete, D(2026, 10, 27)).sessions[1]
    work = step(grind, 1, 0)
    assert work.target.anchor == "pct_curve"
    assert work.target.watts == (400, 400)  # 0.85 x 470 W = 399.5


def test_rpe_and_hr_zone_stay_unresolved(plan_data, athlete):
    plan_data["sessions"]["z2_60"]["steps"][1]["target"] = {"hr_zone": 2, "cadence": [85, 95]}
    plan = validate_plan(plan_data, athlete)
    z2 = day(plan, athlete, D(2026, 10, 28)).sessions[1]
    assert step(z2, 1).target.watts is None
    assert step(z2, 1).target.text() == "HR Z2 @ 85-95 rpm"
    grind = day(plan, athlete, D(2026, 10, 27)).sessions[1]
    assert step(grind, 0).target.text() == "RPE 4"


def test_watts_anchor_passes_through(plan_data, athlete):
    plan_data["sessions"]["grinding_repeats_8"]["steps"][1]["steps"][0]["target"] = {"watts": [380, 420]}
    plan = validate_plan(plan_data, athlete)
    grind = day(plan, athlete, D(2026, 10, 27)).sessions[1]
    assert step(grind, 1, 0).target.watts == (380, 420)


def test_durations_include_repeats(plan, athlete):
    threshold = day(plan, athlete, D(2026, 10, 30)).sessions[1]
    assert threshold.session_id == "bike_threshold_2x20"
    assert threshold.duration_s == 900 + 2 * (1200 + 300) + 600
    assert step(threshold, 1).duration_s == 3000
    strength = day(plan, athlete, D(2026, 10, 30)).sessions[0]
    assert strength.duration_s is None and strength.steps == ()


# ------------------------------------------------------------------ weekly TSS


def test_weekly_tss_sums_sessions(plan, athlete):
    days = expand(plan, athlete, D(2026, 10, 26), D(2026, 11, 1))
    (acc1,) = week_summaries(plan, days)
    assert acc1.planned_tss == sum(s.tss for d in days for s in d.sessions) == 693
    assert acc1.verdict == "ok"


def test_designed_weeks_sit_inside_their_tss_targets(plan, athlete):
    summaries = week_summaries(plan, expand(plan, athlete, plan.valid_from, plan.valid_to))
    assert [w.week.label for w in summaries] == ["re-entry", "test-week", "acc1", "acc2", "acc3", "acc4"]
    assert {w.week.label: w.verdict for w in summaries} == {w.week.label: "ok" for w in summaries}


# ------------------------------------------------------------------ CLI


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "pipeline.plan.expand", *map(str, args)],
        cwd=ROOT, capture_output=True, text=True,
    )


def test_cli_week(tmp_path):
    out = tmp_path / "schedule.csv"
    r = run_cli("config/plan.yaml", "--from", "2026-10-26", "--days", "7", "--csv", out)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Mon 2026-10-26  block1 Accumulation / acc1" in r.stdout
    assert "5 x [work 4:00 265-277 W*, rest 3:00 113-139 W*]" in r.stdout
    assert "acc1       26 Oct-01 Nov    693 TSS  target 650-750  ok" in r.stdout
    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 13  # Mon 2, Tue 3, Wed-Fri 2 each, Sat 1, Sun 1
    assert rows[0]["session_id"] == "upper_me" and rows[0]["date"] == "2026-10-26"
    assert rows[1]["duration_min"] == "90.0" and rows[1]["tss"] == "70"


def test_cli_outside_plan():
    r = run_cli("config/plan.yaml", "--from", "2027-06-01")
    assert r.returncode == 0
    assert "no plan days" in r.stdout


def test_cli_refuses_invalid_plan(tmp_path, plan_data):
    import yaml

    plan_data["sessions"]["z2_60"]["modality"] = "cycling"
    (tmp_path / "plan.yaml").write_text(yaml.safe_dump(plan_data, sort_keys=False))
    r = run_cli(tmp_path / "plan.yaml", "--athlete", ROOT / "config" / "athlete.yaml")
    assert r.returncode == 1
    assert "sessions.z2_60.modality" in r.stdout
