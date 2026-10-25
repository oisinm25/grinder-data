import pytest
from pydantic import ValidationError

from pipeline.athlete import Athlete, load_athlete
from pipeline.plan.schema import format_errors
from tests.conftest import REAL_ATHLETE


def errors_for(data) -> list[str]:
    with pytest.raises(ValidationError) as exc:
        Athlete.model_validate(data)
    return format_errors(exc.value, data)


def test_repo_athlete_values():
    """The real config/athlete.yaml (the rest of the suite uses frozen anchors in tests/fixtures/)."""
    athlete = load_athlete(REAL_ATHLETE)
    assert athlete.ftp["bike"].watts == 340  # 20-min test 361 W on 25 Oct 2026
    assert athlete.ftp["handbike"].watts == 252 and athlete.ftp["handbike"].provisional
    assert not athlete.ftp["bike"].provisional
    assert athlete.lthr_bpm == 170
    assert athlete.curves["grinding_60s"].watts == 470
    assert athlete.curves["grinding_240s"].watts == 301
    assert athlete.strength.rounding_kg == 2.5
    amber, red = athlete.readiness.amber, athlete.readiness.red
    assert amber.hold_last_weight and not amber.allow_increment and amber.rpe_cap == 8
    assert red.pct_of_last_weight == 0.9 and red.rpe_cap == 7
    rules = athlete.readiness.rules
    assert (rules.baseline_days, rules.hrv_sd_below, rules.resting_hr_above_bpm, rules.sleep_min_h) == (28, 1, 5, 6)
    assert (athlete.schedule.timezone, athlete.schedule.window) == ("Europe/Madrid", ("06:30", "07:00"))


def test_schedule_window_is_inclusive(athlete):
    import datetime as dt

    tz = athlete.schedule.zone()
    at = lambda h, m: dt.datetime(2026, 10, 26, h, m, tzinfo=tz)  # noqa: E731
    assert [athlete.schedule.contains(at(*t)) for t in ((6, 29), (6, 30), (7, 0), (7, 1))] == [False, True, True, False]


@pytest.mark.parametrize(
    ("path", "value", "field"),
    [
        (["readiness", "red", "pct_of_last_weight"], 1.2, "readiness.red.pct_of_last_weight"),
        (["readiness", "amber", "rpe_cap"], 12, "readiness.amber.rpe_cap"),
        (["ftp", "rowing"], {"watts": 200}, "ftp.rowing"),
        (["ftp", "bike", "watts"], 0, "ftp.bike.watts"),
        (["lthr_bpm"], -1, "lthr_bpm"),
        (["strength", "rounding_kg"], 0, "strength.rounding_kg"),
        (["strength", "lifts", "Bench Press"], {"name": "x"}, "strength.lifts.Bench Press"),
        (["curves", "grinding_90s"], {"duration_s": 60, "watts": 400}, "curves"),
        (["secret_key"], "x", "secret_key"),
        (["schedule", "timezone"], "Europe/Atlantis", "schedule.timezone"),
        (["schedule", "window"], ["07:00", "06:30"], "schedule.window"),
        (["schedule", "window"], ["6:30", "07:00"], "schedule.window[0]"),
    ],
)
def test_invalid_athlete_names_the_field(athlete_data, path, value, field):
    node = athlete_data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    errors = errors_for(athlete_data)
    assert any(e.split(": ", 1)[0] == field for e in errors), errors
