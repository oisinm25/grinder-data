import csv
import datetime as dt
import json

import pytest

from pipeline.brief import build as build_brief
from pipeline.brief import to_markdown as brief_md
from pipeline.plan.schema import validate_plan
from pipeline.review.debrief import build, drift_from_streams, grind_load, hr_efforts, to_markdown, write

D = dt.date
LTHR = 170  # athlete.yaml


@pytest.fixture
def plan(plan_data, athlete):
    return validate_plan(plan_data, athlete)


class FakeFetch:
    def __init__(self, detail=None, intervals=None, streams=None):
        self._detail, self._intervals, self._streams = detail or {}, intervals or {}, streams or {}
        self.problems = []

    def detail(self, aid):
        return self._detail.get(aid)

    def intervals(self, aid):
        return self._intervals.get(aid)

    def streams(self, aid):
        return self._streams.get(aid, {})


ACT_FIELDS = ["id", "date", "start_local", "sport", "mapped_by", "review", "type", "name", "moving_min", "load",
              "avg_watts", "np_watts", "avg_hr", "intensity"]  # fmt: skip


def write_rows(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


@pytest.fixture
def derived(tmp_path):
    d = tmp_path / "derived"
    d.mkdir()
    return d


def setup(derived, activities=(), sets=(), wellness=()):
    write_rows(derived / "activities.csv", ACT_FIELDS, list(activities))
    base = [{"date": "2026-10-19", "session": "test_bench", "lift": "bench_press", "role": "amrap", "set_no": 1, "kg": 100, "reps": 8, "rpe": 8.5}]
    write_rows(derived / "strength.csv", ["date", "session", "lift", "role", "set_no", "kg", "reps", "rpe"], base + list(sets))
    write_rows(derived / "wellness.csv", ["date", "readiness", "readiness_reason", "sleep_h"], list(wellness))


def act(id, day, sport, minutes, load=50, start="17:30", **kw):
    return {"id": id, "date": day.isoformat(), "start_local": f"{day}T{start}:00", "sport": sport, "type": kw.pop("type", "Ride"),
            "name": kw.pop("name", "Ride"), "moving_min": minutes, "load": load, **kw}  # fmt: skip


def work(*watts, hr=158):
    return [{"type": "WORK", "average_watts": w, "average_heartrate": hr, "moving_time": 60} for w in watts]


FRI, SAT = D(2026, 10, 30), D(2026, 10, 31)


# ------------------------------------------------------------------ bike


def test_held_threshold(plan, athlete, derived):
    setup(derived, [act("t1", FRI, "bike", 76, load=86)], wellness=[{"date": "2026-10-30", "readiness": "green", "readiness_reason": "green: fine", "sleep_h": "7.4"}])
    fetch = FakeFetch(detail={"t1": {"icu_weighted_avg_watts": 262, "icu_intensity": 87.3, "icu_training_load": 86}},
                      intervals={"t1": work(280, 281)})  # fmt: skip
    b = build(FRI, plan, athlete, derived, fetch)
    (item,) = [i for i in b["items"] if i["session"] == "bike_threshold_2x20"]
    assert (item["grade"], item["np_w"], item["intensity"], item["tss"]) == ("held", 262, 0.87, 86)
    assert [(r["n"], r["band"], r["watts"], r["status"]) for r in item["reps"]] == [(1, [276, 285], 280, "held"), (2, [276, 285], 281, "held")]
    assert "hr_drift_pct" not in item  # not a steady ride
    # Friday's speed-bench session wasn't logged, so the verdict says so
    assert b["verdict"] == "Threshold 2 x 20 min at 92-95% FTP, 5 min easy between done; Upper dynamic effort missed."
    assert b["context"] == {"readiness": "green", "readiness_reason": "green: fine", "sleep_h": 7.4, "load_done": 86, "load_planned": 125}


def test_faded_threshold_and_todays_note(plan, athlete, derived):
    setup(derived, [act("t1", FRI, "bike", 76, load=80)])
    b = build(FRI, plan, athlete, derived, FakeFetch(intervals={"t1": work(280, 255)}))
    item = next(i for i in b["items"] if i["session"] == "bike_threshold_2x20")
    assert item["grade"] == "faded" and item["reps"][1]["status"] == "under"
    assert b["verdict"] == "Threshold 2 x 20 min at 92-95% FTP, 5 min easy between faded in rep 2 (255 W vs 276 W)."
    # Saturday's long ride is easy, so: the roadmap's "today stays easy as planned"
    assert b["note_for_today"] == "Threshold 2 x 20 min at 92-95% FTP, 5 min easy between faded in rep 2: today stays easy as planned."
    md = to_markdown(b)
    assert "| 2 | 276-285 W | 255 | 158 | under |" in md and "**Threshold 2 x 20 min" in md


def test_steady_ride_hr_drift(plan, athlete, derived):
    setup(derived, [act("l1", SAT, "bike", 180, load=140, avg_watts=205, start="09:00")])
    b = build(SAT, plan, athlete, derived, FakeFetch(detail={"l1": {"decoupling": 7.24}}))
    item = b["items"][0]
    assert (item["grade"], item["hr_drift_pct"]) == ("held", 7.2)  # one steady block graded on the ride average
    assert b["note_for_today"] == "HR drift 7.2% on yesterday's ride: start the next long ride easier and fuel early."


def test_drift_from_streams_when_intervals_icu_has_none():
    steady = {"watts": [200] * 1200, "heartrate": [130] * 600 + [140] * 600}  # same power, HR up 10 bpm
    assert drift_from_streams(steady) == pytest.approx(round((200 / 130 - 200 / 140) / (200 / 130) * 100, 1))
    assert drift_from_streams({"watts": [200] * 100, "heartrate": [130] * 100}) is None  # too short


# ------------------------------------------------------------------ strength

MON, TUE = D(2026, 10, 26), D(2026, 10, 27)


def bench(kg, reps, rpe, role="top", n=1, day=MON):
    return [{"date": day.isoformat(), "session": "upper_me", "lift": "bench_press", "role": role, "set_no": i + 1,
             "kg": kg, "reps": reps, "rpe": rpe} for i in range(n)]  # fmt: skip


def test_strength_above_prescription(plan, athlete, derived):
    setup(derived, [act("s1", MON, "strength", 65, start="07:00", type="WeightTraining"), act("z1", MON, "bike", 90, avg_watts=205)],
          sets=bench(100, 6, 7.5) + bench(85, 8, "", role="backoff", n=3))  # fmt: skip
    b = build(MON, plan, athlete, derived, FakeFetch())
    item = next(i for i in b["items"] if i["kind"] == "strength")
    lift = item["lifts"][0]
    assert lift["name"] == "Bench press"
    assert [x["status"] for x in lift["sets"]] == ["hit", "hit", "hit", "hit"]
    assert lift["sets"][0]["prescribed"] == "1 x 6 @ 100 kg RPE<=8" and lift["sets"][0]["achieved"] == "100 x 6 @7.5"
    assert (lift["e1rm_before"], lift["e1rm_after"]) == (131.7, 128.3)  # 100 x (1 + (6 + 2.5)/30)
    assert lift["next"]["date"] == "2026-11-02" and (lift["next"]["kg"], lift["next"]["reps"]) == (102.5, 6)
    assert [x["status"] for x in item["lifts"][1]["sets"]] == ["not logged"]  # pull-ups: not logged, not held against you
    assert b["verdict"] == "Held the plan. Bench press moves up next time."
    assert b["note_for_today"] == "Bench press: 102.5 x 6 next Mon 2 Nov."
    md = to_markdown(b)
    assert "| Bench press | top 1 | 1 x 6 @ 100 kg RPE<=8 | 100 x 6 @7.5 | hit |" in md
    assert "- Bench press: e1RM 131.7 -> 128.3 (-3.4); next time 102.5 x 6 on Mon 2 Nov" in md


def test_strength_below_prescription(plan, athlete, derived):
    setup(derived, sets=bench(97.5, 6, 8) + bench(85, 8, 8, role="backoff", n=3))
    b = build(MON, plan, athlete, derived, FakeFetch())
    lift = next(i for i in b["items"] if i["kind"] == "strength")["lifts"][0]
    assert lift["sets"][0]["status"] == "under"
    assert b["verdict"] == "Bench press under prescription: 97.5 x 6 @8 vs 1 x 6 @ 100 kg RPE<=8."
    assert lift["next"]["kg"] == 100  # linear goes by what was lifted: 97.5 x 6 @8 hit every rep, so +2.5


def test_strength_over_rpe_is_flagged(plan, athlete, derived):
    setup(derived, sets=bench(100, 6, 9))
    lift = next(i for i in build(MON, plan, athlete, derived, FakeFetch())["items"] if i["kind"] == "strength")["lifts"][0]
    assert lift["sets"][0]["status"] == "over RPE"


# ------------------------------------------------------------------ grinding


def test_grinding_session(plan, athlete, derived):
    reps = [410, 405, 402, 398, 395, 393, 391, 389]  # the band is 400 W +-3%: 388-412
    ivs = [{"type": "WORK", "average_watts": w, "average_heartrate": hr, "moving_time": 60}
           for w, hr in zip(reps, [140, 146, 149, 151, 152, 154, 155, 156])]  # fmt: skip
    setup(derived, [act("g1", TUE, "grinding", 39, load=35, start="08:00", type="Workout", name="Grinder")])
    b = build(TUE, plan, athlete, derived, FakeFetch(intervals={"g1": ivs}))
    item = next(i for i in b["items"] if i["session"] == "grinding_repeats_8")
    assert item["grade"] == "held"  # 8 reps at ~85% of 470 W = 400 W, all within +-3%
    assert [r["target"] for r in item["reps"]] == ["400 W"] * 8
    assert [r["status"] for r in item["reps"]] == ["held"] * 8
    assert item["fade_pct"] == pytest.approx(round((410 - 389) / 410 * 100, 1))
    assert item["grind_load"] == {"source": "intervals", "efforts": 8, "work_min": 8.0, "efforts_85": 7}  # >= 144.5 bpm
    assert b["verdict"] == ("Grinding repeat-sprints 8 x 1 min at ~85% of 1-min best, 2 min easy done; "
                            "Hand-bike 5 x 4 min at 105-110% hand-bike FTP, 3 min easy missed.")  # fmt: skip
    assert "Grind Load: 8 efforts, 8 work min, 7 at >= 85% LTHR (intervals)" in to_markdown(b)


# ------------------------------------------------------------------ sailing


def hr_stream(*segments):
    """Segments of (seconds, bpm) -> 1 Hz time and heart-rate streams."""
    times, hr, t = [], [], 0
    for secs, bpm in segments:
        for _ in range(secs):
            times.append(t)
            hr.append(bpm)
            t += 1
    return {"time": times, "heartrate": hr}


def test_sailing_day(plan, athlete, derived):
    sun = D(2026, 10, 25)  # a rest day in the plan
    stream = hr_stream((600, 100), (60, 150), (120, 110), (45, 140), (10, 120), (40, 141), (300, 100), (90, 152), (60, 95))
    setup(derived, [act("sail1", sun, "sail", 180, load=54, start="11:00", type="Sail", name="Sailing")])
    b = build(sun, plan, athlete, derived, FakeFetch(detail={"sail1": {"hr_load": 54, "average_heartrate": 112}}, streams={"sail1": stream}))
    (item,) = b["items"]
    assert (item["kind"], item["sport"], item["moving_min"], item["hr_load"]) == ("unplanned", "sail", 180, 54)
    # efforts at >= 136 bpm for >= 20 s; the 10 s dip merges 45 s + 40 s into one: 59 + 94 + 89 s = 4.0 min
    assert item["grind_load"] == {"source": "hr stream", "efforts": 3, "work_min": 4.0, "efforts_85": 2}
    assert b["verdict"] == "Unplanned: sail 180 min."
    assert b["note_for_today"] == "Today: Upper max effort."
    md = to_markdown(b)
    assert "## Unplanned: Sailing (sail)" in md and "HR load 54" in md and "3 efforts, 4 work min, 2 at >= 85% LTHR (hr stream)" in md


def test_unplanned_ride_shows_power_and_drift(plan, athlete, derived):
    thu = D(2026, 10, 22)  # test_grinding was planned; a long ride happened instead
    setup(derived, [act("r1", thu, "bike", 188, load=222, start="10:00", name="Muro Road Cycling")])
    detail = {"r1": {"icu_weighted_avg_watts": 261, "icu_intensity": 84.19, "icu_training_load": 222, "hr_load": 139,
                     "average_heartrate": 141, "decoupling": 14.4}}  # fmt: skip
    b = build(thu, plan, athlete, derived, FakeFetch(detail=detail))
    ride = next(i for i in b["items"] if i["kind"] == "unplanned")
    assert (ride["np_w"], ride["intensity"], ride["tss"], ride["hr_drift_pct"]) == (261, 0.84, 222, 14.4)
    assert b["verdict"] == "Missed: TEST - grinding 1-min and 4-min max efforts (max drag setting). Did bike 188 min instead."
    assert b["note_for_today"] == "Yesterday ran 640% over plan: keep today's easy work easy."
    assert "188 min · NP 261 W · IF 0.84 · TSS 222 · HR load 139 · avg HR 141 · HR drift 14.4%" in to_markdown(b)


def test_hr_efforts():
    s = hr_stream((30, 150), (14, 100), (30, 150), (16, 100), (25, 150), (19, 150))
    eff = hr_efforts(s["time"], s["heartrate"], LTHR)
    # 30 + 14 dip + 30 merge into one (gap 14 < 15); a 16 s dip ends it; then 25 + 19 run on as one
    assert [round(d) for d, _ in eff] == [73, 43]
    assert hr_efforts(*hr_stream((15, 160)).values(), LTHR) == []  # under 20 s isn't an effort


def test_grind_load_without_data():
    assert grind_load(None, {}, LTHR) is None


# ------------------------------------------------------------------ rest, readiness and the brief


def test_rest_day(plan, athlete, derived):
    setup(derived)
    b = build(D(2026, 10, 25), plan, athlete, derived, FakeFetch())
    assert b["verdict"] == "Rest day, as planned." and b["items"] == []


def test_amber_today_comes_first(plan, athlete, derived):
    setup(derived, [act("t1", FRI, "bike", 76)], wellness=[{"date": "2026-10-31", "readiness": "amber", "readiness_reason": "amber: HRV low"}])
    b = build(FRI, plan, athlete, derived, FakeFetch(intervals={"t1": work(280, 255)}))
    assert b["note_for_today"] == "Amber today: last weights held, RPE cap 8."


def test_debrief_goes_into_todays_brief(plan, athlete, derived):
    setup(derived, [act("t1", FRI, "bike", 76)])
    write(build(FRI, plan, athlete, derived, FakeFetch(intervals={"t1": work(280, 255)})), derived)
    b = build_brief(plan, athlete, SAT, derived, status="final")
    assert b["debrief"]["date"] == "2026-10-30" and b["debrief"]["verdict"].endswith("faded in rep 2 (255 W vs 276 W).")
    md = brief_md(b)
    assert "## Yesterday's debrief: Fri 30 Oct 2026" in md and "Note for today:" in md
    assert "## Yesterday (" not in md  # the debrief replaces the plain grades list
    stale = build_brief(plan, athlete, D(2026, 11, 2), derived, status="final")
    assert stale["debrief"] is None  # a debrief for another day is left out
    assert json.loads((derived / "debrief.json").read_text())["date"] == "2026-10-30"
