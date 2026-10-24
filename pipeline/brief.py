"""Today's brief: derived/today.md to read, derived/today.json for anything else to use.

Built from the plan and what the pipeline has written (wellness, prescriptions,
compliance, weekly): the date, readiness colour and reason, each session today (slot,
summary, watt targets, or strength groups as kg x reps @ RPE cap with the reason),
yesterday's grades, the flags from the latest Sunday review, and any pipeline step that
failed. The daily run writes it at the end; on the data branch it's readable from the
GitHub app.

    python -m pipeline.brief [--day 2026-10-26]      # write and print today's brief
"""

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

from pipeline.athlete import Athlete
from pipeline.plan.expand import ResolvedStep, expand
from pipeline.plan.schema import Plan, PlanLoadError, load_checked, report_load_error
from pipeline.review.debrief import to_markdown as debrief_markdown


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(v: str):
    if v in ("", None):
        return None
    x = float(v)
    return int(x) if x.is_integer() else x


def day_status(derived: Path, day: dt.date) -> tuple[str, str]:
    """('final', '') when today's wellness row has sleep, else ('provisional', 'no sleep yet')."""
    row = next((r for r in _rows(derived / "wellness.csv") if r["date"] == day.isoformat()), None)
    return ("final", "") if row and row.get("sleep_h") else ("provisional", "no sleep yet")


def short_readiness(reason: str) -> str:
    """Drop the baseline arithmetic: 'amber: HRV 30 < 32.9 (baseline ...)' -> 'HRV 30 < 32.9'."""
    body = reason.split(": ", 1)[1] if ": " in reason else reason
    body = re.sub(r" \(baseline [^)]*\)", "", body)
    return body.replace("resting HR", "RHR")


def _step(s: ResolvedStep) -> dict:
    if s.kind == "repeat":
        return {"kind": "repeat", "count": s.count, "duration_s": s.duration_s, "steps": [_step(x) for x in s.steps]}
    t = s.target
    out = {"kind": s.kind, "duration_s": s.duration_s, "target": t.text(), "anchor": t.anchor}
    if t.watts:
        out["watts"] = list(t.watts)
        out["provisional"] = t.provisional
    if s.note:
        out["note"] = s.note
    return out


def _provisional(steps) -> bool:
    return any(_provisional(s.steps) if s.kind == "repeat" else s.target.provisional for s in steps)


def build(plan: Plan, athlete: Athlete, day: dt.date, derived: Path, failed: list[str] = (), skipped: list[str] = (),
          *, status: str = "provisional", status_reason: str = "", alerts_sent: list[str] = ()) -> dict:  # fmt: skip
    """The structured brief (what today.json holds). `status` is "final" once today's sleep is in."""
    iso = day.isoformat()
    well = next((r for r in _rows(derived / "wellness.csv") if r["date"] == iso), {})
    colour = well.get("readiness") or None
    reason = well.get("readiness_reason", "")
    if not colour:
        reason = reason or "no wellness data for today yet"
    rx = [r for r in _rows(derived / "prescriptions.csv") if r["date"] == iso]
    names = {k: v.name for k, v in athlete.strength.lifts.items()}

    days = expand(plan, athlete, day, day)
    d = days[0] if days else None
    sessions = []
    for s in (d.sessions if d else ()):
        if s.session.modality == "rest":
            continue
        item = {
            "slot": s.slot, "order": s.order, "session": s.session_id, "modality": s.session.modality,
            "summary": s.session.summary, "environment": s.session.environment,
            "duration_min": round(s.duration_s / 60) if s.duration_s else None, "tss": s.tss or None,
            "readiness_exempt": s.session.readiness_exempt,
        }  # fmt: skip
        if s.steps:
            item["steps"] = [_step(x) for x in s.steps]
            item["provisional_ftp"] = _provisional(s.steps)
        if s.session.modality == "strength":
            item["strength"] = [
                {"lift": r["lift"], "name": names.get(r["lift"], r["lift"]), "role": r["role"], "sets": int(r["sets"]),
                 "reps": int(r["reps"]), "kg": _num(r["kg"]), "rpe_cap": _num(r["rpe_cap"]), "basis": r["basis"],
                 "reason": r["reason"]}  # fmt: skip
                for r in rx if r["session"] == s.session_id
            ]
        sessions.append(item)

    yesterday = day - dt.timedelta(days=1)
    graded = [{"session": r["session"], "item": r["item"], "grade": r["grade"], "note": r["note"]}
              for r in _rows(derived / "compliance.csv") if r["date"] == yesterday.isoformat()]  # fmt: skip
    planned_y = [s for x in expand(plan, athlete, yesterday, yesterday) for s in x.sessions if s.session.modality not in ("rest", "mobility")]
    weeks = [r for r in _rows(derived / "weekly.csv") if r["week_end"] <= iso]
    latest = max(weeks, key=lambda r: r["week_end"]) if weeks else None
    debrief = None
    try:
        debrief = json.loads((derived / "debrief.json").read_text(encoding="utf-8"))
        if debrief.get("date") != yesterday.isoformat():
            debrief = None  # a stale debrief is left out
    except (OSError, ValueError):
        pass

    return {
        "date": iso,
        "status": status,
        "status_reason": status_reason,
        "generated_at": dt.datetime.now(athlete.schedule.zone()).isoformat(timespec="minutes"),
        "readiness": {"colour": colour, "reason": reason},
        "block": {"id": d.block.id, "name": d.block.name, "week": d.week.label if d.week else None,
                  "design_pending": d.design_pending} if d else None,  # fmt: skip
        "sessions": sessions,
        "yesterday": {"date": yesterday.isoformat(), "grades": graded,
                      "status": "graded" if graded else ("not graded" if planned_y else "rest day")},  # fmt: skip
        "debrief": debrief,
        "review": {"week_end": latest["week_end"], "flags": [f for f in latest["flags"].split(" | ") if f]} if latest else None,
        "pipeline": {"failed": list(failed), "skipped": list(skipped)},
        "alerts_sent": list(alerts_sent),
    }


# ------------------------------------------------------------------ markdown


def _clock(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _step_md(s: dict) -> str:
    if s["kind"] == "repeat":
        return f"{s['count']} × [" + ", ".join(_step_md(x) for x in s["steps"]) + "]"
    return f"{s['kind']} {_clock(s['duration_s'])} {s['target']}" + (f" ({s['note']})" if s.get("note") else "")


def _day(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


def to_markdown(b: dict) -> str:
    d = dt.date.fromisoformat(b["date"])
    gen = dt.datetime.fromisoformat(b["generated_at"])
    status = b["status"] + (f" ({b['status_reason']})" if b.get("status_reason") else "")
    out = [f"# Pedestal brief: {d:%a} {d.day} {d:%b %Y}", "", f"*Status: {status} · generated {gen:%Y-%m-%d %H:%M}*", ""]
    r = b["readiness"]
    out += [f"**Readiness: {r['colour'].upper()}**: {r['reason'].split(': ', 1)[-1]}" if r["colour"]
            else f"**Readiness: no call**: {r['reason']}", ""]  # fmt: skip
    if b["block"]:
        week = f", week {b['block']['week']}" if b["block"]["week"] else ""
        out += [f"{b['block']['name']}{week}", ""]
    if b["pipeline"]["failed"]:
        out += [f"> Pipeline: {', '.join(b['pipeline']['failed'])} failed today; this brief may use older data.", ""]

    if b["block"] and b["block"]["design_pending"]:
        out += ["Design pending: no sessions planned yet.", ""]
    elif not b["sessions"]:
        out += ["Rest day.", ""]
    for s in b["sessions"]:
        out.append(f"## {s['slot'][-2:].upper()}: {s['summary']}")
        meta = [s["modality"]] + ([f"{s['duration_min']} min"] if s["duration_min"] else []) + ([f"{s['tss']:g} TSS"] if s["tss"] else [])
        if s["readiness_exempt"]:
            meta.append("test: readiness-exempt")
        out += [" · ".join(meta), ""]
        if s.get("strength") is not None:
            if s["strength"]:
                out += ["| Lift | Group | Sets × reps | kg | RPE cap | Why |", "|---|---|---|---|---|---|"]
                for g in s["strength"]:
                    kg = f"{g['kg']:g}" if g["kg"] is not None else "choose"
                    cap = f"{g['rpe_cap']:g}" if g["rpe_cap"] is not None else ""
                    out.append(f"| {g['name']} | {g['role']} | {g['sets']} × {g['reps']} | {kg} | {cap} | {g['reason']} |")
            else:
                out.append("No prescriptions yet (run python -m pipeline.prescribe).")
            out.append("")
        elif s.get("steps"):
            out += [f"- {_step_md(x)}" for x in s["steps"]]
            if s.get("provisional_ftp"):
                out.append("- \\* watts from a provisional FTP")
            out.append("")

    y = b["yesterday"]
    if b.get("debrief"):  # the debrief has the grades and much more
        out += [debrief_markdown(b["debrief"], heading="##").replace("## Debrief:", "## Yesterday's debrief:", 1)]
    else:
        out += [f"## Yesterday ({_day(y['date'])})"]
        if y["grades"]:
            for g in y["grades"]:
                what = g["session"] if g["item"] == "session" else f"{g['session']}: {g['item']}"
                out.append(f"- **{g['grade']}** {what}" + (f": {g['note']}" if g["note"] else ""))
        else:
            out.append("Rest day." if y["status"] == "rest day" else "Not graded yet.")
        out.append("")

    if b["review"]:
        out += [f"## Flags (review of the week ending {_day(b['review']['week_end'])})"]
        out += [f"- {f}" for f in b["review"]["flags"]] or ["None."]
        out.append("")
    return "\n".join(out)


def write(b: dict, derived: Path) -> tuple[Path, Path]:
    derived.mkdir(parents=True, exist_ok=True)
    md, js = derived / "today.md", derived / "today.json"
    md.write_text(to_markdown(b), encoding="utf-8")
    js.write_text(json.dumps(b, indent=1) + "\n", encoding="utf-8")
    return md, js


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.brief", description="Write today's brief.")
    parser.add_argument("--day", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path)
    parser.add_argument("--derived", type=Path, default=Path("derived"))
    parser.add_argument("--print", dest="show", action="store_true", help="also print the markdown")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        plan, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        return report_load_error(e)
    status, reason = day_status(args.derived, args.day)
    b = build(plan, athlete, args.day, args.derived, status=status, status_reason=reason)
    md, js = write(b, args.derived)
    if args.show:
        print(md.read_text(encoding="utf-8"))
    print(f"wrote {md} and {js} ({len(b['sessions'])} sessions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
