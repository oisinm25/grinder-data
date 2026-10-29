"""Design-rule warnings for an expanded stretch of days (training/design-rules.md).

Run when a session change is added (python -m pipeline.plan.changes). They warn, never block:

- strength and endurance under 6 h apart: both in the same half-day slot (AM and PM count as
  6+ h apart)
- two upper max-effort sessions within 48 h
- bike VO2 and hand-bike VO2 within 48 h (VO2: a work step at 105%+ of that modality's FTP)

Slot times follow the calendar: AM 07:00, PM 17:00.
"""

import datetime as dt
from itertools import combinations

from pipeline.athlete import Athlete
from pipeline.plan.expand import ENDURANCE_MODALITIES, Day, ScheduledSession

HOUR = {"am": 7, "pm": 17}
VO2_PCT = 1.05


def _at(s: ScheduledSession) -> dt.datetime:
    return dt.datetime.combine(s.date, dt.time(HOUR[s.slot[-2:]]))


def _label(s: ScheduledSession) -> str:
    return f"{s.session_id} ({s.date:%a %d %b} {s.slot[-2:].upper()})"


def is_upper_max_effort(s: ScheduledSession) -> bool:
    return s.session.modality == "strength" and s.session.summary.lower().startswith("upper max effort")


def _work_watts(steps):
    for st in steps:
        if st.kind == "repeat":
            yield from _work_watts(st.steps)
        elif st.kind in ("work", "test") and st.target.watts:
            yield st.target.watts[0]


def is_vo2(s: ScheduledSession, athlete: Athlete) -> bool:
    ftp = athlete.ftp.get(s.session.modality)
    # 1% slack: watts are rounded from the percentage, e.g. 1.05 x 252 = 264.6 -> 265
    return bool(ftp) and any(w >= (VO2_PCT - 0.01) * ftp.watts for w in _work_watts(s.steps))


def rule_warnings(days: list[Day], athlete: Athlete, around: set[dt.date] | None = None) -> list[str]:
    """Warnings for pairs of sessions that break a rule; with `around`, only pairs touching those dates."""
    sessions = [s for d in days for s in d.sessions if s.session.modality not in ("rest", "mobility")]
    out = []
    for a, b in combinations(sessions, 2):
        if around is not None and a.date not in around and b.date not in around:
            continue
        gap = abs(_at(a) - _at(b))
        kinds = {a.session.modality, b.session.modality}
        if a.date == b.date and a.slot == b.slot and "strength" in kinds and kinds & set(ENDURANCE_MODALITIES):
            out.append(f"strength and endurance under 6 h apart: {_label(a)} and {_label(b)}")
        if is_upper_max_effort(a) and is_upper_max_effort(b) and gap < dt.timedelta(hours=48):
            out.append(f"two upper max-effort sessions within 48 h: {_label(a)} and {_label(b)}")
        if kinds == {"bike", "handbike"} and is_vo2(a, athlete) and is_vo2(b, athlete) and gap < dt.timedelta(hours=48):
            out.append(f"bike VO2 and hand-bike VO2 within 48 h: {_label(a)} and {_label(b)}")
    return out
