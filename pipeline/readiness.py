"""Daily readiness: green, amber or red from HRV, resting HR and sleep.

Each day is judged against a rolling baseline: the mean (and, for HRV, the SD) of the
measured values in the `baseline_days` before it, the day itself excluded. With the
thresholds in `athlete.yaml` `readiness.rules`:

- amber: HRV below baseline - `hrv_sd_below` SD, or resting HR more than
  `resting_hr_above_bpm` above baseline
- red:   both amber triggers at once, or sleep under `sleep_min_h`
- green: HRV and resting HR both measured and neither triggers

There's no call (blank status) when HRV or resting HR can't be judged (not measured that
day, or fewer than `min_baseline_days` measured days in the window) and nothing else
already makes the day amber or red. Every result carries the reason, with the numbers.
"""

import datetime as dt
import statistics
from dataclasses import dataclass

from pipeline.athlete import ReadinessRules


@dataclass(frozen=True)
class DayInput:
    date: dt.date
    hrv: float | None
    resting_hr: float | None
    sleep_h: float | None


@dataclass(frozen=True)
class Assessment:
    date: dt.date
    status: str  # green | amber | red | "" (no call)
    reason: str
    hrv_baseline: float | None = None
    hrv_sd: float | None = None
    resting_hr_baseline: float | None = None


def assess(days: list[DayInput], rules: ReadinessRules) -> list[Assessment]:
    days = sorted(days, key=lambda d: d.date)
    return [_assess_day(day, days, rules) for day in days]


def _assess_day(day: DayInput, days: list[DayInput], rules: ReadinessRules) -> Assessment:
    start = day.date - dt.timedelta(days=rules.baseline_days)
    window = [d for d in days if start <= d.date < day.date]
    hrvs = [d.hrv for d in window if d.hrv is not None]
    rhrs = [d.resting_hr for d in window if d.resting_hr is not None]
    need = rules.min_baseline_days
    notes: list[str] = []

    hrv_low = hrv_mean = hrv_sd = None
    if len(hrvs) >= need:
        hrv_mean, hrv_sd = statistics.fmean(hrvs), statistics.stdev(hrvs)
    if day.hrv is None:
        notes.append("no HRV")
    elif hrv_mean is None:
        notes.append(f"HRV baseline {len(hrvs)}/{need} days")
    else:
        floor = hrv_mean - rules.hrv_sd_below * hrv_sd
        hrv_low = day.hrv < floor
        notes.append(
            f"HRV {day.hrv:g} {'<' if hrv_low else '>='} {floor:.1f} "
            f"(baseline {hrv_mean:.1f} - {rules.hrv_sd_below:g} SD {hrv_sd:.1f})"
        )

    rhr_high = rhr_mean = None
    if len(rhrs) >= need:
        rhr_mean = statistics.fmean(rhrs)
    if day.resting_hr is None:
        notes.append("no resting HR")
    elif rhr_mean is None:
        notes.append(f"resting HR baseline {len(rhrs)}/{need} days")
    else:
        ceiling = rhr_mean + rules.resting_hr_above_bpm
        rhr_high = day.resting_hr > ceiling
        notes.append(
            f"resting HR {day.resting_hr:g} {'>' if rhr_high else '<='} {ceiling:.1f} "
            f"(baseline {rhr_mean:.1f} + {rules.resting_hr_above_bpm:g})"
        )

    short_sleep = None
    if day.sleep_h is None:
        notes.append("no sleep")
    else:
        short_sleep = day.sleep_h < rules.sleep_min_h
        notes.append(f"sleep {day.sleep_h:g} h {'<' if short_sleep else '>='} {rules.sleep_min_h:g}")

    if short_sleep or (hrv_low and rhr_high):
        status = "red"
    elif hrv_low or rhr_high:
        status = "amber"
    elif hrv_low is None or rhr_high is None:
        status = ""
    else:
        status = "green"

    return Assessment(
        day.date,
        status,
        f"{status or 'no call'}: " + "; ".join(notes),
        None if hrv_mean is None else round(hrv_mean, 1),
        None if hrv_sd is None else round(hrv_sd, 1),
        None if rhr_mean is None else round(rhr_mean, 1),
    )
