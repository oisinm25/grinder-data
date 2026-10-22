"""Workout encodings: intervals.icu workout text, Zwift .zwo, Garmin .fit.

All three take the resolved steps from pipeline.plan.expand, so targets are the watts
resolved from athlete.yaml (the authority), not whatever FTP the device holds.
"""

import datetime as dt
import struct
from xml.sax.saxutils import escape, quoteattr

from pipeline.plan.expand import ResolvedStep, ResolvedTarget

# ------------------------------------------------------------------ intervals.icu workout text

CUES = {"warmup": "Warmup", "cooldown": "Cooldown", "rest": "Easy", "test": "TEST", "work": "", "steady": ""}


def text_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    if not m:
        return f"{s}s"
    return f"{m}m" if not s else f"{m}m{s}"


def text_target(t: ResolvedTarget) -> str:
    out = ""
    if t.watts:
        lo, hi = t.watts
        out = f"{lo}w" if lo == hi else f"{lo}-{hi}w"
    elif t.hr_zone is not None:
        out = f"Z{t.hr_zone} HR"
    if t.cadence:
        out += f" {t.cadence[0]:g}-{t.cadence[1]:g}rpm"
    return out.strip()


def workout_text(steps: tuple[ResolvedStep, ...]) -> list[str]:
    """Lines in the intervals.icu builder syntax: `- [cue] 10m 195-216w`, `Main set 5x`."""
    lines: list[str] = []
    for s in steps:
        if s.kind == "repeat":
            if lines and lines[-1]:
                lines.append("")
            lines.append(f"Main set {s.count}x")
            lines.extend(workout_text(s.steps))
            lines.append("")
            continue
        cue = CUES[s.kind]
        if s.target.rpe is not None:
            cue = f"{cue} RPE {s.target.rpe:g}".strip()
        parts = ["-", cue, text_duration(s.duration_s), text_target(s.target)]
        line = " ".join(p for p in parts if p)
        if s.note:
            line += f", {s.note}"
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return lines


# ------------------------------------------------------------------ Zwift .zwo (bike only)


def _power(t: ResolvedTarget, ftp_w: float) -> float | None:
    """Midpoint as a fraction of FTP, or None for an effort-only step."""
    if not t.watts:
        return None
    return round((t.watts[0] + t.watts[1]) / 2 / ftp_w, 3)


def _zwo_steps(steps, ftp_w: float, out: list[str]) -> None:
    for s in steps:
        if s.kind == "repeat":
            on, off = (s.steps + (None, None))[:2]
            simple = (
                len(s.steps) == 2 and on.kind != "repeat" and off.kind != "repeat"
                and _power(on.target, ftp_w) is not None and _power(off.target, ftp_w) is not None
            )  # fmt: skip
            if simple:
                out.append(
                    f'    <IntervalsT Repeat="{s.count}" OnDuration="{on.duration_s}" OffDuration="{off.duration_s}" '
                    f'OnPower="{_power(on.target, ftp_w)}" OffPower="{_power(off.target, ftp_w)}"/>'
                )
            else:
                for _ in range(s.count):
                    _zwo_steps(s.steps, ftp_w, out)
            continue
        p = _power(s.target, ftp_w)
        cue = f" <textevent timeoffset=\"0\" message={quoteattr(s.note)}/>" if s.note else ""
        if p is None:
            out.append(f'    <FreeRide Duration="{s.duration_s}">{cue}</FreeRide>' if cue else f'    <FreeRide Duration="{s.duration_s}"/>')
        else:
            lo, hi = (round(w / ftp_w, 3) for w in s.target.watts)
            attrs = f'Duration="{s.duration_s}" Power="{p}" PowerLow="{lo}" PowerHigh="{hi}"'
            if s.target.cadence:
                attrs += f' Cadence="{round(sum(s.target.cadence) / 2)}"'
            out.append(f"    <SteadyState {attrs}>{cue}</SteadyState>" if cue else f"    <SteadyState {attrs}/>")


def zwo(name: str, description: str, steps, ftp_w: float) -> str:
    body: list[str] = []
    _zwo_steps(steps, ftp_w, body)
    return "\n".join([
        "<workout_file>",
        "  <author>Pedestal</author>",
        f"  <name>{escape(name)}</name>",
        f"  <description>{escape(description)}</description>",
        "  <sportType>bike</sportType>",
        "  <workout>",
        *body,
        "  </workout>",
        "</workout_file>",
        "",
    ])  # fmt: skip


# ------------------------------------------------------------------ Garmin .fit workout
# A minimal FIT encoder: file_id, workout and workout_step messages only.

FIT_EPOCH = dt.datetime(1989, 12, 31, tzinfo=dt.timezone.utc)
SPORT = {"bike": (2, 0), "handbike": (2, 12), "run": (1, 0), "concept2": (15, 14), "grinding": (10, 0)}  # (sport, sub_sport)
INTENSITY = {"work": 0, "steady": 0, "test": 0, "rest": 1, "warmup": 2, "cooldown": 3}
DUR_TIME, DUR_REPEAT = 0, 6
TGT_HR, TGT_OPEN, TGT_POWER = 1, 2, 4
ENUM, UINT16, UINT32, UINT32Z, STRING = 0x00, 0x84, 0x86, 0x8C, 0x07
NONE32 = 0xFFFFFFFF
_CRC = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]  # fmt: skip


def fit_crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        for nibble in (byte & 0xF, byte >> 4):
            tmp = _CRC[crc & 0xF]
            crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC[nibble]
    return crc


def _fit_string(s: str, size: int) -> bytes:
    raw = s.encode("utf-8")[: size - 1]
    return raw + b"\x00" * (size - len(raw))


class _FitWriter:
    def __init__(self):
        self.records = bytearray()

    def define(self, local: int, global_num: int, fields: list[tuple[int, int, int]]) -> None:
        self.records += struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields))
        for num, size, base in fields:
            self.records += struct.pack("<BBB", num, size, base)

    def data(self, local: int, payload: bytes) -> None:
        self.records += bytes([local]) + payload

    def build(self) -> bytes:
        header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(self.records), b".FIT")
        header += struct.pack("<H", fit_crc(header))
        body = header + bytes(self.records)
        return body + struct.pack("<H", fit_crc(body))


def _flatten(steps, out: list[dict]) -> None:
    for s in steps:
        if s.kind == "repeat":
            first = len(out)
            _flatten(s.steps, out)
            out.append({"name": f"Repeat {s.count}x", "dur_type": DUR_REPEAT, "dur": first,
                        "tgt_type": TGT_OPEN, "tgt": s.count, "lo": NONE32, "hi": NONE32, "intensity": 0})  # fmt: skip
            continue
        t = s.target
        if t.watts:
            tgt_type, tgt, lo, hi = TGT_POWER, 0, t.watts[0] + 1000, t.watts[1] + 1000  # FIT: watts + 1000
        elif t.hr_zone is not None:
            tgt_type, tgt, lo, hi = TGT_HR, t.hr_zone, NONE32, NONE32
        else:
            tgt_type, tgt, lo, hi = TGT_OPEN, 0, NONE32, NONE32
        name = (CUES[s.kind] or s.kind) + (f" RPE {t.rpe:g}" if t.rpe is not None else "")
        out.append({"name": name, "dur_type": DUR_TIME, "dur": s.duration_s * 1000, "tgt_type": tgt_type,
                    "tgt": tgt, "lo": lo, "hi": hi, "intensity": INTENSITY[s.kind]})  # fmt: skip


def fit_workout(name: str, modality: str, steps, created: dt.datetime | None = None) -> bytes:
    flat: list[dict] = []
    _flatten(steps, flat)
    sport, sub_sport = SPORT[modality]
    created = created or dt.datetime.now(dt.timezone.utc)
    w = _FitWriter()
    w.define(0, 0, [(0, 1, ENUM), (1, 2, UINT16), (2, 2, UINT16), (3, 4, UINT32Z), (4, 4, UINT32)])
    w.data(0, struct.pack("<BHHII", 5, 255, 0, 1, int((created - FIT_EPOCH).total_seconds())))
    w.define(1, 26, [(4, 1, ENUM), (11, 1, ENUM), (6, 2, UINT16), (8, 32, STRING)])
    w.data(1, struct.pack("<BBH", sport, sub_sport, len(flat)) + _fit_string(name, 32))
    w.define(2, 27, [(254, 2, UINT16), (0, 16, STRING), (1, 1, ENUM), (2, 4, UINT32), (3, 1, ENUM),
                     (4, 4, UINT32), (5, 4, UINT32), (6, 4, UINT32), (7, 1, ENUM)])  # fmt: skip
    for i, st in enumerate(flat):
        w.data(2, struct.pack("<H", i) + _fit_string(st["name"], 16)
               + struct.pack("<BIBIIIB", st["dur_type"], st["dur"], st["tgt_type"], st["tgt"], st["lo"], st["hi"], st["intensity"]))  # fmt: skip
    return w.build()
