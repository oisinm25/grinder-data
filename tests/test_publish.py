import datetime as dt
import json
import xml.etree.ElementTree as ET

import fitdecode
import httpx
import pytest

from pipeline.plan.expand import ResolvedStep, ResolvedTarget, expand
from pipeline.plan.schema import validate_plan
from pipeline.prescribe import LogRow
from pipeline.publish.__main__ import PREFIX, build, main, sync
from pipeline.publish.formats import fit_crc, fit_workout, text_duration, workout_text, zwo
from pipeline.ingest.intervals import IntervalsClient
from tests.conftest import ROOT

D = dt.date
MON, SUN = D(2026, 10, 26), D(2026, 11, 1)
FAKE_ENV = {"INTERVALS_ICU_API_KEY": "fake-key-" + "0123456789abcdef", "INTERVALS_ICU_ATHLETE_ID": "i99999"}
TEST_BENCH = LogRow(D(2026, 10, 19), "test_bench", "bench_press", "amrap", 100, 8, 8.5)


@pytest.fixture
def plan(plan_data, athlete):
    return validate_plan(plan_data, athlete)


def session(plan, athlete, day, sid):
    return next(s for d in expand(plan, athlete, day, day) for s in d.sessions if s.session_id == sid)


# ------------------------------------------------------------------ workout text


def test_text_durations():
    assert [text_duration(s) for s in (30, 60, 90, 1200, 10800)] == ["30s", "1m", "1m30", "20m", "180m"]


def test_workout_text_for_intervals(plan, athlete):
    s = session(plan, athlete, D(2026, 10, 27), "handbike_intervals")
    assert workout_text(s.steps) == [
        "- Warmup 10m 126-164w", "", "Main set 5x", "- 4m 265-277w", "- Easy 3m 113-139w", "", "- Cooldown 5m 113-139w",
    ]  # fmt: skip


def test_workout_text_rpe_curve_and_notes(plan, athlete):
    grind = session(plan, athlete, D(2026, 10, 27), "grinding_repeats_8")
    assert workout_text(grind.steps)[0] == "- Warmup RPE 4 10m"
    assert "- 1m 400w" in workout_text(grind.steps)
    test = session(plan, athlete, D(2026, 10, 24), "test_bike_20")
    assert "- TEST RPE 10 20m, Even pacing; start near 300 W" in workout_text(test.steps)


def test_workout_text_hr_and_cadence():
    t = ResolvedTarget("hr_zone", hr_zone=2, cadence=(85, 95))
    assert workout_text((ResolvedStep("steady", 3600, t),)) == ["- 60m Z2 HR 85-95rpm"]


# ------------------------------------------------------------------ .zwo


def test_zwo_is_valid_and_uses_intervals(plan, athlete):
    s = session(plan, athlete, D(2026, 10, 30), "bike_threshold_2x20")
    root = ET.fromstring(zwo("Threshold", "2x20", s.steps, 300))
    els = list(root.find("workout"))
    assert [e.tag for e in els] == ["SteadyState", "IntervalsT", "SteadyState"]
    iv = els[1].attrib
    assert (iv["Repeat"], iv["OnDuration"], iv["OffDuration"]) == ("2", "1200", "300")
    assert float(iv["OnPower"]) == pytest.approx(280.5 / 300, abs=1e-3)  # midpoint of 276-285 W


def test_zwo_free_ride_for_effort_steps(plan, athlete):
    s = session(plan, athlete, D(2026, 10, 24), "test_bike_20")
    tags = [e.tag for e in ET.fromstring(zwo("Test", "", s.steps, 300)).find("workout")]
    assert tags[3] == "FreeRide"  # the RPE 10 test


# ------------------------------------------------------------------ .fit


def decode_steps(data: bytes, tmp_path):
    p = tmp_path / "w.fit"
    p.write_bytes(data)
    steps, workout = [], None
    with fitdecode.FitReader(str(p), check_crc=fitdecode.CrcCheck.RAISE) as fr:
        for frame in fr:
            if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "workout":
                workout = {f.name: f.value for f in frame.fields}
            if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "workout_step":
                steps.append({f.name: f.value for f in frame.fields})
    return workout, steps


def test_fit_workout_decodes(plan, athlete, tmp_path):
    s = session(plan, athlete, D(2026, 10, 27), "handbike_intervals")
    workout, steps = decode_steps(fit_workout("Hand-bike 5x4", "handbike", s.steps), tmp_path)
    assert (workout["sport"], workout["sub_sport"], workout["num_valid_steps"]) == ("cycling", "hand_cycling", 5)
    assert [st["duration_type"] for st in steps] == ["time", "time", "time", "repeat_until_steps_cmplt", "time"]
    assert steps[1]["duration_time"] == 240 and steps[1]["intensity"] == "active"
    assert steps[3]["repeat_steps"] == 5 and steps[3]["duration_step"] == 1


def test_fit_crc_matches_known_value():
    assert fit_crc(b"123456789") == 0xBB3D  # CRC-16/ARC check value


# ------------------------------------------------------------------ events


def test_events_for_the_week(plan, athlete):
    events, _ = build(plan, athlete, [TEST_BENCH], {}, MON, SUN)
    assert len(events) == 13  # Block 1 week 1, rest days excluded
    by_id = {e["external_id"]: e for e in events}
    upper = by_id[f"{PREFIX}winter-2026-27:2026-10-26:mon_am:0"]
    assert upper["type"] == "WeightTraining" and upper["start_date_local"] == "2026-10-26T07:00:00"
    assert "- Bench press (top): 1 x 6 @ 100 kg, RPE<=8" in upper["description"]
    z2 = by_id[f"{PREFIX}winter-2026-27:2026-10-26:mon_pm:0"]
    assert (z2["type"], z2["indoor"], z2["moving_time"], z2["icu_training_load"]) == ("VirtualRide", True, 5400, 70)
    long = by_id[f"{PREFIX}winter-2026-27:2026-10-31:sat_am:0"]
    assert (long["type"], long["indoor"]) == ("Ride", False)
    grind = by_id[f"{PREFIX}winter-2026-27:2026-10-27:tue_am:1"]
    assert grind["start_date_local"] == "2026-10-27T08:00:00"  # second session in the slot
    assert grind["name"].lower().startswith("grind")


def test_test_grinding_gets_a_grind_title(plan, athlete):
    events, _ = build(plan, athlete, None, {}, D(2026, 10, 22), D(2026, 10, 22))
    assert events[0]["name"] == "Grind: TEST - grinding 1-min and 4-min max efforts (max drag setting)"
    assert "readiness-exempt" in events[0]["description"]


# ------------------------------------------------------------------ sync


class FakeCalendar:
    def __init__(self, existing):
        self.existing = existing
        self.calls = []

    def __call__(self, request: httpx.Request):
        self.calls.append((request.method, request.url.path, request.content))
        if request.method == "GET":
            return httpx.Response(200, json=self.existing)
        if request.method == "POST":
            return httpx.Response(200, json=json.loads(request.content))
        if request.method == "PUT":
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(200)


def test_sync_only_touches_pedestal_events():
    existing = [
        {"id": 11, "external_id": f"{PREFIX}p:2026-10-26:mon_am:0", "name": "old upper"},
        {"id": 12, "external_id": f"{PREFIX}p:2026-10-27:tue_am:0", "name": "dropped by a swap"},
        {"id": 13, "external_id": None, "name": "Club ride (added by hand)"},
        {"id": 14, "external_id": "other-app:1", "name": "another app's event"},
    ]
    fake = FakeCalendar(existing)
    events = [
        {"external_id": f"{PREFIX}p:2026-10-26:mon_am:0", "name": "upper"},
        {"external_id": f"{PREFIX}p:2026-10-26:mon_pm:0", "name": "z2"},
    ]
    with IntervalsClient("k", "i99999", transport=httpx.MockTransport(fake), sleep=lambda s: None) as c:
        assert sync(c, events, MON, SUN) == (1, 1, 1)
    methods = [(m, p) for m, p, _ in fake.calls]
    assert methods == [
        ("GET", "/api/v1/athlete/i99999/events"),
        ("PUT", "/api/v1/athlete/i99999/events/11"),
        ("POST", "/api/v1/athlete/i99999/events/bulk"),
        ("DELETE", "/api/v1/athlete/i99999/events/12"),
    ]
    assert json.loads(fake.calls[2][2]) == [events[1]]


# ------------------------------------------------------------------ CLI


def test_dry_run_sends_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    fake = FakeCalendar([])
    code = main(["--from", "2026-10-26", "--exports", str(tmp_path), "--strength", str(tmp_path / "none.csv")],
                env=FAKE_ENV, transport=httpx.MockTransport(fake))  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0 and fake.calls == []
    assert "Dry run: nothing sent" in out
    events = json.loads((tmp_path / "intervals_events_2026-10-26_2026-11-01.json").read_text(encoding="utf-8"))
    assert len(events) == 13
    files = sorted(p.name for p in (tmp_path / "workouts").iterdir())
    assert "2026-10-30_fri_pm_0_bike_threshold_2x20.zwo" in files
    assert "2026-10-27_tue_am_1_grinding_repeats_8.fit" in files
    assert not any(n.endswith(".zwo") and "handbike" in n for n in files)  # .zwo is bike only


def test_push(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    fake = FakeCalendar([])
    code = main(["--from", "2026-10-26", "--exports", str(tmp_path), "--push"],
                env=FAKE_ENV, transport=httpx.MockTransport(fake), sleep=lambda s: None)  # fmt: skip
    assert code == 0
    assert "13 created, 0 updated, 0 stale" in capsys.readouterr().out
    assert [m for m, _, _ in fake.calls] == ["GET", "POST"]


def test_push_without_secret_sends_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    fake = FakeCalendar([])
    code = main(["--from", "2026-10-26", "--exports", str(tmp_path), "--push"], env={}, transport=httpx.MockTransport(fake))
    assert code == 2 and fake.calls == []
    assert "INTERVALS_ICU_API_KEY is not set" in capsys.readouterr().err
