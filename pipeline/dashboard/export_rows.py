"""python -m pipeline.dashboard.export_rows [--ref origin/data | --src derived] [--out derived/rows]

Reshapes the data branch's dashboard.json, today.json, debrief.json and analysis/latest.json
into one JSON file per Pedestal dashboard dataset: an array of flat row objects, every row
with the same keys. Reads nothing else and writes only into --out.
"""

import argparse
import csv
import datetime as dt
import io
import json
import subprocess
import sys
from pathlib import Path

SOURCES = ("dashboard.json", "today.json", "debrief.json", "analysis/latest.json")
TABLES = ("wellness.csv", "compliance.csv", "activities.csv", "zones.csv", "heavy_reps.csv")  # for the week rows; may be absent
GOAL_DATE = "2027-04-17"


def _even(rows: list[dict]) -> list[dict]:
    """Give every row the same keys, in first-seen order, so a dataset has one set of columns."""
    cols = list(dict.fromkeys(k for r in rows for k in r))
    return [{c: r.get(c) for c in cols} for r in rows]


def _band(v) -> tuple:
    return (v[0], v[1]) if v else (None, None)


def _clock(s) -> str:
    if not s:
        return ""
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _step_text(s: dict) -> str:
    if s["kind"] == "repeat":
        return f"{s['count']} × [" + ", ".join(_step_text(x) for x in s["steps"]) + "]"
    return f"{s['kind']} {_clock(s['duration_s'])} {s['target']}".strip() + (f" ({s['note']})" if s.get("note") else "")


def _strength_text(g: dict) -> str:
    kg = f"{g['kg']:g} kg" if g.get("kg") is not None else "choose kg"
    cap = f" RPE ≤{g['rpe_cap']:g}" if g.get("rpe_cap") is not None else ""
    return f"{g['name']} {g['sets']}×{g['reps']} @ {kg}{cap}"


def summary(dash: dict, today: dict, debrief: dict) -> list[dict]:
    day = dt.date.fromisoformat(today.get("date") or dash["date"])
    goals = [e["date"] for e in dash["charts"]["fitness"]["events"] if e.get("priority") == "A" and e["date"] >= day.isoformat()]
    goal = min(goals) if goals else GOAL_DATE
    r, bike, block = today.get("readiness") or {}, dash["charts"]["bike"], today.get("block") or {}
    lo, hi = _band(bike.get("ftp_target"))
    return [{
        "date": day.isoformat(), "status": today.get("status"), "status_reason": today.get("status_reason") or "",
        "generated_at": today.get("generated_at"), "readiness": r.get("colour") or "no call", "readiness_reason": r.get("reason", ""),
        "block": block.get("name"), "week": block.get("week"), "ftp_w": bike.get("ftp_now"), "ftp_target_lo_w": lo, "ftp_target_hi_w": hi,
        "goal_date": goal, "days_to_goal": (dt.date.fromisoformat(goal) - day).days,
        "yesterday_date": debrief.get("date"), "yesterday_verdict": debrief.get("verdict", ""), "note_for_today": debrief.get("note_for_today", ""),
        "pipeline_failed": ", ".join(today.get("pipeline", {}).get("failed", [])),
    }]  # fmt: skip


def today_sessions(today: dict) -> list[dict]:
    rows = []
    for s in today.get("sessions", []):
        if s.get("strength") is not None:
            detail = "; ".join(_strength_text(g) for g in s["strength"]) or "no prescriptions yet"
        else:
            detail = "; ".join(_step_text(x) for x in s.get("steps", []))
        rows.append({"slot": s["slot"], "order": s.get("order", 0), "session": s["session"], "modality": s["modality"],
                     "summary": s["summary"], "duration_min": s.get("duration_min"), "tss": s.get("tss"),
                     "readiness_exempt": bool(s.get("readiness_exempt")), "provisional_ftp": bool(s.get("provisional_ftp")),
                     "detail": detail})  # fmt: skip
    return _even(rows)


def latest_session(an: dict) -> list[dict]:
    if not an or an.get("status") != "ok":
        return [{"status": (an or {}).get("status", "none"), "generated_at": (an or {}).get("generated_at")}]
    a, b, f = an["activity"], an["breakdown"], an["fitness"]
    proj, week = an.get("projections") or f.get("projections") or {}, f.get("week") or {}
    nxt = proj.get("next_key_session") or {}
    lo, hi = _band(week.get("band"))
    curve = b.get("power_curve") or {}
    row = {
        "status": "ok", "generated_at": an["generated_at"], "activity_id": a["id"], "date": a["date"], "start_local": a.get("start_local"),
        "name": a["name"], "sport": a["sport"], "kind": b.get("kind"), "session": b.get("session"), "grade": b.get("grade"),
        "moving_min": b.get("moving_min") or float(a["moving_min"]), "np_w": b.get("np_w"), "intensity": b.get("intensity"),
        "tss": b.get("tss"), "avg_hr": b.get("avg_hr"), "hr_drift_pct": b.get("hr_drift_pct"),
        "best_1min_w": curve.get("60"), "best_4min_w": curve.get("240"), "best_20min_w": curve.get("1200"),
        "session_load": f["session_load"], "load_source": f["load_source"], "grind_correction": f.get("grind_correction"),
        "ctl_before": f["before"]["ctl"], "atl_before": f["before"]["atl"], "form_before": f["before"]["form"],
        "ctl_after": f["after"]["ctl"], "atl_after": f["after"]["atl"], "form_after": f["after"]["form"],
        "form_tomorrow": proj.get("form_tomorrow"), "next_key_date": nxt.get("date"), "next_key_session": nxt.get("summary"),
        "next_key_form": nxt.get("form"), "week_label": week.get("label"), "week_done_tss": week.get("done_tss"),
        "week_band_lo": lo, "week_band_hi": hi,
        "bests": " · ".join(f"{x['duration']} {x['watts']} W" + (" (first)" if x.get("first") else f" (was {x['previous']} W)" if x.get("previous") else "")
                            for x in an.get("bests", [])),  # fmt: skip
        "bests_since": an.get("bests_since"),
        "anchor_flags": "; ".join(f"{x['anchor']} {x['current_w']:g} → {x['suggested_w']:g} W ({x['change_pct']:+g}%): {x['evidence']}"
                                  for x in an.get("anchor_flags", [])),  # fmt: skip
    }  # fmt: skip
    return [row]


def yesterday_items(debrief: dict) -> list[dict]:
    rows = []
    for i in debrief.get("items", []):
        detail = ""
        if i.get("lifts"):
            detail = "; ".join(f"{lf['name']}: " + ", ".join(f"{s['achieved'] or s['prescribed']} {s['status']}" for s in lf["sets"]) for lf in i["lifts"])
        elif i.get("reps"):
            detail = ", ".join(f"{r['watts']} W {r['status']}" for r in i["reps"])
        act = i.get("activity") or {}
        rows.append({"date": debrief["date"], "kind": i["kind"], "slot": i.get("slot"), "session": i.get("session"),
                     "modality": i.get("modality") or i.get("sport"), "summary": i.get("summary") or i.get("name"),
                     "planned_min": i.get("planned_min"), "planned_tss": i.get("planned_tss"),
                     "done_min": act.get("moving_min") or i.get("moving_min"), "activity": act.get("name") or i.get("name"),
                     "grade": i.get("grade", "unplanned" if i["kind"] == "unplanned" else None), "grade_note": i.get("grade_note", ""),
                     "detail": detail})  # fmt: skip
    return _even(rows)


def weekly_load(dash: dict) -> list[dict]:
    weeks = dash["charts"]["weekly_load"]
    sports = sorted({s for w in weeks for s in w["by_sport"]})
    rows = []
    for w in weeks:
        lo, hi = _band(w.get("band"))
        rows.append({"week": w["week"], "total": w["total"], "planned": w["planned"], "band_lo": lo, "band_hi": hi,
                     "future": w["future"], **{f"load_{s}": w["by_sport"].get(s, 0) for s in sports}})  # fmt: skip
    return rows


def grinding(dash: dict) -> list[dict]:
    g = dash["charts"]["grinding"]
    rows = []
    for key, label, secs in (("60s", "1 min", 60), ("240s", "4 min", 240)):
        tests = [t for t in g.get("tests", []) if t.get("duration") in (key, label, secs)]
        best = max(tests, key=lambda t: t["watts"], default=None)
        rows.append({"duration": label, "duration_s": secs, "now_w": g["now"].get(key), "target_w": g["target"].get(key),
                     "best_test_w": best["watts"] if best else None, "best_test_date": best["date"] if best else None})  # fmt: skip
    return rows


def bike(dash: dict) -> list[dict]:
    b, day = dash["charts"]["bike"], dash["date"]
    lo, hi = _band(b.get("ftp_target"))
    rows = [{"series": "ftp_intervals_icu", "date": x["date"], "value": x["ftp"], "label": "intervals.icu FTP"} for x in b.get("ftp_intervals_icu", [])]
    rows.append({"series": "ftp_now", "date": day, "value": b.get("ftp_now"), "label": "FTP in athlete.yaml"})
    rows += [{"series": "ftp_target_lo", "date": GOAL_DATE, "value": lo, "label": "FTP target, low"},
             {"series": "ftp_target_hi", "date": GOAL_DATE, "value": hi, "label": "FTP target, high"}]  # fmt: skip
    rows += [{"series": "long_ride_min", "date": x["date"], "value": x["minutes"], "label": x["name"]} for x in b.get("long_rides", [])]
    rows += [{"series": "late_ride_ratio", "date": x["date"], "value": x["ratio"], "label": f"{x['first_hour_w']:g} → {x['last_hour_w']:g} W"}
             for x in b.get("late_ride_power", [])]  # fmt: skip
    return rows


def build(dash: dict, today: dict, debrief: dict, analysis: dict | None, *, request: dict | None = None,
          now: dt.datetime | None = None, plan=None, athlete=None, tables: dict | None = None) -> dict[str, list[dict]]:  # fmt: skip
    """`now` (UTC) is stamped on the summary row as exported_at: when the dashboard was last refreshed.
    `request` is what analysis/latest.json said, when `analysis` is an older successful one."""
    c, fit = dash["charts"], dash["charts"]["fitness"]
    now = now or dt.datetime.now(dt.timezone.utc)
    head = summary(dash, today, debrief)
    head[0]["exported_at"] = now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    head[0]["analysis_generated_at"] = (analysis or {}).get("generated_at")
    req = request or analysis or {}
    head[0]["last_request_status"], head[0]["last_request_at"] = req.get("status"), req.get("generated_at")
    return {
        "summary": head,
        "today_sessions": today_sessions(today),
        "latest_session": latest_session(analysis),
        "yesterday_items": yesterday_items(debrief),
        "fitness_days": _even(fit["days"]),
        "fitness_planned": _even(fit["planned"]),
        "blocks": [{"id": b["id"], "name": b["name"], "start": b["start"], "end": b["end"], "status": b["status"],
                    "ctl_end_lo": _band(b.get("ctl_end"))[0], "ctl_end_hi": _band(b.get("ctl_end"))[1]} for b in fit["blocks"]],  # fmt: skip
        "events": _even([{"date": e["date"], "end": e.get("end"), "label": e["label"], "priority": e["priority"],
                          "month": bool(e.get("month"))} for e in fit["events"]]),  # fmt: skip
        "weekly_load": weekly_load(dash),
        "bench_points": _even(c["bench"]["points"]),
        "bench_checkpoints": _even(c["bench"]["checkpoints"]),
        "lifts": [{"lift": k, "date": p["date"], "e1rm": p["e1rm"], "set": p["set"]} for k, pts in c["lifts"].items() for p in pts],
        "grinding": grinding(dash),
        "bike": _even(bike(dash)),
        "readiness_days": _even(c["readiness"]),
        "compliance": _even(c["compliance"]),
        "body": _even([{"kind": "weight", **w} for w in c["body"]["weight"]] + [{"kind": "dexa", **x} for x in c["body"].get("dexa", [])]),
        **(_week(dash, today, plan, athlete, tables or {}) if plan is not None else {}),
    }  # fmt: skip


def _week(dash: dict, today: dict, plan, athlete, tables: dict) -> dict[str, list[dict]]:
    from pipeline.dashboard import week

    day = dt.date.fromisoformat(today.get("date") or dash["date"])
    return {k: _even(v) for k, v in week.build(day, plan, athlete, dash["charts"]["fitness"], tables).items()}


def read_sources(src: Path | None = None, ref: str | None = None, *, git=subprocess.run) -> dict[str, dict | None]:
    """The four JSON files from a directory or a git ref; analysis/latest.json may be absent.

    When latest.json isn't "ok" (no_new_activity, error), "analysis/latest.json" is the last
    successful analysis instead (its `last_analysis`, else the newest analysis/<date>-<id>.json),
    and "analysis/request" keeps what latest.json said.
    """

    def read(name: str) -> str | None:
        if ref:
            r = git(["git", "show", f"{ref}:{name}"], capture_output=True, text=True, encoding="utf-8")
            return r.stdout if r.returncode == 0 else None
        p = src / name
        return p.read_text(encoding="utf-8") if p.exists() else None

    def analyses() -> list[str]:
        if ref:
            r = git(["git", "ls-tree", "--name-only", f"{ref}:analysis"], capture_output=True, text=True, encoding="utf-8")
            names = r.stdout.split() if r.returncode == 0 else []
        else:
            names = [p.name for p in (src / "analysis").glob("*.json")] if (src / "analysis").exists() else []
        return sorted(f"analysis/{n}" for n in names if n[:4].isdigit() and n.endswith(".json"))

    out = {}
    for name in SOURCES:
        text = read(name)
        if text is None and name != "analysis/latest.json":
            raise FileNotFoundError(f"{name} not found in {ref or src}")
        out[name] = json.loads(text) if text else None
    out["tables"] = {name: list(csv.DictReader(io.StringIO(text))) for name in TABLES if (text := read(name))}
    latest = out["analysis/latest.json"]
    out["analysis/request"] = latest
    if latest and latest.get("status") != "ok":
        last = (latest.get("last_analysis") or "").removesuffix(".md")
        candidates = ([last + ".json"] if last else []) + analyses()[::-1]
        for name in candidates:
            text = read(name)
            if text:
                full = json.loads(text)
                full.setdefault("status", "ok")
                out["analysis/latest.json"] = full
                break
    return out


def write(rows: dict[str, list[dict]], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in rows.items():
        p = out / f"{name}.json"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        paths.append(p)
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.dashboard.export_rows", description=__doc__.split("\n\n")[1])
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--ref", help="read the files from this git ref, e.g. origin/data")
    where.add_argument("--src", type=Path, default=Path("derived"), help="read the files from this directory (default: derived)")
    parser.add_argument("--out", type=Path, default=Path("derived/rows"))
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"), help="for the week rows (default: config/plan.yaml)")
    parser.add_argument("--athlete", type=Path, help="default: athlete.yaml beside the plan")
    args = parser.parse_args(argv)
    from pipeline.plan.schema import PlanLoadError, load_checked, report_load_error

    try:
        plan, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        return report_load_error(e)
    try:
        s = read_sources(None if args.ref else args.src, args.ref)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"export_rows: {e}", file=sys.stderr)
        return 1
    rows = build(s["dashboard.json"], s["today.json"], s["debrief.json"], s["analysis/latest.json"], request=s["analysis/request"],
                 plan=plan, athlete=athlete, tables=s["tables"])  # fmt: skip
    write(rows, args.out)
    print(f"wrote {len(rows)} datasets to {args.out}: " + ", ".join(f"{k} {len(v)}" for k, v in rows.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
