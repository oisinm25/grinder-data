import csv
import datetime as dt
import json
import math
import re

import pytest
from matplotlib import image as mpimg

from pipeline.dashboard import data, html, load, mail
from pipeline.dashboard.__main__ import build_all
from pipeline.plan.schema import validate_plan
from tests.conftest import ROOT

D = dt.date
TODAY = D(2026, 10, 27)


@pytest.fixture
def plan(plan_data, athlete):
    return validate_plan(plan_data, athlete)


class FakeFetch:
    def __init__(self, streams=None, intervals=None):
        self._streams, self._intervals, self.calls = streams or {}, intervals or {}, []
        self.problems = []

    def streams(self, aid):
        self.calls.append(aid)
        return self._streams.get(aid, {})

    def intervals(self, aid):
        return self._intervals.get(aid)

    def detail(self, aid):
        return None


def write_rows(path, rows):
    fields = list(dict.fromkeys(k for r in rows for k in r)) or ["date"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)


def act(id, day, sport, minutes=60, **kw):
    return {"id": id, "date": day.isoformat(), "start_local": f"{day}T08:00:00", "sport": sport, "type": "Ride", "name": id,
            "moving_min": minutes, "load": kw.pop("load", 50), "power_load": kw.pop("power_load", ""), "hr_load": kw.pop("hr_load", ""),
            "icu_ftp": kw.pop("icu_ftp", ""), **kw}  # fmt: skip


# ------------------------------------------------------------------ load model


def test_activity_load_prefers_power_then_hr():
    assert load.activity_load({"power_load": "80", "hr_load": "60", "load": "70"}) == (80, 0, "power")
    assert load.activity_load({"power_load": "", "hr_load": "60", "load": "70"}) == (60, 0, "hr")
    assert load.activity_load({"load": "70"}) == (70, 0, "intervals.icu")
    assert load.activity_load({}) == (0, 0, "none")


def test_grind_load_correction_for_grinding_and_sailing_only():
    m = {"grind_work_min": "8", "grind_efforts_85": "5"}
    assert load.activity_load({"sport": "grinding", "hr_load": "30"}, m) == (30, 8 * 1.0 + 5 * 2.0, "hr")
    assert load.activity_load({"sport": "sail", "hr_load": "40"}, m)[1] == 18
    assert load.activity_load({"sport": "bike", "hr_load": "40"}, m)[1] == 0
    assert load.activity_load({"sport": "grinding", "hr_load": "30"}, None)[1] == 0  # no metrics, no correction


def test_ctl_atl_are_exponential_averages():
    pts = load.run_model({D(2026, 1, 1): 100}, D(2026, 1, 1), D(2026, 1, 2), ctl0=50, atl0=50)
    assert pts[0].ctl == pytest.approx(50 + 50 * (1 - math.exp(-1 / 42)))
    assert pts[0].atl == pytest.approx(50 + 50 * (1 - math.exp(-1 / 7)))
    assert pts[1].ctl == pytest.approx(pts[0].ctl * math.exp(-1 / 42))  # a rest day decays it
    assert pts[0].form == pytest.approx(pts[0].ctl - pts[0].atl)
    steady = load.run_model({D(2026, 1, 1) + dt.timedelta(days=i): 60 for i in range(400)}, D(2026, 1, 1), D(2027, 2, 4))
    assert steady[-1].ctl == pytest.approx(60, abs=0.01) and steady[-1].atl == pytest.approx(60, abs=0.01)


def test_seed_from_intervals_icu():
    wellness = [{"date": "2026-10-05", "ctl": "28", "atl": "30"}, {"date": "2026-10-07", "ctl": "30.8", "atl": "41"}]
    assert load.seed(wellness, D(2026, 10, 8)) == (30.8, 41)
    assert load.seed(wellness, D(2026, 10, 6)) == (28, 30)
    assert load.seed([], D(2026, 10, 6)) == (0, 0)


def test_planned_loads(plan, athlete):
    designed = load.planned_loads(plan, athlete, D(2026, 10, 26), D(2026, 10, 26))
    assert designed[D(2026, 10, 26)] == 50 + 70  # upper_me + z2_90
    pending = load.planned_loads(plan, athlete, D(2026, 11, 24), D(2026, 11, 24))
    assert pending[D(2026, 11, 24)] == pytest.approx((300 + 450) / 2 / 7)  # ATHX week's weekly_tss midpoint


# ------------------------------------------------------------------ chart data


def test_april_targets_from_goals_md():
    t = data.april_targets(ROOT / "training" / "goals.md")
    assert t["Bike FTP"]["values"][:2] == [365, 380]
    assert t["Grinding 1-min"]["values"][0] == 500 and t["Grinding 4-min"]["values"][0] == 330


def test_hour_power():
    t = list(range(0, 3 * 3600))
    w = [220 if s < 3600 else 200 if s < 7200 else 180 for s in t]
    assert data.hour_power({"time": t, "watts": w}) == (220, 180)
    assert data.hour_power({"time": t[:3600], "watts": w[:3600]}) == (None, None)  # under 2 h


def test_metrics_are_computed_once_and_only_with_streams(athlete, tmp_path):
    acts = [act("g1", TODAY, "grinding", 40), act("r1", TODAY, "bike", 180), act("r2", TODAY, "bike", 60), act("s1", TODAY, "sail", 120)]
    hr = {"time": list(range(600)), "heartrate": [150] * 60 + [100] * 540}
    fetch = FakeFetch(streams={"g1": hr, "r1": {"time": list(range(3 * 3600)), "watts": [200] * (3 * 3600)}})
    m = data.update_metrics(tmp_path, acts, fetch, athlete)
    assert set(m) == {"g1", "r1"}  # r2 is short; s1's streams weren't available, so it's retried later
    assert m["g1"]["grind_efforts"] == "1" and m["r1"]["first_hour_w"] == "200"
    fetch.calls.clear()
    data.update_metrics(tmp_path, acts, fetch, athlete)
    assert fetch.calls == ["s1"]  # g1 and r1 aren't fetched again


def test_weekly_load_by_sport_with_band(plan, athlete):
    acts = [act("a", D(2026, 10, 26), "bike", power_load=70), act("b", D(2026, 10, 26), "strength", hr_load=40),
            act("c", D(2026, 10, 28), "grinding", hr_load=30)]  # fmt: skip
    metrics = {"c": {"grind_work_min": "8", "grind_efforts_85": "5"}}
    weeks = data.weekly_load(plan, athlete, acts, metrics, TODAY)
    w = weeks[0]
    assert (w["week"], w["by_sport"], w["total"], w["band"]) == ("2026-10-26", {"bike": 70, "strength": 40, "grinding": 48}, 158, [650, 750])
    assert w["planned"] == 693 and not w["future"] and weeks[-1]["future"]


def test_bench_checkpoint_status(plan):
    sets = [{"date": "2026-10-19", "lift": "bench_press", "role": "amrap", "kg": "100", "reps": "8", "rpe": "8.5"},
            {"date": "2026-11-27", "lift": "bench_press", "role": "top", "kg": "105", "reps": "6", "rpe": "8"}]  # fmt: skip
    b = data.bench(plan, sets, D(2026, 12, 31))
    status = {c["date"]: c["status"] for c in b["checkpoints"]}
    assert status["2026-10-19"] == "met"  # 131.7 against 131.5
    assert status["2026-11-29"] == "met"  # 133.0 against 132.5
    assert status["2026-12-31"] == "no data"
    assert status["2027-04-17"] == "upcoming"
    missed = data.bench(plan, sets[:1] + [{**sets[1], "kg": "95"}], D(2026, 12, 1))
    assert {c["date"]: c["status"] for c in missed["checkpoints"]}["2026-11-29"] == "missed"  # 120.3, more than a rep under
    assert b["goal"] == {"date": "2027-04-17", "e1rm_needed_kg": 147}


def test_compliance_and_body():
    rows = [{"date": "2026-10-26", "modality": "bike", "grade": "held"}, {"date": "2026-10-27", "modality": "bike", "grade": "held"},
            {"date": "2026-10-28", "modality": "strength", "grade": "under"}]  # fmt: skip
    assert data.compliance(rows) == [{"week": "2026-10-26", "held": 2, "strength_under": 1}]
    weights = [{"date": f"2026-10-{d}", "weight_kg": str(w)} for d, w in ((20, 92), (21, 93), (22, 91))]
    assert data.body(weights)["weight"][-1] == {"date": "2026-10-22", "weight_kg": 91, "avg7_kg": 92.0}


# ------------------------------------------------------------------ the whole build


@pytest.fixture
def derived(tmp_path):
    d = tmp_path / "derived"
    d.mkdir()
    acts = [act(f"z{i}", D(2026, 10, 14) + dt.timedelta(days=i), "bike", 60, power_load=50 + i, icu_ftp=310) for i in range(12)]
    acts.append(act("long", D(2026, 10, 24), "bike", 200, power_load=180, icu_ftp=310))
    write_rows(d / "activities.csv", acts)
    write_rows(d / "wellness.csv", [{"date": (D(2026, 10, 13) + dt.timedelta(days=i)).isoformat(), "ctl": 30 + i / 2, "atl": 35,
                                     "readiness": ["green", "amber", "red", ""][i % 4], "hrv": 45 + i % 5, "hrv_baseline": 46,
                                     "resting_hr": 55, "resting_hr_baseline": 56, "sleep_h": 7, "weight_kg": 92 + i / 10} for i in range(15)])  # fmt: skip
    write_rows(d / "strength.csv", [{"date": "2026-10-19", "session": "test_bench", "lift": "bench_press", "role": "amrap", "set_no": 1,
                                     "kg": 100, "reps": 8, "rpe": 8.5},
                                    {"date": "2026-10-21", "session": "test_lower", "lift": "trap_bar_deadlift", "role": "test", "set_no": 1,
                                     "kg": 180, "reps": 6, "rpe": ""}])  # fmt: skip
    write_rows(d / "compliance.csv", [{"date": "2026-10-26", "slot": "mon_pm", "session": "z2_90", "modality": "bike", "item": "session", "grade": "held"}])
    (d / "today.json").write_text(json.dumps({"date": TODAY.isoformat(), "status": "final", "readiness": {"colour": "amber", "reason": "amber: HRV low"},
                                              "sessions": [{"slot": "tue_am", "summary": "Hand-bike 5 x 4", "strength": None}]}))  # fmt: skip
    (d / "debrief.json").write_text(json.dumps({"date": "2026-10-26", "verdict": "Held the plan.", "note_for_today": "Today: Hand-bike.",
                                                "items": [{"kind": "endurance", "summary": "Z2 90 min", "grade": "held",
                                                           "reps": [{"n": 1, "target": "195-216 W", "band": [195, 216], "watts": 205, "status": "held"}]}]}))  # fmt: skip
    return d


def test_dashboard_json_has_all_ten_charts(plan, athlete, derived):
    d, _ = build_all(plan, athlete, derived, TODAY, FakeFetch(), ROOT / "training" / "goals.md")
    assert set(d["charts"]) == {"fitness", "weekly_load", "yesterday", "bench", "lifts", "grinding", "bike", "readiness", "compliance", "body"}
    f = d["charts"]["fitness"]
    assert f["seed"] == {"date": "2026-10-13", "ctl": 30.0, "atl": 35.0}  # intervals.icu's values the day before the history starts
    assert f["days"][-1]["date"] == TODAY.isoformat() and f["planned"][0]["date"] == "2026-10-28"
    assert {"label": "athx", "date": "2026-11-23", "end": "2026-11-29", "priority": "C"} in f["events"]
    assert {e["label"] for e in f["events"]} >= {"solo-312", "bench-10x110", "athx", "hyrox"}
    assert d["charts"]["bike"]["ftp_intervals_icu"] == [{"date": "2026-10-14", "ftp": 310}]
    assert d["charts"]["bike"]["ftp_target"] == [365, 380] and d["charts"]["grinding"]["target"] == {"60s": 500, "240s": 330}
    assert d["charts"]["bike"]["long_rides"] == [{"date": "2026-10-24", "minutes": 200, "name": "long"}]
    assert d["charts"]["lifts"]["trap_bar_deadlift"] == [{"date": "2026-10-21", "e1rm": 216.0, "set": "180 x 6"}]
    assert d["status"] == "final" and d["charts"]["yesterday"]["verdict"] == "Held the plan."
    assert json.loads((derived / "dashboard.json").read_text())["date"] == TODAY.isoformat()


def test_dashboard_html_is_self_contained(plan, athlete, derived):
    build_all(plan, athlete, derived, TODAY, FakeFetch(), ROOT / "training" / "goals.md")
    page = (derived / "dashboard.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", page)  # no network requests
    assert not re.search(r"<script(?![^>]*application/json)", page)  # data embedded, nothing executes
    assert "prefers-color-scheme: dark" in page and 'name=viewport content="width=device-width' in page
    for title in ("Fitness and fatigue", "Weekly load", "Yesterday (26 Oct)", "Bench line", "Lower body and pulling",
                  "Grinding curve", "Bike FTP and durability", "Readiness", "Compliance", "Body composition"):  # fmt: skip
        assert f"<h2>{title}</h2>" in page
    assert json.loads(re.search(r'id="dashboard-data">(.*?)</script>', page).group(1))["date"] == TODAY.isoformat()


def test_email_version(plan, athlete, derived):
    _, manifest = build_all(plan, athlete, derived, TODAY, FakeFetch(), ROOT / "training" / "goals.md")
    out = derived / "email"
    assert [i["cid"] for i in manifest["images"]] == ["fitness_fatigue", "weekly_load", "bench_line", "readiness_strip", "yesterday_intervals"]
    for img in manifest["images"]:
        path = out / img["file"]
        assert path.stat().st_size < 150_000 and img["alt"]
        pixels = mpimg.imread(path)
        assert pixels.shape[1] == 1200  # 600 px at 2x
        assert pixels[2, 2][:3].tolist() == [1.0, 1.0, 1.0]  # white background
    page = (out / "dashboard_email.html").read_text(encoding="utf-8")
    assert "<script" not in page and "<svg" not in page and "<style" not in page  # email-safe: inline CSS, tables
    assert set(re.findall(r'src="cid:([a-z_]+)"', page)) == {i["cid"] for i in manifest["images"]}
    assert page.index("amber") < page.index("Hand-bike 5 x 4") < page.index("Held the plan.") < page.index("cid:fitness_fatigue")
    assert (manifest["date"], manifest["status"], manifest["html"]) == ("2026-10-27", "final", "dashboard_email.html")


def test_stale_yesterday_chart_is_removed(plan, athlete, derived):
    build_all(plan, athlete, derived, TODAY, FakeFetch(), ROOT / "training" / "goals.md")
    assert (derived / "email" / "yesterday_intervals.png").exists()
    build_all(plan, athlete, derived, TODAY + dt.timedelta(days=1), FakeFetch(), ROOT / "training" / "goals.md")  # debrief is now stale
    assert not (derived / "email" / "yesterday_intervals.png").exists()
