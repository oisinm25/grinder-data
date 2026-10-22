import datetime as dt

import pytest
from pydantic import ValidationError

from pipeline.athlete import ReadinessRules
from pipeline.readiness import DayInput, assess

TODAY = dt.date(2026, 10, 22)
# Baseline HRV 61/65/69 -> mean 65, SD 4 -> floor 61. Resting HR 50 -> ceiling 55.
RULES = ReadinessRules(baseline_days=28, min_baseline_days=3, hrv_sd_below=1, resting_hr_above_bpm=5, sleep_min_h=6)


def history(hrvs=(61, 65, 69), rhrs=(50, 50, 50), days_back=1):
    return [
        DayInput(TODAY - dt.timedelta(days=days_back + i), hrv, rhr, 7.5)
        for i, (hrv, rhr) in enumerate(zip(hrvs, rhrs))
    ]


def today(hrv=65, rhr=50, sleep=7.5, past=None, rules=RULES):
    days = (history() if past is None else past) + [DayInput(TODAY, hrv, rhr, sleep)]
    return next(a for a in assess(days, rules) if a.date == TODAY)


def test_green_with_reason():
    a = today()
    assert a.status == "green"
    assert a.reason == (
        "green: HRV 65 >= 61.0 (baseline 65.0 - 1 SD 4.0); "
        "resting HR 50 <= 55.0 (baseline 50.0 + 5); sleep 7.5 h >= 6"
    )
    assert (a.hrv_baseline, a.hrv_sd, a.resting_hr_baseline) == (65.0, 4.0, 50.0)


@pytest.mark.parametrize(
    ("hrv", "rhr", "sleep", "status"),
    [
        (60.9, 50, 7.5, "amber"),  # HRV below baseline - 1 SD
        (61, 50, 7.5, "green"),  # exactly at the floor is not below it
        (65, 55.1, 7.5, "amber"),  # resting HR more than 5 above
        (65, 55, 7.5, "green"),  # exactly 5 above is not more than 5
        (60, 56, 7.5, "red"),  # both amber triggers
        (65, 50, 5.9, "red"),  # sleep under 6 h alone
        (65, 50, 6.0, "green"),  # 6 h is not under 6 h
        (60, 50, 5.5, "red"),  # sleep wins over a single amber trigger
    ],
)
def test_thresholds(hrv, rhr, sleep, status):
    assert today(hrv, rhr, sleep).status == status


def test_reason_names_the_trigger():
    assert today(60, 50).reason.startswith("amber: HRV 60 < 61.0")
    assert "resting HR 56 > 55.0" in today(65, 56).reason
    assert today(65, 50, 5.9).reason.endswith("sleep 5.9 h < 6")


def test_missing_hrv_is_no_call_unless_something_triggers():
    a = today(hrv=None)
    assert (a.status, a.reason.split(";")[0]) == ("", "no call: no HRV")
    assert today(hrv=None, rhr=56).status == "amber"
    assert today(hrv=None, rhr=None, sleep=5).status == "red"


def test_missing_sleep_does_not_block_a_call():
    a = today(sleep=None)
    assert a.status == "green" and a.reason.endswith("no sleep")


def test_short_baseline_is_no_call():
    a = today(past=history(hrvs=(61, 65), rhrs=(50, 50)))
    assert a.status == ""
    assert "HRV baseline 2/3 days" in a.reason and "resting HR baseline 2/3 days" in a.reason


def test_baseline_window_excludes_today_and_old_days():
    past = history() + [DayInput(TODAY - dt.timedelta(days=29), 20, 90, 7.5)]  # outside 28 days
    assert today(past=past).hrv_baseline == 65.0
    assert today(hrv=40).hrv_baseline == 65.0  # today's value never moves its own baseline


def test_every_day_is_judged_against_the_days_before_it():
    days = history(hrvs=(61, 65, 69, 65), rhrs=(50, 50, 50, 50), days_back=1)
    results = assess(days, RULES)
    assert [a.status != "" for a in results] == [False, False, False, True]  # oldest three lack a 3-day baseline


def test_thresholds_come_from_rules():
    looser = RULES.model_copy(update={"hrv_sd_below": 2.0, "resting_hr_above_bpm": 8.0, "sleep_min_h": 5.0})
    assert today(58, 57, 5.5, rules=looser).status == "green"
    assert today(58, 57, 5.5).status == "red"


def test_rules_validation():
    with pytest.raises(ValidationError, match="more than baseline_days"):
        ReadinessRules(baseline_days=5, min_baseline_days=7, hrv_sd_below=1, resting_hr_above_bpm=5, sleep_min_h=6)
    with pytest.raises(ValidationError, match="at least 2"):
        ReadinessRules(baseline_days=28, min_baseline_days=1, hrv_sd_below=1, resting_hr_above_bpm=5, sleep_min_h=6)
