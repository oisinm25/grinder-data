import datetime as dt

import pytest

from pipeline.dashboard import week
from pipeline.ingest.tidy import heavy_rows
from pipeline.ingest.zones import activity_zone_rows, icu_seconds, merge_zone_rows, stream_seconds
from pipeline.plan.schema import validate_plan

D = dt.date
TODAY = D(2026, 10, 26)  # Monday: block0 ends Sun 25 Oct, block1 starts today


@pytest.fixture
def plan(plan_data, athlete):
    return validate_plan(plan_data, athlete)


WELLNESS = [
    {"date": "2026-10-23", "readiness": "green", "readiness_reason": "green: HRV 39 >= 32.4 (baseline 43.4 - 1 SD 11.0); resting HR 56 <= 62.4 (baseline 57.4 + 5)"},
    {"date": "2026-10-24", "readiness": "green", "readiness_reason": "green: HRV 54 >= 32.0 (baseline 42.9 - 1 SD 10.9)"},
    {"date": "2026-10-26", "readiness": "red", "readiness_reason": "red: sleep 5.38 h < 6"},
    {"date": "2026-10-27", "readiness": "green", "readiness_reason": "green: should never show, it's tomorrow"},
]  # fmt: skip
COMPLIANCE = [
    {"date": "2026-10-23", "slot": "fri_pm", "session": "z2_45_openers", "item": "session", "actual": "29 min, activity i200663412", "grade": "done"},
    {"date": "2026-10-24", "slot": "sat_am", "session": "test_bike_20", "item": "session", "actual": "", "grade": "skipped"},
]  # fmt: skip
ACTIVITIES = [{"id": "i200663412", "date": "2026-10-23", "sport": "bike", "load": "13.14"}]


def outline(plan, athlete):
    return week.week_outline(TODAY, plan, athlete, WELLNESS, COMPLIANCE, ACTIVITIES)


# ------------------------------------------------------------------ week outline


def test_outline_covers_seven_days_across_a_block_change(plan, athlete):
    rows = outline(plan, athlete)
    assert sorted({r["date"] for r in rows}) == [f"2026-10-{d}" for d in range(23, 30)]
    blocks = {r["date"]: r["block"] for r in rows}
    assert (blocks["2026-10-25"], blocks["2026-10-26"]) == ("block0", "block1")
    assert {r["date"]: r["when"] for r in rows}["2026-10-26"] == "today"


def test_a_day_with_two_sessions(plan, athlete):
    tue = [r for r in outline(plan, athlete) if r["date"] == "2026-10-27"]
    assert [(r["slot"], r["summary"], r["planned_tss"]) for r in tue] == [
        ("AM", "Hand-bike 5x4 min", 48), ("AM", "Grinding repeat-sprints 8x1 min", 35), ("PM", "Mobility and trunk", None)]  # fmt: skip
    assert all(r["status"] == "planned" and r["grade"] is None for r in tue)


def test_future_day_has_no_readiness(plan, athlete):
    tue = [r for r in outline(plan, athlete) if r["date"] == "2026-10-27"]
    assert all(r["readiness"] is None and r["readiness_reason"] == "" for r in tue)


def test_past_and_today_readiness_and_grades(plan, athlete):
    rows = {(r["date"], r["session"]): r for r in outline(plan, athlete)}
    fri = rows[("2026-10-23", "z2_45_openers")]
    assert (fri["readiness"], fri["readiness_reason"]) == ("green", "HRV 39 >= 32.4; RHR 56 <= 62.4")
    assert (fri["grade"], fri["actual_tss"], fri["status"]) == ("done", 13, "done")
    sat = rows[("2026-10-24", "test_bike_20")]
    assert (sat["grade"], sat["actual_tss"], sat["summary"]) == ("missed", None, "Test: 20-min bike test on the power meter")
    mob = rows[("2026-10-23", "mobility_trunk")]
    assert (mob["grade"], mob["status"]) == (None, "not graded")
    sun = rows[("2026-10-25", None)]  # rest day: one row, no session
    assert (sun["summary"], sun["status"], sun["readiness"]) == ("Rest", "rest", None)  # no wellness row that day
    mon = rows[("2026-10-26", "upper_me")]
    assert (mon["readiness"], mon["grade"], mon["status"], mon["summary"]) == ("red", None, "planned", "Upper max effort")


@pytest.mark.parametrize(("grades", "word"), [
    (["held", "skipped"], "held"), (["skipped", "skipped"], "missed"), (["held", "under"], "off target"),
    (["on_target"], "held"), (["faded"], "faded"), (["logged"], "done"), ([], None)])  # fmt: skip
def test_session_grade(grades, word):
    assert week.session_grade([{"grade": g} for g in grades]) == word


def test_short_summary():
    assert week.short_summary("Threshold 2 x 20 min at 92-95% FTP, 5 min easy between") == "Threshold 2x20 min"
    assert week.short_summary("Z2 90 min") == "Z2 90 min"
    assert week.short_summary("Easy hand-bike 40 min (optional - rest is fine)") == "Easy hand-bike 40 min"


# ------------------------------------------------------------------ fitness over the week


def test_fitness_week_actual_then_projected():
    fitness = {"days": [{"date": f"2026-10-{d}", "ctl": 30.0 + d - 23, "atl": 40.0, "form": -10.0 + d - 23} for d in range(20, 27)],
               "planned": [{"date": "2026-10-27", "ctl": 34.0, "atl": 48.5}, {"date": "2026-10-28", "ctl": 35.0, "atl": 50.0}]}  # fmt: skip
    rows = week.fitness_week(TODAY, fitness)
    assert [(r["date"][-2:], r["kind"]) for r in rows] == [("23", "actual"), ("24", "actual"), ("25", "actual"), ("26", "actual"),
                                                           ("27", "projected"), ("28", "projected"), ("29", "projected")]  # fmt: skip
    assert (rows[4]["ctl"], rows[4]["form"]) == (34.0, -14.5)
    assert rows[6]["ctl"] is None  # beyond the projection


# ------------------------------------------------------------------ zones


def zone_rows(aid, day, sport, kind, secs, source="streams"):
    return [{"activity_id": aid, "date": day, "sport": sport, "kind": kind, "zone": str(z + 1), "seconds": str(s), "source": source}
            for z, s in enumerate(secs)]  # fmt: skip


def test_icu_seconds_drops_sweet_spot_and_reads_hr():
    raw = {"icu_zone_times": [{"id": f"Z{i}", "secs": i * 10} for i in range(1, 8)] + [{"id": "SS", "secs": 999}],
           "icu_hr_zone_times": [5, 4, 3, 2, 1, 0, 0]}  # fmt: skip
    assert icu_seconds(raw) == {"power": [10, 20, 30, 40, 50, 60, 70], "hr": [5, 4, 3, 2, 1, 0, 0]}


def test_activity_with_no_zone_data_has_no_rows_and_no_fetch(athlete):
    calls = []
    row = {"id": "i1", "date": "2026-10-23", "sport": "bike"}
    assert activity_zone_rows(row, {"icu_zone_times": None, "icu_hr_zone_times": None}, athlete, calls.append) == []
    assert calls == []


def test_streams_are_rezoned_with_athlete_anchors(athlete):
    # fixture athlete: bike FTP 300, LTHR 170. 10 s at 150 W (Z1 < 165), 10 s at 280 W (Z4 270-315), then a 60 s pause.
    t = list(range(0, 21)) + [81]
    w = [150] * 10 + [280] * 10 + [280, 0]
    hr = [130] * 10 + [165] * 10 + [165, 120]
    raw = {"icu_zone_times": [{"id": "Z1", "secs": 1}], "icu_hr_zone_times": [1, 0, 0, 0, 0, 0, 0]}
    rows = activity_zone_rows({"id": "i9", "date": "2026-10-25", "sport": "bike"}, raw, athlete,
                              lambda aid: {"time": t, "watts": w, "heartrate": hr})  # fmt: skip
    power = [int(r["seconds"]) for r in rows if r["kind"] == "power"]
    hrs = [int(r["seconds"]) for r in rows if r["kind"] == "hr"]
    assert power == [11, 0, 0, 11, 0, 0, 0]  # the pause counts 1 s; the last sample 1 s
    assert hrs == [11, 0, 0, 11, 0, 0, 0]  # 130 < 137.7 (81% LTHR); 165 in Z4 (159.8-170)
    assert {r["source"] for r in rows} == {"streams"}


def test_no_streams_keeps_intervals_seconds_and_no_power_for_strength(athlete):
    raw = {"icu_zone_times": [{"id": "Z2", "secs": 60}], "icu_hr_zone_times": [100, 50, 0, 0, 0, 0, 0]}
    ride = activity_zone_rows({"id": "i1", "date": "2026-10-23", "sport": "bike"}, raw, athlete, lambda aid: None)
    assert {(r["kind"], r["source"]) for r in ride} == {("power", "icu"), ("hr", "icu")}
    lift = activity_zone_rows({"id": "i2", "date": "2026-10-23", "sport": "strength"}, raw, athlete, lambda aid: None)
    assert {r["kind"] for r in lift} == {"hr"}


def test_stream_seconds_needs_samples():
    assert stream_seconds([0], [100], [1, 2, 3, 4, 5, 6]) is None


def test_merge_replaces_fetched_activities_only():
    old = zone_rows("i1", "2026-10-10", "bike", "hr", [1] * 7) + zone_rows("i2", "2026-10-20", "bike", "hr", [2] * 7)
    new = zone_rows("i3", "2026-10-21", "bike", "hr", [3] * 7)
    out = merge_zone_rows(old, new, {"i3"}, "2026-10-15", "2026-10-26")
    assert sorted({r["activity_id"] for r in out}) == ["i1", "i3"]  # i2 is in range but gone upstream


def test_zones_week_last_and_this_week():
    zones = (zone_rows("i1", "2026-10-22", "bike", "power", [3600, 1800, 0, 0, 0, 0, 0])
             + zone_rows("i2", "2026-10-25", "bike", "power", [0, 1800, 0, 0, 0, 0, 0])
             + zone_rows("i3", "2026-10-26", "bike", "hr", [600, 0, 0, 0, 0, 0, 0])
             + zone_rows("i0", "2026-10-18", "bike", "power", [9999] * 7))  # the week before: left out
    rows = week.zones_week(TODAY, zones)
    last = [r for r in rows if r["week"] == "last"]
    assert [(r["zone"], r["hours"], r["share"]) for r in last][:2] == [(1, 1.0, 0.5), (2, 1.0, 0.5)]
    assert {(r["week_start"], r["week_end"], r["activities"]) for r in last} == {("2026-10-19", "2026-10-25", 2)}
    this = [r for r in rows if r["week"] == "this"]
    assert {r["kind"] for r in this} == {"hr"} and len(this) == 7


# ------------------------------------------------------------------ heavy reps


def strength(day, lift, role, kg, reps, e1rm, line):
    return {"date": day, "lift": lift, "role": role, "kg": str(kg), "reps": str(reps), "e1rm_kg": str(e1rm), "log_line": str(line)}


def test_a_lift_with_no_earlier_e1rm_uses_its_own_set():
    (r,) = heavy_rows([strength("2026-10-19", "bench_press", "amrap", 100, 8, 131.7, 2)])
    assert (r["basis"], r["e1rm_basis_kg"], r["pct_of_e1rm"], r["heavy_reps"]) == ("own set", "131.7", "0.759", "0")


def test_heavy_reps_use_the_e1rm_as_it_stood_and_skip_backoffs():
    sets = [strength("2026-10-19", "bench_press", "amrap", 100, 8, 131.7, 2),
            strength("2026-10-26", "bench_press", "top", 120, 3, 132.0, 3),     # 120 / 131.7 = 91% -> 3 heavy reps
            strength("2026-10-26", "bench_press", "backoff", 120, 3, 132.0, 4),  # backoff: not counted
            strength("2026-10-26", "bench_press", "work", 110, 5, 128.3, 5)]     # 84%: counted set, no heavy reps
    rows = heavy_rows(sets)
    assert [(r["date"], r["role"], r["heavy_reps"], r["basis"]) for r in rows] == [
        ("2026-10-19", "amrap", "0", "own set"), ("2026-10-26", "top", "3", "earlier"), ("2026-10-26", "work", "0", "earlier")]  # fmt: skip
    wk = week.heavy_reps_week(dt.date(2026, 10, 27), rows)
    assert [(r["week"], r["lift"], r["heavy_reps"], r["heavy_sets"], r["sets"]) for r in wk] == [
        ("last", "bench_press", 0, 0, 1), ("this", "bench_press", 3, 1, 2)]  # fmt: skip
    assert wk[1]["heavy_detail"] == "10-26 120 x 3 (91%)"


def test_moved_and_vacated_slots(plan, athlete, monkeypatch):
    """Session moves (training/logs/changes.yaml) are optional to week.py: it reads moved_note and Day.vacated
    with getattr. This test needs pipeline.plan.changes to build them, so it skips without it."""
    import dataclasses

    changes = pytest.importorskip("pipeline.plan.changes")
    Pos, Vacated = changes.Pos, changes.Vacated
    from pipeline.plan.expand import expand

    def moved(p, a, start, end, **kw):
        out = []
        for d in expand(p, a, start, end, changes=()):
            if d.date == D(2026, 10, 26):  # upper_me leaves Monday AM ...
                d = dataclasses.replace(d, sessions=tuple(s for s in d.sessions if s.session_id != "upper_me"),
                                        vacated=(Vacated("mon_am", "upper_me", "moved", Pos(D(2026, 10, 27), "am"), "skipped Monday"),))  # fmt: skip
            if d.date == D(2026, 10, 27):  # ... for Tuesday AM
                upper = next(s for s in expand(p, a, D(2026, 10, 26), D(2026, 10, 26), changes=())[0].sessions if s.session_id == "upper_me")
                upper = dataclasses.replace(upper, date=d.date, slot="tue_am", order=2, moved_from=Pos(D(2026, 10, 26), "am"))
                d = dataclasses.replace(d, sessions=(*d.sessions, upper))
            out.append(d)
        return out

    monkeypatch.setattr(week, "expand", moved)
    rows = outline(plan, athlete)
    mon = [(r["slot"], r["session"], r["status"], r["note"]) for r in rows if r["date"] == "2026-10-26"]
    assert mon == [("AM", "upper_me", "moved", "moved to Tue AM: skipped Monday"), ("PM", "z2_90", "planned", "")]
    tue = [(r["slot"], r["session"], r["moved_note"]) for r in rows if r["date"] == "2026-10-27"]
    assert ("AM", "upper_me", "(moved from Mon)") in tue
