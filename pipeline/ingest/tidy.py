"""Tidy rows from raw intervals.icu records and the strength log, and the CSV writers.

Re-running never duplicates. Activities and wellness are keyed by activity id and by date.
A fetch replaces every row inside its date range (so an activity deleted upstream
disappears) and keeps rows outside it. The strength CSV is rebuilt from the whole log each run.
"""

import csv
import datetime as dt
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pipeline.ingest.strength import StrengthLog

SPORTS = {
    "bike": {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide", "EMountainBikeRide", "TrackRide", "Velomobile"},
    "handbike": {"Handcycle"},
    "run": {"Run", "VirtualRun", "TrailRun"},
    "concept2": {"Rowing", "VirtualRow"},
    "strength": {"WeightTraining"},
    "walk": {"Walk", "Hike"},
}
SPORT_OF = {t: sport for sport, types in SPORTS.items() for t in types}

ACTIVITY_FIELDS = (
    "id", "date", "start_local", "sport", "type", "name", "moving_min", "elapsed_min", "distance_km",
    "load", "avg_watts", "np_watts", "avg_hr", "max_hr", "intensity", "trainer", "source", "detail",
)  # fmt: skip
WELLNESS_FIELDS = (
    "date", "resting_hr", "hrv", "hrv_sdnn", "sleep_h", "sleep_score", "sleep_quality", "readiness",
    "weight_kg", "ctl", "atl", "form", "soreness", "fatigue", "stress", "mood", "motivation", "spo2", "steps",
)  # fmt: skip
STRENGTH_FIELDS = ("date", "session", "lift", "role", "set_no", "kg", "reps", "rpe", "e1rm_kg", "notes", "log_line")
MEASURED_WELLNESS = ("resting_hr", "hrv", "hrv_sdnn", "sleep_h", "sleep_score", "readiness")


def _num(v: Any, scale: float = 1.0, digits: int = 1) -> str:
    if v is None or v == "":
        return ""
    try:
        x = float(v) / scale
    except (TypeError, ValueError):
        return ""
    return f"{round(x, digits):g}"


def activity_row(a: dict[str, Any]) -> dict[str, str]:
    start = str(a.get("start_date_local") or "")
    kind = str(a.get("type") or "")
    # Strava-sourced activities come through the API as stubs without detail.
    stub = "_note" in a or (a.get("source") == "STRAVA" and a.get("moving_time") is None)
    return {
        "id": str(a.get("id", "")),
        "date": start[:10],
        "start_local": start,
        "sport": SPORT_OF.get(kind, "other"),
        "type": kind,
        "name": str(a.get("name") or ""),
        "moving_min": _num(a.get("moving_time"), 60),
        "elapsed_min": _num(a.get("elapsed_time"), 60),
        "distance_km": _num(a.get("distance"), 1000, 2),
        "load": _num(a.get("icu_training_load"), digits=0),
        "avg_watts": _num(a.get("icu_average_watts", a.get("average_watts")), digits=0),
        "np_watts": _num(a.get("icu_weighted_avg_watts"), digits=0),
        "avg_hr": _num(a.get("average_heartrate"), digits=0),
        "max_hr": _num(a.get("max_heartrate"), digits=0),
        "intensity": _num(a.get("icu_intensity")),
        "trainer": "" if a.get("trainer") is None else str(bool(a.get("trainer"))).lower(),
        "source": str(a.get("source") or ""),
        "detail": "stub" if stub else "full",
    }


def wellness_row(w: dict[str, Any]) -> dict[str, str]:
    ctl, atl = w.get("ctl"), w.get("atl")
    return {
        "date": str(w.get("id", ""))[:10],
        "resting_hr": _num(w.get("restingHR"), digits=0),
        "hrv": _num(w.get("hrv")),
        "hrv_sdnn": _num(w.get("hrvSDNN")),
        "sleep_h": _num(w.get("sleepSecs"), 3600, 2),
        "sleep_score": _num(w.get("sleepScore"), digits=0),
        "sleep_quality": _num(w.get("sleepQuality"), digits=0),
        "readiness": _num(w.get("readiness"), digits=0),
        "weight_kg": _num(w.get("weight")),
        "ctl": _num(ctl),
        "atl": _num(atl),
        "form": _num(ctl - atl) if isinstance(ctl, (int, float)) and isinstance(atl, (int, float)) else "",
        "soreness": _num(w.get("soreness"), digits=0),
        "fatigue": _num(w.get("fatigue"), digits=0),
        "stress": _num(w.get("stress"), digits=0),
        "mood": _num(w.get("mood"), digits=0),
        "motivation": _num(w.get("motivation"), digits=0),
        "spo2": _num(w.get("spO2")),
        "steps": _num(w.get("steps"), digits=0),
    }


def strength_rows(log: StrengthLog) -> list[dict[str, str]]:
    rows, counter = [], {}
    for line, s in sorted(log.sets, key=lambda ls: (ls[1].date, ls[0])):
        key = (s.date, s.session, s.lift)
        counter[key] = counter.get(key, 0) + 1
        rows.append({
            "date": s.date.isoformat(),
            "session": s.session,
            "lift": s.lift,
            "role": s.role,
            "set_no": str(counter[key]),
            "kg": f"{s.kg:g}",
            "reps": str(s.reps),
            "rpe": "" if s.rpe is None else f"{s.rpe:g}",
            "e1rm_kg": f"{round(s.e1rm_kg, 1):g}",
            "notes": s.notes,
            "log_line": str(line),
        })  # fmt: skip
    return rows


# ------------------------------------------------------------------ writers


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: Iterable[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, list(fields), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def upsert_range(
    path: Path, fields: tuple[str, ...], key: str, rows: list[dict[str, str]], oldest: dt.date, newest: dt.date
) -> int:
    """Replace the rows dated oldest..newest with `rows`, keep the rest, one row per key. Returns the total."""
    lo, hi = oldest.isoformat(), newest.isoformat()
    kept = [r for r in read_csv(path) if not lo <= r.get("date", "") <= hi]
    merged = {r[key]: r for r in kept}
    merged.update({r[key]: r for r in rows})
    ordered = sorted(merged.values(), key=lambda r: (r.get("date", ""), r.get("start_local", ""), r[key]))
    write_csv(path, fields, ordered)
    return len(ordered)


def latest_measured_wellness(rows: list[dict[str, str]]) -> dict[str, str] | None:
    """The latest day with a real measurement (intervals.icu fills CTL/ATL for every day)."""
    measured = [r for r in rows if any(r.get(f) for f in MEASURED_WELLNESS)]
    pool = measured or rows
    return max(pool, key=lambda r: r["date"]) if pool else None
