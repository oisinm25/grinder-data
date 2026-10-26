"""Time in zones per activity: derived/zones.csv (activity_id, date, sport, kind, zone, seconds, source).

intervals.icu sends `icu_zone_times` (power: [{id: "Z1".."Z7", secs}, plus "SS", sweet spot,
which overlaps Z3/Z4 and is dropped) and `icu_hr_zone_times` (HR: 7 ints) on each activity, but
it works them out from its own anchors (its FTP and LTHR), not athlete.yaml's. So when the
activity's streams can be had, the seconds are recomputed here (source "streams"):

- power, bike and hand-bike only, against that modality's FTP in athlete.yaml, using
  intervals.icu's 7 power zones: tops at 55, 75, 90, 105, 120 and 150% of FTP;
- HR, every sport, against lthr_bpm, using Friel's 7 LTHR zones (intervals.icu's default):
  tops at 81, 90, 94, 100, 103 and 106% of LTHR.

Otherwise intervals.icu's own seconds are kept (source "icu"). An activity with neither has no rows.
A value exactly on a zone's top goes to the zone above.
"""

from collections.abc import Callable
from typing import Any

from pipeline.athlete import Athlete

ZONE_FIELDS = ("activity_id", "date", "sport", "kind", "zone", "seconds", "source")
POWER_TOPS = (0.55, 0.75, 0.90, 1.05, 1.20, 1.50)
HR_TOPS = (0.81, 0.90, 0.94, 1.00, 1.03, 1.06)
POWER_SPORTS = {"bike": "bike", "handbike": "handbike"}  # sport -> athlete.yaml ftp modality
MAX_GAP_S = 5  # a longer gap between samples is a pause: only its first second counts


def icu_seconds(a: dict[str, Any]) -> dict[str, list[int]]:
    """{"power": [7 ints], "hr": [7 ints]} from intervals.icu's fields, for the kinds it sent."""
    out = {}
    power = a.get("icu_zone_times")
    if power:
        by_id = {z.get("id"): int(z.get("secs") or 0) for z in power if isinstance(z, dict)}
        if any(f"Z{i}" in by_id for i in range(1, 8)):
            out["power"] = [by_id.get(f"Z{i}", 0) for i in range(1, 8)]
    hr = a.get("icu_hr_zone_times")
    if hr:
        out["hr"] = [int(x or 0) for x in list(hr)[:7]] + [0] * max(0, 7 - len(hr))
    return out


def stream_seconds(time: list, values: list, tops: list[float]) -> list[int] | None:
    """Seconds in each of 7 zones (len(tops) == 6 tops in the stream's unit), or None with no samples."""
    pairs = [(t, v) for t, v in zip(time or [], values or []) if t is not None and v is not None]
    if len(pairs) < 2:
        return None
    secs = [0] * 7

    def zone(v: float) -> int:
        return next((i for i, top in enumerate(tops) if v < top), 6)

    for (t0, v0), (t1, _) in zip(pairs, pairs[1:]):
        gap = t1 - t0
        if gap <= 0:
            continue
        secs[zone(v0)] += gap if gap <= MAX_GAP_S else 1
    secs[zone(pairs[-1][1])] += 1
    return [round(s) for s in secs]


def activity_zone_rows(row: dict[str, str], raw: dict[str, Any], athlete: Athlete,
                       streams: Callable[[str], dict | None] | None = None) -> list[dict[str, str]]:  # fmt: skip
    """zones.csv rows for one activity. `row` is its tidy activities.csv row (for id, date, sport),
    `streams(id)` returns {"time", "watts", "heartrate"} or None (not available)."""
    icu = icu_seconds(raw)
    data = streams(row["id"]) if streams and icu else None  # no zone data at all: don't fetch
    got: dict[str, tuple[list[int], str]] = {k: (v, "icu") for k, v in icu.items()}
    if data:
        modality = POWER_SPORTS.get(row["sport"])
        if modality and modality in athlete.ftp:
            ftp = athlete.ftp[modality].watts
            s = stream_seconds(data.get("time"), data.get("watts"), [ftp * p for p in POWER_TOPS])
            if s and any(s):
                got["power"] = (s, "streams")
        s = stream_seconds(data.get("time"), [v or None for v in data.get("heartrate") or []], [athlete.lthr_bpm * p for p in HR_TOPS])
        if s and any(s):
            got["hr"] = (s, "streams")
    if not POWER_SPORTS.get(row["sport"]):
        got.pop("power", None)  # power zones only mean something against an FTP anchor
    return [
        {"activity_id": row["id"], "date": row["date"], "sport": row["sport"], "kind": kind,
         "zone": str(z + 1), "seconds": str(secs[z]), "source": source}  # fmt: skip
        for kind in ("power", "hr") if kind in got
        for secs, source in [got[kind]]
        for z in range(7)
    ]


def merge_zone_rows(existing: list[dict[str, str]], new: list[dict[str, str]], ids: set[str],
                    oldest: str, newest: str) -> list[dict[str, str]]:  # fmt: skip
    """Replace the rows of the activities fetched this time (`ids`, dated oldest..newest), keep the rest.
    Activities in the range that no longer exist upstream lose their rows."""
    kept = [r for r in existing if r["activity_id"] not in ids and not oldest <= r["date"] <= newest]
    return sorted(kept + new, key=lambda r: (r["date"], r["activity_id"], r["kind"], int(r["zone"])))
