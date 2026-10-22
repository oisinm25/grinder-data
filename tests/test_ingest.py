import base64
import csv
import datetime as dt
import json

import httpx
import pytest

from pipeline.ingest.__main__ import main
from pipeline.ingest.intervals import IntervalsClient, IntervalsError
from pipeline.ingest.strength import StrengthLogError, latest_top_sets, read_strength_log
from pipeline.ingest.tidy import ACTIVITY_FIELDS, activity_row, add_readiness, map_sport, upsert_range, wellness_row
from pipeline.plan.schema import validate_plan
from pipeline.settings import MissingSetting, require_env
from tests.conftest import ROOT

FIXTURES = ROOT / "tests" / "fixtures" / "intervals"
FAKE_KEY = "fake-key-" + "0123456789abcdef"
FAKE_ENV = {"INTERVALS_ICU_API_KEY": FAKE_KEY, "INTERVALS_ICU_ATHLETE_ID": "i99999"}
D = dt.date


def recorded(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeIntervals:
    """A mock transport serving the recorded responses; can fail the first N calls."""

    def __init__(self, fail_first=0, status=429, headers=None):
        self.calls: list[httpx.Request] = []
        self.fail_first, self.status, self.headers = fail_first, status, headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if len(self.calls) <= self.fail_first:
            return httpx.Response(self.status, headers=self.headers, text="slow down")
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=recorded(name))

    @property
    def transport(self):
        return httpx.MockTransport(self)


def client(fake, tmp_path=None, **kw):
    return IntervalsClient(FAKE_KEY, "i99999", transport=fake.transport, cache_dir=tmp_path, sleep=kw.pop("sleep", lambda s: None), **kw)


# ------------------------------------------------------------------ settings


def test_missing_setting_names_the_variable():
    with pytest.raises(MissingSetting, match="INTERVALS_ICU_API_KEY is not set"):
        require_env("INTERVALS_ICU_API_KEY", {})
    with pytest.raises(MissingSetting):
        require_env("INTERVALS_ICU_API_KEY", {"INTERVALS_ICU_API_KEY": "   "})
    assert require_env("X", {"X": " v "}) == "v"


# ------------------------------------------------------------------ client


def test_request_shape_and_basic_auth():
    fake = FakeIntervals()
    with client(fake) as c:
        assert len(c.activities(D(2026, 10, 14), D(2026, 10, 22))) == 10
    (req,) = fake.calls
    assert req.url.path == "/api/v1/athlete/i99999/activities"
    assert dict(req.url.params) == {"oldest": "2026-10-14", "newest": "2026-10-22"}
    assert req.headers["authorization"] == "Basic " + base64.b64encode(f"API_KEY:{FAKE_KEY}".encode()).decode()
    assert FAKE_KEY not in str(req.url)


def test_key_not_in_repr():
    with client(FakeIntervals()) as c:
        assert FAKE_KEY not in repr(c) and "i99999" in repr(c)


def test_retries_rate_limit_honouring_retry_after():
    waits = []
    fake = FakeIntervals(fail_first=2, status=429, headers={"Retry-After": "3"})
    with client(fake, sleep=waits.append) as c:
        assert len(c.wellness(D(2026, 10, 14), D(2026, 10, 17))) == 13
    assert waits == [3.0, 3.0] and len(fake.calls) == 3


def test_retries_server_errors_with_backoff():
    waits = []
    fake = FakeIntervals(fail_first=3, status=503)
    with client(fake, sleep=waits.append) as c:
        c.wellness(D(2026, 10, 14), D(2026, 10, 17))
    assert waits == [1.0, 2.0, 4.0]


def test_gives_up_after_max_attempts():
    fake = FakeIntervals(fail_first=99, status=503)
    with client(fake, max_attempts=3) as c, pytest.raises(IntervalsError, match="503"):
        c.wellness(D(2026, 10, 14), D(2026, 10, 17))
    assert len(fake.calls) == 3


def test_network_failure_is_retried_then_reported():
    attempts = []

    def down(request):
        attempts.append(request)
        raise httpx.ConnectError("no route", request=request)

    c = IntervalsClient(FAKE_KEY, "i99999", transport=httpx.MockTransport(down), sleep=lambda s: None, max_attempts=2)
    with pytest.raises(IntervalsError, match="unreachable") as exc:
        c.activities(D(2026, 10, 14), D(2026, 10, 22))
    assert len(attempts) == 2 and FAKE_KEY not in str(exc.value)


def test_auth_failure_names_variables_not_values():
    fake = FakeIntervals(fail_first=1, status=401)
    with client(fake) as c, pytest.raises(IntervalsError) as exc:
        c.activities(D(2026, 10, 14), D(2026, 10, 22))
    msg = str(exc.value)
    assert "INTERVALS_ICU_API_KEY" in msg and FAKE_KEY not in msg
    assert len(fake.calls) == 1  # no retry on 401


def test_cache_written_without_key_and_read_offline(tmp_path):
    with client(FakeIntervals(), tmp_path) as c:
        c.activities(D(2026, 10, 14), D(2026, 10, 22))
    cached = tmp_path / "intervals_icu" / "activities_2026-10-14_2026-10-22.json"
    text = cached.read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    assert json.loads(text)["data"] == recorded("activities")

    offline_fake = FakeIntervals()
    with client(offline_fake, tmp_path, offline=True) as c:
        assert c.activities(D(2026, 10, 14), D(2026, 10, 22)) == recorded("activities")
        with pytest.raises(IntervalsError, match="no cached wellness"):
            c.wellness(D(2026, 10, 14), D(2026, 10, 22))
    assert offline_fake.calls == []


# ------------------------------------------------------------------ tidy rows


def test_activity_rows():
    rows = {r["id"]: r for r in (activity_row(a, frozenset({"2026-10-19"})) for a in recorded("activities"))}
    z2 = rows["i1001"]
    assert (z2["date"], z2["sport"], z2["moving_min"], z2["distance_km"], z2["np_watts"], z2["trainer"]) == (
        "2026-10-14", "bike", "60", "31.25", "204", "true"
    )
    assert {i: r["sport"] for i, r in rows.items()} == {
        "i1001": "bike",
        "i1002": "conditioning",  # WeightTraining, nothing in the strength log that day
        "i1003": "bike",
        "i1004": "handbike",
        "i1005": "grinding",  # Workout titled "Grinder"
        "i1006": "strength",  # WeightTraining on a logged day
        "i1007": "conditioning",  # HIIT
        "i1008": "sail",
        "i1009": "sail_or_other",
        "88123456": "run",
    }
    assert rows["i1009"]["review"] and not any(r["review"] for i, r in rows.items() if i != "i1009")
    assert rows["88123456"]["detail"] == "stub" and rows["i1001"]["detail"] == "full"


S = frozenset({"2026-10-19"})  # dates with logged strength sets


@pytest.mark.parametrize(
    ("name", "kind", "date", "sport", "mapped_by"),
    [
        ("Grinding repeats", "Workout", "2026-10-21", "grinding", "name"),
        ("grind 1+4 min test", "Rowing", "2026-10-22", "grinding", "name"),  # name beats type
        ("  GRIND", "HighIntensityIntervalTraining", "2026-10-21", "grinding", "name"),
        ("Grinder pedestal", "WeightTraining", "2026-10-19", "grinding", "name"),
        ("HIIT", "HIIT", "2026-10-17", "conditioning", "type"),
        ("HIIT", "HighIntensityIntervalTraining", "2026-10-17", "conditioning", "type"),
        ("Box WOD", "Crossfit", "2026-10-17", "conditioning", "type"),
        ("Strength", "WeightTraining", "2026-10-19", "strength", "type+log"),
        ("Strength", "WeightTraining", "2026-10-16", "conditioning", "type, no log"),
        ("Sailing", "Sail", "2026-10-18", "sail", "type"),
        ("Workout", "Workout", "2026-10-20", "sail_or_other", "type"),
        ("Afternoon Workout", "Workout", "2026-10-20", "sail_or_other", "type"),
        ("Ride", "VirtualRide", "2026-10-18", "bike", "type"),
        ("Hand-bike", "Handcycle", "2026-10-20", "handbike", "type"),
        ("Erg", "Rowing", "2026-10-20", "concept2", "type"),
        ("Swim", "Swim", "2026-10-20", "other", "type"),
        ("Engrind", "Workout", "2026-10-20", "sail_or_other", "type"),  # "grind" must start the title
    ],
)
def test_map_sport(name, kind, date, sport, mapped_by):
    got, by, review = map_sport(name, kind, date, S)
    assert (got, by) == (sport, mapped_by)
    assert bool(review) == (sport == "sail_or_other")


def test_wellness_rows():
    rows = {r["date"]: r for r in (wellness_row(w) for w in recorded("wellness"))}
    day = rows["2026-10-14"]
    assert (day["resting_hr"], day["hrv"], day["sleep_h"], day["weight_kg"], day["form"]) == ("49", "66", "7.25", "92.4", "3.8")
    assert day["garmin_readiness"] == ""  # nullable until Garmin sends it
    assert wellness_row({"id": "2026-10-14", "readiness": 64})["garmin_readiness"] == "64"
    assert rows["2026-10-17"]["resting_hr"] == "" and rows["2026-10-17"]["ctl"] == "53.2"


def test_add_readiness_over_rows(athlete):
    rows = add_readiness([wellness_row(w) for w in recorded("wellness")], athlete.readiness.rules)
    by_date = {r["date"]: r for r in rows}
    assert by_date["2026-10-05"]["readiness"] == ""  # no baseline yet
    assert by_date["2026-10-15"]["readiness"] == "green"
    assert by_date["2026-10-16"]["readiness"] == "amber"
    assert by_date["2026-10-16"]["readiness_reason"].startswith("amber: HRV 55 < 66.3 (baseline 68.7 - 1 SD 2.4)")
    assert by_date["2026-10-16"]["hrv_baseline"] == "68.7"
    assert by_date["2026-10-17"]["readiness"] == ""  # only CTL/ATL that day


def test_upsert_is_idempotent_and_replaces_the_range(tmp_path):
    path = tmp_path / "activities.csv"
    rows = [activity_row(a) for a in recorded("activities")]
    n = len(rows)
    lo, hi = D(2026, 10, 14), D(2026, 10, 22)
    assert upsert_range(path, ACTIVITY_FIELDS, "id", rows, lo, hi) == n
    assert upsert_range(path, ACTIVITY_FIELDS, "id", rows, lo, hi) == n  # re-run: no duplicates

    old = dict(rows[0], id="i0001", date="2026-10-01", start_local="2026-10-01T08:00:00")
    upsert_range(path, ACTIVITY_FIELDS, "id", [old], D(2026, 10, 1), D(2026, 10, 1))
    # a later fetch without i1005 (deleted upstream) drops it, keeps the row outside the range
    total = upsert_range(path, ACTIVITY_FIELDS, "id", [r for r in rows if r["id"] != "i1005"], lo, hi)
    with open(path, newline="", encoding="utf-8") as f:
        ids = [r["id"] for r in csv.DictReader(f)]
    assert total == n and "i1005" not in ids and ids[0] == "i0001"
    assert ids == sorted(set(ids), key=ids.index)  # no duplicates, date order kept


# ------------------------------------------------------------------ strength log


HEADER = "date,session,lift,role,kg,reps,rpe,notes\n"


@pytest.fixture
def plan(plan_data, athlete):
    return validate_plan(plan_data, athlete)


def write_log(tmp_path, body, header=HEADER):
    p = tmp_path / "strength.csv"
    p.write_text(header + body, encoding="utf-8")
    return p


def test_repo_strength_log_reads(plan, athlete):
    log = read_strength_log(ROOT / "training" / "logs" / "strength.csv", plan, athlete)
    assert len(log.sets) >= 2 and log.warnings == []
    tops = latest_top_sets(log)
    assert tops["bench_press"].kg == 100 and round(tops["bench_press"].e1rm_kg, 1) == 131.7


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ("2026-10-26,upper_me,bench,top,105,6,8,", "line 2, lift: unknown lift 'bench'"),
        ("2026-10-26,upper_max,bench_press,top,105,6,8,", "line 2, session: unknown session 'upper_max'"),
        ("2026-10-26,z2_90,bench_press,top,105,6,8,", "line 2, session: 'z2_90' is a bike session"),
        ("2026-10-26,upper_me,bench_press,heavy,105,6,8,", "line 2, role:"),
        ("2026-10-26,upper_me,bench_press,top,105,6,11,", "line 2, rpe:"),
        ("2026-10-26,upper_me,bench_press,top,-5,6,8,", "line 2, kg:"),
        ("2026-10-26,upper_me,bench_press,top,105,0,8,", "line 2, reps:"),
        ("2026-10-26,upper_me,bench_press,top,105,six,8,", "line 2, reps:"),
        ("26/10/2026,upper_me,bench_press,top,105,6,8,", "line 2, date:"),
        ("2026-10-26,upper_me,bench_press,top,105,6,8,a,b", "line 2: 1 extra cell"),
    ],
)
def test_bad_log_rows_name_line_and_column(tmp_path, plan, athlete, row, expected):
    with pytest.raises(StrengthLogError) as exc:
        read_strength_log(write_log(tmp_path, row + "\n"), plan, athlete)
    assert any(e.startswith(expected) for e in exc.value.errors), exc.value.errors


def test_bad_header(tmp_path, plan, athlete):
    with pytest.raises(StrengthLogError, match="1 error") as exc:
        read_strength_log(write_log(tmp_path, "", header="date,lift,kg,reps\n"), plan, athlete)
    assert exc.value.errors[0].startswith("line 1: header must be")


def test_all_bad_rows_are_reported(tmp_path, plan, athlete):
    body = "2026-10-26,upper_me,bench,top,105,6,8,\n2026-10-26,upper_me,bench_press,top,105,6,8,\n2026-10-27,nope,bench_press,top,1,1,,\n"
    with pytest.raises(StrengthLogError) as exc:
        read_strength_log(write_log(tmp_path, body), plan, athlete)
    assert [e.split(",")[0] for e in exc.value.errors] == ["line 2", "line 4"]


def test_soft_mismatches_are_warnings(tmp_path, plan, athlete):
    body = (
        "2026-10-26,upper_me,push_press,work,50,8,7,extra\n"  # not in upper_me
        "2026-10-27,upper_me,bench_press,top,105,6,8,moved a day\n"  # upper_me is Monday
    )
    log = read_strength_log(write_log(tmp_path, body), plan, athlete)
    assert log.warnings == [
        "line 2: push_press is not part of session upper_me",
        "line 3: upper_me is not scheduled on 2026-10-27 in the plan",
    ]


def test_latest_top_set_per_lift(tmp_path, plan, athlete):
    body = (
        "2026-10-26,upper_me,bench_press,top,105,6,8,\n"
        "2026-10-26,upper_me,bench_press,backoff,90,8,,\n"
        "2026-11-02,upper_me,bench_press,top,107.5,6,8,\n"
        "2026-11-02,upper_me,bench_press,backoff,92.5,8,,\n"
        "2026-11-05,upper_re,bench_press,work,82.5,12,8,\n"  # later, but not a top set
        "2026-10-26,upper_me,weighted_pullup,work,10,8,,\n"
        "2026-11-02,upper_me,weighted_pullup,work,12.5,6,,\n"
        "2026-11-02,upper_me,weighted_pullup,work,12.5,7,,\n"
    )
    tops = latest_top_sets(read_strength_log(write_log(tmp_path, body), plan, athlete))
    assert (tops["bench_press"].date, tops["bench_press"].kg) == (D(2026, 11, 2), 107.5)
    assert (tops["weighted_pullup"].kg, tops["weighted_pullup"].reps) == (12.5, 7)  # no top role: heaviest latest


def test_blank_rpe_is_none(tmp_path, plan, athlete):
    log = read_strength_log(write_log(tmp_path, "2026-10-21,test_lower,trap_bar_deadlift,test,180,6,,\n"), plan, athlete)
    assert log.sets[0][1].rpe is None


# ------------------------------------------------------------------ CLI


def run(tmp_path, *extra, env=FAKE_ENV, fake=None):
    fake = fake or FakeIntervals()
    argv = ["--from", "2026-10-14", "--to", "2026-10-22", "--out", str(tmp_path / "derived"), "--cache", str(tmp_path / "cache"), *extra]
    return main(argv, env=env, transport=fake.transport, sleep=lambda s: None), fake


def test_cli_end_to_end_and_rerun(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    code, fake = run(tmp_path)
    out = capsys.readouterr().out
    assert code == 0, out
    assert (
        "Activities: 10 (bike 2, conditioning 2, handbike 1, grinding 1, strength 1, sail 1, sail_or_other 1, run 1)"
    ) in out
    assert "review: 2026-10-20 'Workout' [Workout] -> sail_or_other: plain Workout without a Grind title" in out
    assert out.count("review:") == 1
    assert "1 came from Strava without detail" in out
    assert "Wellness: 4 days in range" in out and "(13 rows incl. baseline)" in out
    assert "latest 2026-10-16: resting HR 50 bpm, HRV 55 ms, sleep 6.8 h (score 71)" in out
    assert "  readiness amber: HRV 55 < 66.3" in out
    assert "bench_press        2026-10-19  100 kg x 8 @ RPE 8.5  (amrap)  e1RM 131.7 kg" in out
    assert FAKE_KEY not in out
    assert len(fake.calls) == 2
    assert dict(fake.calls[1].url.params)["oldest"] == "2026-09-16"  # wellness fetched 28 days early

    code, _ = run(tmp_path)
    assert code == 0
    derived = tmp_path / "derived"
    for name, n in (("activities.csv", 10), ("wellness.csv", 13)):
        with open(derived / name, newline="", encoding="utf-8") as f:
            assert len(list(csv.DictReader(f))) == n
    assert (derived / "strength.csv").exists()
    assert not any(FAKE_KEY in p.read_text(encoding="utf-8") for p in tmp_path.rglob("*") if p.is_file())


def test_cli_missing_secret(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    code, fake = run(tmp_path, env={"INTERVALS_ICU_API_KEY": FAKE_KEY})
    err = capsys.readouterr().err
    assert code == 2 and "INTERVALS_ICU_ATHLETE_ID is not set" in err
    assert fake.calls == []


def test_cli_bad_log_stops_before_network(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    bad = write_log(tmp_path, "2026-10-26,upper_me,bench,top,105,6,8,\n")
    code, fake = run(tmp_path, "--log", str(bad))
    assert code == 1 and fake.calls == []
    assert "line 2, lift: unknown lift 'bench'" in capsys.readouterr().out


def test_cli_offline_uses_cache(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    run(tmp_path)
    code, fake = run(tmp_path, "--offline")
    assert code == 0 and fake.calls == []
