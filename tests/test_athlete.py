import pytest
from pydantic import ValidationError

from pipeline.athlete import Athlete
from pipeline.plan.schema import format_errors


def errors_for(data) -> list[str]:
    with pytest.raises(ValidationError) as exc:
        Athlete.model_validate(data)
    return format_errors(exc.value, data)


def test_repo_athlete_values(athlete):
    assert athlete.ftp["bike"].watts == 300
    assert athlete.ftp["handbike"].watts == 252 and athlete.ftp["handbike"].provisional
    assert not athlete.ftp["bike"].provisional
    assert athlete.lthr_bpm == 170
    assert athlete.curves["grinding_60s"].watts == 470
    assert athlete.curves["grinding_240s"].watts == 301
    assert athlete.strength.rounding_kg == 2.5
    amber, red = athlete.readiness.amber, athlete.readiness.red
    assert amber.hold_last_weight and not amber.allow_increment and amber.rpe_cap == 8
    assert red.pct_of_last_weight == 0.9 and red.rpe_cap == 7


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
    ],
)
def test_invalid_athlete_names_the_field(athlete_data, path, value, field):
    node = athlete_data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    errors = errors_for(athlete_data)
    assert any(e.split(": ", 1)[0] == field for e in errors), errors
