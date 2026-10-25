import csv
import datetime as dt
import json
import shutil
from zoneinfo import ZoneInfo

import httpx
import pytest

from pipeline.analyse import analyse, anchor_flags, main, power_curve, to_markdown
from pipeline.plan.schema import validate_plan
from tests.conftest import ROOT

MADRID = ZoneInfo("Europe/Madrid")
D = dt.date
KEY = "fake-key-" + "0123456789abcdef"
ENV = {"INTERVALS_ICU_API_KEY": KEY, "INTERVALS_ICU_ATHLETE_ID": "i99999"}


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


def watts_stream(*blocks):
    """(seconds, watts) blocks -> a 1 Hz time/watts stream."""
    t, w, s = [], [], 0
    for secs, watts in blocks:
        for _ in range(secs):
            t.append(s)
            w.append(watts)
            s += 1
    return {"time": t, "watts": w, "heartrate": [150] * len(t)}


def write_rows(path, rows):
    fields = list(dict.fromkeys(k for r in rows for k in r)) or ["date"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)


def act(id, day, sport, minutes, start="17:30", **kw):
    return {"id": id, "date": day.isoformat(), "start_local": f"{day}T{start}:00", "sport": sport, "type": kw.pop("type", "Ride"),
            "name": kw.pop("name", "Ride"), "moving_min": minutes, "load": kw.pop("load", 80), "power_load": kw.pop("power_load", 80),
            "hr_load": "", "avg_watts": kw.pop("avg_watts", ""), **kw}  # fmt: skip


@pytest.fixture
def derived(tmp_path):
    d = tmp_path / "derived"
    d.mkdir()
    write_rows(d / "wellness.csv", [{"date": "2026-10-20", "ctl": 30, "atl": 35}])
    write_rows(d / "strength.csv", [{"date": "2026-10-19", "session": "test_bench", "lift": "bench_press", "role": "amrap",
                                     "set_no": 1, "kg": 100, "reps": 8, "rpe": 8.5}])  # fmt: skip
    return d


def run(plan, athlete, derived, acts, fetch, when, choice="latest"):
    write_rows(derived / "activities.csv", acts)
    return analyse(choice, plan, athlete, derived, fetch, when)


FRI = D(2026, 10, 30)
AT_FRI = dt.datetime(2026, 10, 30, 19, 0, tzinfo=MADRID)


# ------------------------------------------------------------------ sessions


def test_threshold_session(plan, athlete, derived):
    stream = watts_stream((900, 180), (1200, 280), (300, 160), (1200, 281), (600, 150))
    fetch = FakeFetch(detail={"t1": {"icu_weighted_avg_watts": 262, "icu_intensity": 87.3, "icu_training_load": 85, "decoupling": 2.1}},
                      intervals={"t1": [{"type": "WORK", "average_watts": 280, "average_heartrate": 160},
                                        {"type": "WORK", "average_watts": 281, "average_heartrate": 165}]},
                      streams={"t1": stream})  # fmt: skip
    r = run(plan, athlete, derived, [act("t1", FRI, "bike", 70, power_load=85)], fetch, AT_FRI)
    item = r["breakdown"]
    assert r["status"] == "ok" and item["session"] == "bike_threshold_2x20" and item["grade"] == "held"
    assert [x["status"] for x in item["reps"]] == ["held", "held"]
    assert (item["np_w"], item["intensity"], item["tss"]) == (262, 0.87, 85)
    assert item["power_curve"]["1200"] == 281 and item["power_curve"]["60"] == 281
    assert r["anchor_flags"] == []  # 281 x 0.95 = 267 W: under the 300 W anchor
    f = r["fitness"]
    assert f["session_load"] == 85 and f["load_source"] == "power"
    assert f["after"]["ctl"] > f["before"]["ctl"] and f["after"]["atl"] > f["before"]["atl"]
    assert f["projections"]["next_key_session"]["date"] == "2026-10-31"  # Saturday's 3 h ride
    assert f["week"]["label"] == "acc1" and f["week"]["band"] == [650, 750]
    assert (derived / "analysis" / "2026-10-30-t1.md").exists() and (derived / "analysis" / "2026-10-30-t1.json").exists()
    latest = json.loads((derived / "analysis" / "latest.json").read_text())
    assert {k: latest[k] for k in ("status", "activity_id")} == {"status": "ok", "activity_id": "t1"}
    assert set(latest) >= {"generated_at", "breakdown", "fitness", "projections", "bests", "anchor_flags"}


def test_20min_test_triggers_an_ftp_flag(plan, athlete, derived):
    sat = D(2026, 10, 24)
    stream = watts_stream((1200, 200), (300, 330), (600, 150), (1200, 361), (600, 140))
    r = run(plan, athlete, derived, [act("ftp1", sat, "bike", 65, start="09:00", name="Coll de sa Creu")],
            FakeFetch(streams={"ftp1": stream}), dt.datetime(2026, 10, 24, 12, 0, tzinfo=MADRID))  # fmt: skip
    assert r["breakdown"]["session"] == "test_bike_20"
    (flag,) = r["anchor_flags"]
    assert flag == {"anchor": "ftp.bike", "current_w": 300.0, "suggested_w": 343, "change_pct": 14.3,
                    "evidence": "20-min best 361 W x 0.95"}  # fmt: skip
    md = to_markdown(r)
    assert "**Anchor flag: ftp.bike**: 20-min best 361 W x 0.95 = 343 W, +14.3% against the 300 W anchor." in md
    assert "20 min 361 W" in md and "Pedestal never changes `config/athlete.yaml`" in md


def test_strength_session(plan, athlete, derived):
    mon = D(2026, 10, 26)
    write_rows(derived / "strength.csv", [
        {"date": "2026-10-19", "session": "test_bench", "lift": "bench_press", "role": "amrap", "set_no": 1, "kg": 100, "reps": 8, "rpe": 8.5},
        {"date": "2026-10-26", "session": "upper_me", "lift": "bench_press", "role": "top", "set_no": 1, "kg": 105, "reps": 6, "rpe": 8},
        {"date": "2026-10-26", "session": "upper_me", "lift": "bench_press", "role": "backoff", "set_no": 1, "kg": 85, "reps": 8, "rpe": ""},
    ])  # fmt: skip
    r = run(plan, athlete, derived, [act("s1", mon, "strength", 60, start="07:00", type="WeightTraining", power_load="", hr_load=40)],
            FakeFetch(), dt.datetime(2026, 10, 26, 9, 0, tzinfo=MADRID))  # fmt: skip
    lift = r["breakdown"]["lifts"][0]
    assert r["breakdown"]["kind"] == "strength" and lift["sets"][0]["prescribed"] == "1 x 6 @ 100 kg RPE<=8"
    assert lift["sets"][0]["status"] == "hit" and (lift["e1rm_before"], lift["e1rm_after"]) == (131.7, 133.0)
    assert {"kind": "e1rm", "lift": "bench_press", "name": "Bench press", "e1rm": 133.0, "previous": 131.7, "first": False} in r["bests"]
    assert r["breakdown"]["power_curve"] == {} and r["anchor_flags"] == []
    assert "- e1RM 131.7 -> 133 (+1.3)" in to_markdown(r)


def test_grinding_session(plan, athlete, derived):
    tue = D(2026, 10, 27)
    reps = [410, 405, 402, 398, 395, 393, 391, 389]
    blocks = [(600, 120)] + [b for w in reps for b in ((60, w), (120, 90))] + [(300, 80)]
    ivs = [{"type": "WORK", "average_watts": w, "average_heartrate": 150, "moving_time": 60} for w in reps]
    r = run(plan, athlete, derived, [act("g1", tue, "grinding", 39, start="08:00", type="Workout", name="Grinder", power_load="", hr_load=30)],
            FakeFetch(intervals={"g1": ivs}, streams={"g1": watts_stream(*blocks)}), dt.datetime(2026, 10, 27, 12, 0, tzinfo=MADRID))  # fmt: skip
    item = r["breakdown"]
    assert item["session"] == "grinding_repeats_8" and item["grade"] == "held"
    assert item["fade_pct"] == pytest.approx(5.1) and item["grind_load"]["efforts"] == 8
    assert item["power_curve"]["60"] == 410
    assert r["anchor_flags"] == []  # 410 W is under the 470 W curve point
    # hrTSS under-reads grinding: the Grind Load correction is added
    assert r["fitness"]["grind_correction"] > 0 and r["fitness"]["session_load"] == pytest.approx(30 + r["fitness"]["grind_correction"])


def test_grinding_curve_flag():
    from pipeline.athlete import load_athlete

    athlete = load_athlete(ROOT / "config" / "athlete.yaml")
    flags = anchor_flags("grinding", {60: 495, 240: 305}, athlete)
    assert [f["anchor"] for f in flags] == ["curves.grinding_60s"]  # 495 is +5.3%; 305 is only +1.3%


# ------------------------------------------------------------------ selection and status


def test_no_new_activity(plan, athlete, derived):
    acts = [act("a1", FRI, "bike", 60)]
    fetch = FakeFetch(streams={"a1": watts_stream((3600, 200))})
    assert run(plan, athlete, derived, acts, fetch, AT_FRI)["status"] == "ok"
    again = run(plan, athlete, derived, acts, fetch, AT_FRI)
    assert again["status"] == "no_new_activity" and again["activity_id"] == "a1"
    assert json.loads((derived / "analysis" / "latest.json").read_text())["status"] == "no_new_activity"
    assert run(plan, athlete, derived, acts, fetch, AT_FRI, choice="a1")["status"] == "ok"  # an explicit id always runs
    newer = acts + [act("a2", FRI, "bike", 30, start="19:00")]
    assert run(plan, athlete, derived, newer, fetch, AT_FRI)["activity_id"] == "a2"


def test_unknown_id_is_an_error(plan, athlete, derived):
    r = run(plan, athlete, derived, [act("a1", FRI, "bike", 60)], FakeFetch(), AT_FRI, choice="i000")
    assert r == {"status": "error", "generated_at": "2026-10-30T19:00+01:00", "activity_id": "i000", "error": "activity i000 not found"}


def test_power_bests_are_kept(plan, athlete, derived):
    fetch = FakeFetch(streams={"a1": watts_stream((1200, 300)), "a2": watts_stream((1200, 290)), "a3": watts_stream((1200, 310))})
    run(plan, athlete, derived, [act("a1", FRI, "bike", 20)], fetch, AT_FRI)
    second = run(plan, athlete, derived, [act("a1", FRI, "bike", 20), act("a2", FRI, "bike", 20, start="19:00")], fetch, AT_FRI)
    assert not [b for b in second["bests"] if b["duration"] == "20 min"]
    third = run(plan, athlete, derived, [act("a1", FRI, "bike", 20), act("a3", FRI, "bike", 20, start="20:00")], fetch, AT_FRI)
    best = next(b for b in third["bests"] if b["duration"] == "20 min")
    assert best["watts"] == 310 and best["previous"]["watts"] == 300


def test_power_curve_rebuilds_1hz_and_zeroes_pauses():
    s = {"time": [0, 2, 4, 6, 16, 18], "watts": [300, 300, 300, 300, 300, 300]}  # 2 s recording, a 10 s pause
    assert power_curve(s) == {5: 300}
    assert power_curve({"time": list(range(30)), "watts": [0] * 30}) == {5: 0}
    assert power_curve({}) == {}


# ------------------------------------------------------------------ read-only beyond derived/analysis


class FakeIntervals:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request):
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/activities"):
            return httpx.Response(200, json=[{"id": "x1", "start_date_local": "2026-10-24T09:00:00", "type": "Ride", "name": "Coll de sa Creu",
                                              "moving_time": 3900, "icu_training_load": 75, "power_load": 75}])  # fmt: skip
        if path.endswith("/wellness"):
            return httpx.Response(200, json=[{"id": "2026-10-24", "restingHR": 52, "hrv": 56.0, "sleepSecs": 30000, "ctl": 31, "atl": 40}])
        if path.endswith("/streams.json"):
            s = watts_stream((1200, 200), (1200, 361), (600, 150))
            return httpx.Response(200, json=[{"type": k, "data": v} for k, v in s.items()])
        if path.endswith("/intervals"):
            return httpx.Response(200, json={"icu_intervals": []})
        if path.endswith("/x1"):
            return httpx.Response(200, json={"icu_weighted_avg_watts": 300})
        return httpx.Response(404)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "training", tmp_path / "training")
    (tmp_path / "derived").mkdir()
    (tmp_path / "derived" / "today.json").write_text('{"date": "2026-10-24", "status": "final"}')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_never_writes_todays_brief_the_anchors_or_the_calendar(workdir, capsys):
    before = {p: (workdir / p).read_bytes() for p in ("derived/today.json", "config/athlete.yaml", "config/plan.yaml")}
    fake = FakeIntervals()
    code = main(["--activity", "latest", "--cache", str(workdir / "cache")], env=ENV, transport=httpx.MockTransport(fake),
                sleep=lambda s: None, now=dt.datetime(2026, 10, 24, 12, 0, tzinfo=MADRID))  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out
    assert "anchor flags: ftp.bike -> 343 W" in out and KEY not in out
    for p, data in before.items():
        assert (workdir / p).read_bytes() == data, f"{p} changed"
    assert all(m == "GET" for m, _ in fake.requests)  # nothing written to intervals.icu
    assert not any("/events" in p for _, p in fake.requests)  # the calendar isn't even read
    written = {p.relative_to(workdir / "derived").as_posix() for p in (workdir / "derived").rglob("*") if p.is_file()}
    assert {"analysis/latest.json", "analysis/2026-10-24-x1.md", "analysis/2026-10-24-x1.json", "analysis/bests.json"} <= written
    assert not any(p.startswith(("today.md", "dashboard", "email/", "prescriptions")) for p in written)


def test_analyse_workflow():
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "analyse.yml").read_text(encoding="utf-8"))
    assert wf[True]["push"] == {"branches": ["requests"]} and wf[True]["workflow_dispatch"]["inputs"]["activity"]["default"] == "latest"
    assert wf["concurrency"] == {"group": "analyse", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "write"}
    text = (ROOT / ".github" / "workflows" / "analyse.yml").read_text(encoding="utf-8")
    assert "${{ github.event.inputs.activity }}" not in text.split("run:")[1]  # untrusted input only via env, never inlined
    assert "git add analysis" in text and "publish" not in text and "today.json" not in text.split("steps:")[1].replace("never today.json", "")


def test_activity_argument_is_validated(workdir):
    with pytest.raises(SystemExit):
        main(["--activity", "latest; rm -rf /"], env=ENV)


def test_errors_land_in_latest_json_without_secrets(workdir, capsys):
    def down(request):
        raise httpx.ConnectError(f"no route {KEY}", request=request)

    code = main(["--activity", "latest"], env=ENV, transport=httpx.MockTransport(down), sleep=lambda s: None,
                now=dt.datetime(2026, 10, 24, 12, 0, tzinfo=MADRID))  # fmt: skip
    latest = json.loads((workdir / "derived" / "analysis" / "latest.json").read_text())
    assert code == 1 and latest["status"] == "error"
    assert KEY not in json.dumps(latest) and KEY not in capsys.readouterr().out
