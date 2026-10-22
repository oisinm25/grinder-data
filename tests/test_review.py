import csv
import datetime as dt
import json

import httpx
import pytest

from pipeline.plan.expand import expand
from pipeline.plan.schema import validate_plan
from pipeline.prescribe import LogRow, Prescription
from pipeline.review.__main__ import last_sunday, main
from pipeline.review.compliance import Grade, grade_endurance, grade_strength, match_activities, power_steps
from pipeline.review.drift import review_week, strength_line_at
from tests.conftest import ROOT

D = dt.date
MON, FRI, SAT, SUN = D(2026, 10, 26), D(2026, 10, 30), D(2026, 10, 31), D(2026, 11, 1)
FAKE_ENV = {"INTERVALS_ICU_API_KEY": "fake-key-" + "0123456789abcdef", "INTERVALS_ICU_ATHLETE_ID": "i99999"}


@pytest.fixture
def plan(plan_data, athlete):
    return validate_plan(plan_data, athlete)


def sess(plan, athlete, day, sid):
    return next(s for d in expand(plan, athlete, day, day) for s in d.sessions if s.session_id == sid)


def act(id, day, sport, moving, avg="", start="07:00"):
    return {"id": id, "date": day.isoformat(), "start_local": f"{day}T{start}:00", "sport": sport, "moving_min": str(moving), "avg_watts": str(avg), "load": "50"}


WORK = lambda *w: [{"type": "WORK", "average_watts": x} for x in w]  # noqa: E731


# ------------------------------------------------------------------ endurance


def test_power_steps_unroll_repeats(plan, athlete):
    assert [s.target.watts for s in power_steps(sess(plan, athlete, FRI, "bike_threshold_2x20").steps)] == [(276, 285)] * 2
    assert len(power_steps(sess(plan, athlete, D(2026, 10, 27), "handbike_intervals").steps)) == 5


@pytest.mark.parametrize(
    ("work", "grade"),
    [
        ((280, 281), "held"),
        ((268, 280), "held"),  # 268 >= 276 x 0.97 = 267.7
        ((280, 300), "off_target"),  # 300 > 285 x 1.03 = 293.6
        ((280, 255), "faded"),  # last rep < 95% of 276 = 262.2
    ],
)
def test_threshold_grades(plan, athlete, work, grade):
    s = sess(plan, athlete, FRI, "bike_threshold_2x20")
    g = grade_endurance(s, act("a1", FRI, "bike", 75), WORK(*work))
    assert g.grade == grade
    assert len(g.ratios) == 2


def test_faded_note_names_the_rep(plan, athlete):
    g = grade_endurance(sess(plan, athlete, FRI, "bike_threshold_2x20"), act("a1", FRI, "bike", 75), WORK(280, 255))
    assert g.note.startswith("last rep 255 W < 95% of 276 W")


def test_steps_held_pct_allows_a_miss(plan, athlete):
    # handbike_intervals has steps_held_pct 0.9: 4 of 5 is 80%, so not enough; 5 of 5 holds
    s = sess(plan, athlete, D(2026, 10, 27), "handbike_intervals")
    assert grade_endurance(s, act("a", s.date, "handbike", 50), WORK(270, 270, 270, 270, 290)).grade == "off_target"
    assert grade_endurance(s, act("a", s.date, "handbike", 50), WORK(270, 270, 270, 270, 270)).grade == "held"


def test_skipped_missed_done(plan, athlete):
    z2 = sess(plan, athlete, MON, "z2_90")
    assert grade_endurance(z2, None, None).grade == "skipped"
    assert grade_endurance(z2, act("a", MON, "bike", 40), None).grade == "missed"  # under half of 90 min
    g = grade_endurance(z2, act("a", MON, "bike", 88), None)
    assert (g.grade, g.note) == ("done", "no interval data to grade power")
    test = sess(plan, athlete, D(2026, 10, 22), "test_grinding")  # all RPE steps
    assert grade_endurance(test, act("a", test.date, "grinding", 35), []).note == "no power targets to grade"


def test_single_steady_block_uses_ride_average(plan, athlete):
    z2 = sess(plan, athlete, MON, "z2_90")
    assert grade_endurance(z2, act("a", MON, "bike", 90, avg=205), []).grade == "held"
    assert grade_endurance(z2, act("a", MON, "bike", 90, avg=170), []).grade == "off_target"


def test_matching_by_day_sport_and_order(plan, athlete):
    planned = [sess(plan, athlete, D(2026, 10, 27), "handbike_intervals"), sess(plan, athlete, D(2026, 10, 27), "grinding_repeats_8")]
    acts = [act("g", D(2026, 10, 27), "grinding", 39, start="08:00"), act("h", D(2026, 10, 27), "handbike", 50, start="07:00"),
            act("x", D(2026, 10, 28), "grinding", 39)]  # fmt: skip
    m = match_activities(planned, acts)
    assert (m[0]["id"], m[1]["id"]) == ("h", "g")


def test_strength_session_matches_conditioning_too(plan, athlete):
    m = match_activities([sess(plan, athlete, MON, "upper_me")], [act("w", MON, "conditioning", 60)])
    assert m[0]["id"] == "w"


# ------------------------------------------------------------------ strength


def rx(kg=105, reps=6, cap=8, role="top", sets=1):
    return Prescription(MON, "upper_me", "mon_am", "bench_press", role, sets, reps, kg, cap, "e1rm", "")


def logged(kg, reps, rpe, role="top", n=1):
    return [LogRow(MON, "upper_me", "bench_press", role, kg, reps, rpe, i + 1) for i in range(n)]


@pytest.mark.parametrize(
    ("p", "rows", "grade", "note"),
    [
        (rx(), logged(105, 6, 8), "held", ""),
        (rx(), logged(107.5, 6, 7.5), "held", ""),
        (rx(), logged(102.5, 6, 8), "under", "2.5 kg lighter"),
        (rx(), logged(105, 5, 8), "under", "reps under 6"),
        (rx(sets=3, role="backoff", cap=None), logged(90, 8, None, "backoff", n=2), "under", "2 of 3 sets"),
        (rx(), logged(105, 6, 9), "over_rpe", "above RPE 8"),
        (rx(), logged(105, 6, None), "held", "RPE not logged"),
        (rx(kg=None), logged(60, 6, 8), "logged", "first weight logged"),
        (rx(role="amrap", reps=20, cap=None), logged(100, 8, 8.5, "amrap"), "logged", "test result"),
        (rx(), [], "skipped", "not logged"),
    ],
)
def test_strength_grades(p, rows, grade, note):
    g = grade_strength(p, rows)
    assert g.grade == grade and note in g.note


# ------------------------------------------------------------------ drift


def test_strength_line_interpolation(plan):
    assert strength_line_at(plan, "bench_press", D(2026, 10, 19)) == (127.0, D(2026, 10, 19))
    value, checkpoint = strength_line_at(plan, "bench_press", D(2026, 11, 8))  # 19 Oct 127 -> 29 Nov 132.5
    assert value == pytest.approx(127 + 20 / 41 * 5.5) and checkpoint is None
    assert strength_line_at(plan, "bench_press", D(2026, 11, 29))[1] == D(2026, 11, 29)
    assert strength_line_at(plan, "trap_bar_deadlift", MON) is None


def wellness_week(start, states, hrv=44, rhr=57):
    return [{"date": (start + dt.timedelta(days=i)).isoformat(), "readiness": s, "hrv": str(hrv), "resting_hr": str(rhr),
             "ctl": "30", "atl": "32"} for i, s in enumerate(states)]  # fmt: skip


def week(plan, grades=(), acts=(), wellness=(), logged_rows=(), previous=(), graded_to=None, start=MON, end=SUN):
    return review_week(plan, start, end, 693, list(grades), list(acts), list(wellness), list(logged_rows), list(previous), graded_to)


def test_quiet_week_has_no_flags(plan):
    acts = [act(f"a{i}", MON + dt.timedelta(days=i), "bike", 60) for i in range(7)]
    for a in acts:
        a["load"] = "100"
    base = wellness_week(MON - dt.timedelta(days=28), ["green"] * 28)
    w = week(plan, acts=acts, wellness=base + wellness_week(MON, ["green"] * 7),
             logged_rows=[LogRow(D(2026, 10, 19), "test_bench", "bench_press", "amrap", 100, 8, 8.5)])  # fmt: skip
    assert w.flags == []
    assert (w.record["actual_tss"], w.record["tss_target"], w.record["green"]) == ("700", "650-750", "7")
    assert w.record["bench_e1rm"] == "131.7"


def test_load_flag_only_once_the_week_is_over(plan):
    assert any(f.startswith("load: 0 TSS done, under the 650-750 target (advisory") for f in week(plan).flags)
    assert not any(f.startswith("load") for f in week(plan, graded_to=FRI).flags)
    assert week(plan, graded_to=FRI).record["no_call"] == "5"  # Mon-Fri, nothing measured


def test_recovery_flags(plan):
    w = week(plan, wellness=wellness_week(MON, ["red", "amber", "amber", "amber", "red", "green", "green"]))
    assert "recovery: 2 red days" in w.flags and "recovery: 3 amber days" in w.flags
    base = wellness_week(MON - dt.timedelta(days=28), [""] * 28, hrv=50, rhr=55)
    w = week(plan, wellness=base + wellness_week(MON, [""] * 7, hrv=45, rhr=59))
    assert any(f.startswith("recovery: 7-day HRV 45 is 10% under") for f in w.flags)
    assert any(f.startswith("recovery: 7-day resting HR 59 is 4 bpm over") for f in w.flags)


def test_session_flags(plan):
    g = lambda grade, sid="z2_60", note="": Grade(MON, "mon_pm", sid, "bike", "session", "", "", grade, note)  # noqa: E731
    w = week(plan, grades=[g("skipped"), g("missed"), g("faded", "bike_threshold_2x20", "last rep 255 W < 95% of 276 W; 1/2")])
    assert "sessions: 2 endurance sessions missed or skipped" in w.flags
    assert "sessions: bike_threshold_2x20 on Mon 26 Oct faded (last rep 255 W < 95% of 276 W)" in w.flags


def test_bench_checkpoint_missed_by_more_than_a_rep(plan):
    end = D(2026, 11, 29)  # checkpoint 132.5; one rep ~ 4.4 kg
    rows = [LogRow(D(2026, 11, 23), "upper_me", "bench_press", "top", 105, 6, 9)]  # e1RM 105 x 1.233 = 129.5 -> fine
    assert not any(f.startswith("bench") for f in week(plan, logged_rows=rows, start=end - dt.timedelta(days=6), end=end).flags)
    rows = [LogRow(D(2026, 11, 23), "upper_me", "bench_press", "top", 100, 6, 9)]  # 123.3: 9.2 kg under
    flags = week(plan, logged_rows=rows, start=end - dt.timedelta(days=6), end=end).flags
    assert any(f.startswith("bench: checkpoint 29 Nov missed by more than one rep") and "cut rule" in f for f in flags)


def test_bench_behind_the_line_between_checkpoints(plan):
    rows = [LogRow(D(2026, 10, 27), "upper_me", "bench_press", "top", 95, 6, 9)]  # 117.2 vs ~127.6
    assert any(f.startswith("bench: e1RM 117.2 is") for f in week(plan, logged_rows=rows).flags)


def test_cut_rule_two_weeks_of_falling_power_and_rising_fatigue(plan):
    previous = [
        {"week_end": "2026-10-18", "power_index": "1.02", "ctl": "30", "atl": "28"},
        {"week_end": "2026-10-25", "power_index": "0.99", "ctl": "30", "atl": "31"},
    ]
    falling = [Grade(FRI, "fri_pm", "bike_threshold_2x20", "bike", "session", "", "", "off_target", ratios=(0.95, 0.94))]
    wellness = wellness_week(MON, ["green"] * 7)  # ATL 32 - CTL 30 = 2 > 1
    w = week(plan, grades=falling, wellness=wellness, previous=previous)
    assert any(f.startswith("cut rule: power falling and fatigue rising") for f in w.flags)
    steady = [Grade(FRI, "fri_pm", "bike_threshold_2x20", "bike", "session", "", "", "held", ratios=(1.0, 1.01))]
    assert not any(f.startswith("cut rule") for f in week(plan, grades=steady, wellness=wellness, previous=previous).flags)


# ------------------------------------------------------------------ CLI


def test_last_sunday():
    assert last_sunday(D(2026, 11, 1)) == D(2026, 11, 1)
    assert last_sunday(D(2026, 11, 4)) == D(2026, 11, 1)


def write(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def derived(tmp_path):
    d = tmp_path / "derived"
    d.mkdir()
    acts = [
        act("i1", MON, "bike", 90, avg=205, start="17:30"),
        act("i2", FRI, "bike", 76, start="17:30"),
        act("i3", MON, "strength", 70),
    ]
    write(d / "activities.csv", acts, list(acts[0]))
    strength = [
        {"date": "2026-10-19", "session": "test_bench", "lift": "bench_press", "role": "amrap", "set_no": "1", "kg": "100", "reps": "8", "rpe": "8.5"},
        {"date": "2026-10-26", "session": "upper_me", "lift": "bench_press", "role": "top", "set_no": "1", "kg": "105", "reps": "6", "rpe": "8"},
    ]  # fmt: skip
    write(d / "strength.csv", strength, list(strength[0]))
    write(d / "wellness.csv", wellness_week(MON, ["green"] * 7), ["date", "readiness", "hrv", "resting_hr", "ctl", "atl"])
    return d


class FakeIntervals:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        self.calls.append(request.url.path)
        if request.url.path.endswith("/i2/intervals"):
            return httpx.Response(200, json={"icu_intervals": WORK(280, 255)})
        return httpx.Response(200, json={"icu_intervals": []})


def test_cli_week(derived, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    fake = FakeIntervals()
    code = main(["--week-ending", "2026-11-01", "--derived", str(derived), "--cache", str(tmp_path / "cache")],
                env=FAKE_ENV, transport=httpx.MockTransport(fake), today=D(2026, 11, 1))  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Week 2026-10-26 to 2026-11-01  block1 / acc1" in out
    assert "Load: 693 TSS planned, 150 done (target 650-750)" in out
    assert "faded      Fri 30 Oct  bike_threshold_2x20: last rep 255 W" in out
    assert "Bench: e1RM 133" in out  # latest top set: 105 x 6 @8 on 26 Oct
    assert "/api/v1/activity/i2/intervals" in fake.calls
    with open(derived / "compliance.csv", newline="", encoding="utf-8") as f:
        grades = {(r["session"], r["item"]): r["grade"] for r in csv.DictReader(f)}
    assert grades[("z2_90", "session")] == "held"  # ride average 205 W inside 195-216
    assert grades[("upper_me", "bench_press top")] == "held"
    assert grades[("long_ride_3h", "session")] == "skipped"
    rerun = main(["--week-ending", "2026-11-01", "--derived", str(derived), "--cache", str(tmp_path / "cache"), "--offline"],
                 env=FAKE_ENV, transport=httpx.MockTransport(fake), today=D(2026, 11, 1))  # fmt: skip
    assert rerun == 0
    with open(derived / "weekly.csv", newline="", encoding="utf-8") as f:
        assert [r["week_end"] for r in csv.DictReader(f)] == ["2026-11-01"]  # re-run replaces the week


def test_cli_today_is_pending_not_skipped(derived, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert main(["--week-ending", "2026-11-01", "--derived", str(derived), "--no-intervals"], env={}, today=SAT) == 0
    out = capsys.readouterr().out
    assert "(graded to 2026-10-31)" in out and "pending 1" in out
    with open(derived / "compliance.csv", newline="", encoding="utf-8") as f:
        sat = [r for r in csv.DictReader(f) if r["date"] == "2026-10-31"]
    assert [(r["session"], r["grade"]) for r in sat] == [("long_ride_3h", "pending")]


def test_cli_without_intervals_needs_no_secret(derived, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    code = main(["--week-ending", "2026-11-01", "--derived", str(derived), "--no-intervals"], env={}, today=D(2026, 11, 1))
    assert code == 0 and "done" in capsys.readouterr().out


def test_cli_missing_secret_suggests_no_intervals(derived, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    code = main(["--week-ending", "2026-11-01", "--derived", str(derived)], env={}, today=D(2026, 11, 1))
    assert code == 2 and "--no-intervals" in capsys.readouterr().err


def test_cli_rejects_non_sunday_and_future_weeks(derived, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    with pytest.raises(SystemExit):
        main(["--week-ending", "2026-10-31", "--derived", str(derived)], env={})
    assert main(["--week-ending", "2026-11-08", "--derived", str(derived)], env={}, today=D(2026, 11, 1)) == 0
    assert "hasn't started yet" in capsys.readouterr().out
