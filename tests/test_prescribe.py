import csv
import datetime as dt

import pytest

from pipeline.ingest.strength import e1rm
from pipeline.plan.schema import validate_plan
from pipeline.prescribe import FIELDS, LogRow, load_history, main, prescribe, round_to, rpe_weight, write_prescriptions

D = dt.date
MON1, WED1, THU1, FRI1 = D(2026, 10, 26), D(2026, 10, 28), D(2026, 10, 29), D(2026, 10, 30)
MON2, THU2, FRI2 = D(2026, 11, 2), D(2026, 11, 5), D(2026, 11, 6)
TEST_BENCH = LogRow(D(2026, 10, 19), "test_bench", "bench_press", "amrap", 100, 8, 8.5)
TEST_LOWER = LogRow(D(2026, 10, 21), "test_lower", "trap_bar_deadlift", "test", 180, 6, None)


@pytest.fixture
def plan(plan_data, athlete):
    return validate_plan(plan_data, athlete)


@pytest.fixture
def no_start(plan_data, athlete):
    """The plan with every start_kg removed, to exercise the e1RM basis."""
    for s in plan_data["sessions"].values():
        for ex in s.get("exercises", []):
            ex.pop("start_kg", None)
    return validate_plan(plan_data, athlete)


def rx(plan, athlete, history, day, readiness=None, session=None):
    """Prescriptions for one day, keyed (lift, role)."""
    items = prescribe(plan, athlete, history, readiness or {}, day, day)
    return {(p.lift, p.role): p for p in items if session is None or p.session == session}


def sets(day, session, lift, role, kg, reps_list, rpe):
    return [LogRow(day, session, lift, role, kg, r, rpe, i + 1) for i, r in enumerate(reps_list)]


# ------------------------------------------------------------------ formulas


def test_e1rm_formula():
    assert e1rm(100, 8, 8.5) == pytest.approx(131.667, abs=1e-3)  # 100 x (1 + (8 + 1.5)/30)
    assert e1rm(180, 6, None) == pytest.approx(216.0)  # missing RPE counts as 10
    assert rpe_weight(e1rm(100, 8, 8.5), 6, 8) == pytest.approx(103.95, abs=0.01)


def test_rounding():
    assert [round_to(x, 2.5) for x in (103.95, 101.24, 101.25, 72.42, 89.25)] == [105, 100, 102.5, 72.5, 90]


# ------------------------------------------------------------------ linear


def test_linear_starts_at_start_kg_with_backoff(plan, athlete):
    p = rx(plan, athlete, [TEST_BENCH], MON1)
    top, back = p[("bench_press", "top")], p[("bench_press", "backoff")]
    assert (top.kg, top.reps, top.rpe_cap, top.basis) == (100, 6, 8, "start")  # start_kg beats the e1RM
    assert top.reason.startswith("start_kg 100 x 6: no upper_me history in this slot within 14 days")
    assert (back.kg, back.sets, back.reps, back.basis) == (85, 3, 8, "top")  # 0.85 x 100
    assert back.reason == "85% of top 100 -> 85 x 8"


def test_linear_from_e1rm_without_start_kg(no_start, athlete):
    p = rx(no_start, athlete, [TEST_BENCH], MON1)
    top, back = p[("bench_press", "top")], p[("bench_press", "backoff")]
    assert (top.kg, top.basis) == (105, "e1rm")
    assert top.reason.startswith("e1RM 132 from 100x8 @8.5 on 19 Oct -> 105 x 6 @RPE 8")
    assert back.kg == 90  # 0.85 x 105 = 89.25


def test_test_session_in_same_slot_is_not_a_last_basis(plan, athlete):
    # test_bench ran in mon_am on 19 Oct, but it's a different session from upper_me
    assert rx(plan, athlete, [TEST_BENCH], MON1)[("bench_press", "top")].basis == "start"


def test_basis_order(plan, no_start, athlete):
    hist = [TEST_BENCH, *sets(MON1, "upper_me", "bench_press", "top", 102.5, [6], 8)]
    assert rx(plan, athlete, hist, MON2)[("bench_press", "top")].basis == "last"  # 1. last beats start_kg
    assert rx(plan, athlete, [TEST_BENCH], MON1)[("bench_press", "top")].basis == "start"  # 2. start beats e1RM
    assert rx(no_start, athlete, [TEST_BENCH], MON1)[("bench_press", "top")].basis == "e1rm"  # 3. e1RM
    assert rx(no_start, athlete, [], MON1)[("bench_press", "top")].basis == "choose"  # 4. choose


def test_linear_hit_adds_increment(plan, athlete):
    hist = [TEST_BENCH, *sets(MON1, "upper_me", "bench_press", "top", 105, [6], 8)]
    top = rx(plan, athlete, hist, MON2)[("bench_press", "top")]
    assert (top.kg, top.basis) == (107.5, "last")
    assert "every rep at or below RPE 8 -> +2.5 = 107.5 x 6" in top.reason
    assert rx(plan, athlete, hist, MON2)[("bench_press", "backoff")].kg == 92.5  # 0.85 x 107.5 = 91.4


@pytest.mark.parametrize(
    ("reps", "rpe", "why"),
    [(6, 8.5, "above RPE 8"), (5, 8, "under 6 reps"), (6, None, "RPE not logged, treated as 10")],
)
def test_linear_miss_repeats(plan, athlete, reps, rpe, why):
    hist = [TEST_BENCH, *sets(MON1, "upper_me", "bench_press", "top", 105, [reps], rpe)]
    top = rx(plan, athlete, hist, MON2)[("bench_press", "top")]
    assert (top.kg, top.basis) == (105, "last")
    assert f"miss ({why}) -> repeat 105 x 6" in top.reason


def test_linear_resets_after_two_misses(plan, athlete):
    hist = [
        TEST_BENCH,
        *sets(MON1, "upper_me", "bench_press", "top", 105, [6], 9),
        *sets(MON2, "upper_me", "bench_press", "top", 105, [6], 9),
    ]
    top = rx(plan, athlete, hist, D(2026, 11, 9))[("bench_press", "top")]
    assert top.kg == 100  # 105 x 0.95 = 99.75
    assert "2 misses in a row -> reset -5% = 100 x 6" in top.reason


def test_last_basis_only_within_14_days(plan, no_start, athlete):
    hist = [TEST_BENCH, *sets(MON1, "upper_me", "bench_press", "top", 105, [6], 8)]
    later = D(2026, 11, 16)  # 21 days later
    assert rx(plan, athlete, hist, later)[("bench_press", "top")].basis == "start"
    top = rx(no_start, athlete, hist, later)[("bench_press", "top")]
    assert top.basis == "e1rm" and "from 105x6 @8 on 26 Oct" in top.reason  # latest e1RM is that set


# ------------------------------------------------------------------ double


def test_double_from_start_kg_at_bottom_of_range(plan, athlete):
    p = rx(plan, athlete, [TEST_BENCH], THU1)[("bench_press", "work")]
    assert (p.kg, p.sets, p.reps, p.rpe_cap, p.basis) == (80, 3, 10, 8.5, "start")


def test_double_from_e1rm_starts_at_bottom_of_range(no_start, athlete):
    p = rx(no_start, athlete, [TEST_BENCH], THU1)[("bench_press", "work")]
    assert (p.kg, p.reps, p.rpe_cap, p.basis) == (95, 10, 8.5, "e1rm")  # 131.7 / (1 + 11.5/30) = 95.2


def test_double_adds_a_rep_to_the_weakest_set(plan, athlete):
    hist = [TEST_BENCH, *sets(THU1, "upper_re", "bench_press", "work", 95, [11, 10, 9], 8.5)]
    p = rx(plan, athlete, hist, THU2)[("bench_press", "work")]
    assert (p.kg, p.reps, p.basis) == (95, 10, "last")
    assert "last 95 x 11,10,9 on 29 Oct: weakest 9 -> same weight, aim 10" in p.reason


def test_double_steps_up_when_every_set_reaches_the_top(plan, athlete):
    hist = [TEST_BENCH, *sets(THU1, "upper_re", "bench_press", "work", 95, [12, 12, 12], 8.5)]
    p = rx(plan, athlete, hist, THU2)[("bench_press", "work")]
    assert (p.kg, p.reps) == (97.5, 10)
    assert "every set reached 12 -> +2.5 = 97.5 x 10" in p.reason


def test_double_with_missing_sets_does_not_step_up(plan, athlete):
    hist = [TEST_BENCH, *sets(THU1, "upper_re", "bench_press", "work", 95, [12, 12], 8.5)]
    p = rx(plan, athlete, hist, THU2)[("bench_press", "work")]
    assert (p.kg, p.reps) == (95, 12) and "(2 of 3 sets logged)" in p.reason


# ------------------------------------------------------------------ wave


@pytest.mark.parametrize(
    ("day", "kg", "week"),
    [
        (FRI1, 72.5, "wave week 1 of 3: 55% of e1RM 132 (from"),  # 0.55 x 131.7 = 72.4
        (FRI2, 80, "wave week 2 of 3: 60%"),  # 79.0
        (D(2026, 11, 13), 85, "wave week 3 of 3: 65%"),  # 85.6
        (D(2026, 11, 20), 75, "wave week 1 of 3: 55% of e1RM 132 + 2.5 after 1 cycle(s)"),  # 0.55 x 134.2 = 73.8
    ],
)
def test_wave_cycles_by_week(plan, athlete, day, kg, week):
    p = rx(plan, athlete, [TEST_BENCH], day, session="upper_de")[("bench_press", "speed")]
    assert (p.kg, p.basis) == (kg, "e1rm")
    assert p.reason.startswith(week)


def test_wave_start_kg_sets_only_the_first_session(plan_data, athlete):
    plan_data["sessions"]["upper_de"]["exercises"][1]["start_kg"] = 70
    plan = validate_plan(plan_data, athlete)
    first = rx(plan, athlete, [TEST_BENCH], FRI1, session="upper_de")[("bench_press", "speed")]
    assert (first.kg, first.basis) == (70, "start")
    hist = [TEST_BENCH, *sets(FRI1, "upper_de", "bench_press", "speed", 70, [3] * 8, None)]
    second = rx(plan, athlete, hist, FRI2, session="upper_de")[("bench_press", "speed")]
    assert (second.kg, second.basis) == (80, "e1rm")  # week 2: 60% of e1RM 131.7, not start_kg again


def test_wave_uses_e1rm_from_before_the_block(plan, athlete):
    later = sets(MON1, "upper_me", "bench_press", "top", 115, [6], 8)  # a big top set inside the block
    p = rx(plan, athlete, [TEST_BENCH, *later], FRI1, session="upper_de")[("bench_press", "speed")]
    assert p.kg == 72.5 and "from 100x8 @8.5 on 19 Oct" in p.reason


# ------------------------------------------------------------------ rpe and pct_e1rm


def test_rpe_rule_from_last_same_session_sets(plan, athlete):
    hist = sets(FRI1, "upper_de", "bench_pull", "speed", 60, [3] * 6, 6)  # felt easier than RPE 7
    p = rx(plan, athlete, hist, FRI2)[("bench_pull", "speed")]
    # e1RM 60 x (1 + 7/30) = 74 -> at RPE 7 for 3: 74 / 1.2 = 61.7 -> 62.5
    assert (p.kg, p.rpe_cap, p.basis) == (62.5, 7, "last")
    assert p.reason.startswith("e1RM 74 from last 60x3 @6 on 30 Oct -> 62.5 x 3 @RPE 7")


def test_rpe_rule_from_e1rm_of_another_session(plan, athlete):
    hist = sets(THU1, "upper_re", "bench_pull", "work", 50, [10, 10, 10], 8)
    p = rx(plan, athlete, hist, FRI1)[("bench_pull", "speed")]
    # e1RM 50 x (1 + 12/30) = 70 -> 70 / 1.2 = 58.3 -> 57.5
    assert (p.kg, p.basis) == (57.5, "e1rm")


def test_pct_e1rm_rule(plan_data, athlete):
    plan_data["progressions"]["power_rpe7"] = {"type": "pct_e1rm", "pct": 0.5}
    plan = validate_plan(plan_data, athlete)
    hist = [LogRow(D(2026, 10, 20), "upper_reentry", "push_press", "work", 60, 5, 8)]  # e1RM 60 x 1.233 = 74
    p = rx(plan, athlete, hist, FRI1)[("push_press", "speed")]
    assert (p.kg, p.basis) == (37.5, "e1rm")  # 0.5 x 74 = 37
    assert "-> 50% = 37.5 x 3" in p.reason


# ------------------------------------------------------------------ fallbacks


def test_start_kg_when_no_history(plan, athlete):
    p = rx(plan, athlete, [], THU1)[("bench_press", "work")]
    assert (p.kg, p.reps, p.basis) == (80, 10, "start")


def test_choose_when_nothing_is_known(plan, athlete):
    p = rx(plan, athlete, [], MON1)[("weighted_pullup", "work")]
    assert (p.kg, p.basis, p.reps) == (None, "choose", 5)
    assert p.reason.startswith("no history, e1RM or start_kg: choose a weight for 5 at RPE 8.5 and log it")


def test_backoff_without_a_top_weight(plan, athlete):
    p = rx(plan, athlete, [], WED1)[("trap_bar_deadlift", "backoff")]
    assert p.kg is None and "90% of whatever top set you choose" in p.reason


def test_missing_rpe_in_e1rm_basis(plan, athlete):
    top = rx(plan, athlete, [TEST_LOWER], WED1)[("trap_bar_deadlift", "top")]
    assert (top.kg, top.basis) == (175, "e1rm")  # 216 / (1 + 7/30) = 175.1
    assert top.reason.startswith("e1RM 216 from 180x6 (RPE not logged, treated as 10) on 21 Oct -> 175 x 5 @RPE 8")


def test_by_feel_lift_uses_e1rm_only_with_an_rpe_cap(plan, athlete):
    hist = [LogRow(D(2026, 10, 15), "upper_reentry", "bench_press", "work", 85, 8, 7)]
    p = rx(plan, athlete, hist, D(2026, 10, 19))
    assert (p[("bench_press", "amrap")].kg, p[("bench_press", "amrap")].basis) == (100, "start")  # no cap: start_kg
    row_hist = [LogRow(D(2026, 10, 15), "upper_reentry", "chest_supported_row", "work", 50, 10, 7)]
    row = rx(plan, athlete, row_hist, D(2026, 10, 19))[("chest_supported_row", "work")]
    assert row.basis == "e1rm" and row.rpe_cap == 7


def test_no_rule_repeats_last_same_session_weight(plan_data, athlete):
    plan_data["sessions"]["upper_me"]["exercises"][2] = {
        "lift": "chest_supported_row", "groups": [{"role": "work", "sets": 3, "reps": 10, "rest_s": 90}],
    }  # fmt: skip
    plan = validate_plan(plan_data, athlete)
    hist = sets(MON1, "upper_me", "chest_supported_row", "work", 50, [10, 10, 10], 7)
    p = rx(plan, athlete, hist, MON2)[("chest_supported_row", "work")]
    assert (p.kg, p.basis) == (50, "last")
    assert p.reason.startswith("no progression rule: repeat last 50x10 @7 on 26 Oct -> 50 x 10")


# ------------------------------------------------------------------ readiness


def test_amber_holds_last_weight(plan, athlete):
    hist = [TEST_BENCH, *sets(MON1, "upper_me", "bench_press", "top", 105, [6], 8)]
    p = rx(plan, athlete, hist, MON2, {MON2: "amber"})
    top = p[("bench_press", "top")]
    assert (top.kg, top.rpe_cap) == (105, 8)  # green would be 107.5
    assert "amber: hold last 105, no increment, RPE cap 8" in top.reason
    assert p[("bench_press", "backoff")].kg == 90  # from the held top


def test_amber_caps_rpe_at_8(plan, athlete):
    p = rx(plan, athlete, [TEST_BENCH], THU1, {THU1: "amber"})[("bench_press", "work")]
    assert (p.kg, p.rpe_cap) == (80, 8)  # start_kg held, cap from 8.5 down to 8


def test_red_is_90_percent_of_last(plan, athlete):
    hist = [TEST_BENCH, *sets(MON1, "upper_me", "bench_press", "top", 105, [6], 8)]
    p = rx(plan, athlete, hist, MON2, {MON2: "red"})
    top = p[("bench_press", "top")]
    assert (top.kg, top.rpe_cap) == (95, 7)  # 0.9 x 105 = 94.5
    assert "red: 90% of last 105 -> 95, RPE cap 7" in top.reason
    assert p[("bench_press", "backoff")].kg == 80  # 0.85 x 95 = 80.75


def test_red_without_last_takes_90_percent_of_suggestion(plan, athlete):
    top = rx(plan, athlete, [TEST_BENCH], MON1, {MON1: "red"})[("bench_press", "top")]
    assert top.kg == 90  # 0.9 x start_kg 100


def test_red_on_a_choose_day_only_caps_rpe(plan, athlete):
    p = rx(plan, athlete, [], MON1, {MON1: "red"})[("weighted_pullup", "work")]
    assert (p.kg, p.rpe_cap) == (None, 7)


def test_readiness_exempt_ignores_amber_but_not_red(plan, athlete):
    test_day = D(2026, 10, 19)
    amber = rx(plan, athlete, [], test_day, {test_day: "amber"})[("bench_press", "amrap")]
    assert amber.kg == 100 and "amber ignored (readiness-exempt test)" in amber.reason
    red = rx(plan, athlete, [], test_day, {test_day: "red"})[("bench_press", "amrap")]
    assert (red.kg, red.rpe_cap) == (90, 7)


def test_unknown_readiness_is_noted(plan, athlete):
    top = rx(plan, athlete, [TEST_BENCH], MON1)[("bench_press", "top")]
    assert top.readiness == "" and top.reason.endswith("readiness not known yet")


# ------------------------------------------------------------------ output and CLI


def test_week_covers_every_strength_session(plan, athlete):
    items = prescribe(plan, athlete, [TEST_BENCH, TEST_LOWER], {}, MON1, D(2026, 11, 1))
    assert sorted({(p.date, p.session) for p in items}) == [
        (MON1, "upper_me"), (WED1, "lower_me"), (THU1, "upper_re"), (FRI1, "upper_de"),
    ]  # fmt: skip
    assert len(items) == 13


def test_csv_columns_and_rerun(tmp_path, plan, athlete):
    out = tmp_path / "prescriptions.csv"
    items = prescribe(plan, athlete, [TEST_BENCH], {}, MON1, D(2026, 11, 1))
    assert write_prescriptions(out, items, MON1, D(2026, 11, 1)) == 13
    assert write_prescriptions(out, items, MON1, D(2026, 11, 1)) == 13  # no duplicates
    with open(out, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert tuple(reader.fieldnames) == FIELDS
    assert rows[0] == {
        "date": "2026-10-26", "session": "upper_me", "lift": "bench_press", "role": "top", "sets": "1",
        "reps": "6", "kg": "100", "rpe_cap": "8", "basis": "start",
        "reason": "start_kg 100 x 6: no upper_me history in this slot within 14 days; readiness not known yet",
    }  # fmt: skip


def test_load_history_reads_derived_strength(tmp_path):
    p = tmp_path / "strength.csv"
    p.write_text(
        "date,session,lift,role,set_no,kg,reps,rpe,e1rm_kg,notes,log_line\n"
        "2026-10-21,test_lower,trap_bar_deadlift,test,1,180,6,,216,,3\n",
        encoding="utf-8",
    )
    (row,) = load_history(p)
    assert (row.kg, row.reps, row.rpe, row.e1rm) == (180, 6, None, 216)


def test_cli(tmp_path, capsys, monkeypatch):
    from tests.conftest import ROOT

    monkeypatch.chdir(ROOT)
    strength = tmp_path / "strength.csv"
    strength.write_text(
        "date,session,lift,role,set_no,kg,reps,rpe,e1rm_kg,notes,log_line\n"
        "2026-10-19,test_bench,bench_press,amrap,1,100,8,8.5,131.7,,2\n",
        encoding="utf-8",
    )
    wellness = tmp_path / "wellness.csv"
    wellness.write_text("date,readiness,readiness_reason\n2026-10-26,amber,amber: HRV low\n", encoding="utf-8")
    out = tmp_path / "prescriptions.csv"
    code = main(["--from", "2026-10-26", "--days", "7", "--strength", str(strength), "--wellness", str(wellness), "--out", str(out)])
    text = capsys.readouterr().out
    assert code == 0
    assert "Mon 2026-10-26  upper_me (mon_am)  readiness: amber" in text
    assert "Thu 2026-10-29  upper_re (thu_am)  readiness: not known yet" in text
    assert "wrote 13 prescriptions" in text and out.exists()


def test_cli_without_ingest(tmp_path, capsys, monkeypatch):
    from tests.conftest import ROOT

    monkeypatch.chdir(ROOT)
    code = main(["--from", "2026-10-26", "--strength", str(tmp_path / "nope.csv")])
    assert code == 2 and "run python -m pipeline.ingest first" in capsys.readouterr().err
