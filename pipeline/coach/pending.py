"""python -m pipeline.coach.pending ITEMS_DIR [--today DATE] [--commit] [--push] [--results PATH]

Applies the items Pedestal Coach saved under data/users/{you}/coach/pending/ (read out of its store
with ArtifactData into ITEMS_DIR, one <doc id>.json per item) to the training logs:

- session_change -> training/logs/changes.yaml   (done_late, move, swap, drop)
- lift_sets      -> training/logs/strength.csv
- bodyweight     -> training/logs/body.csv       (ingest prefers it over intervals.icu's weight that day)

Everything in an item is untrusted data. Each one is checked again here, against the plan with the
changes log applied, in the order session changes, sets, bodyweight (oldest first within each), so a
done_late saved before its sets makes them valid. An item that fails, or repeats what a log already
holds, is rejected with the reason; nothing of it is written. Items not marked pending are skipped.

With --commit the changed logs are committed (only those files), and with --push pushed to main.
Prints one JSON line, and writes it to --results: {applied, rejected, skipped, items: {id: {status,
reason}}, commit, files}. Claude then marks each item applied or rejected in the store (README.md).
"""

import argparse
import csv
import datetime as dt
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from pipeline.ingest.strength import COLUMNS as STRENGTH_COLUMNS
from pipeline.ingest.strength import StrengthLogError, read_strength_log
from pipeline.plan import changes as ch
from pipeline.plan.expand import apply_changes
from pipeline.plan.schema import PlanLoadError, load_checked

KINDS = ("session_change", "lift_sets", "bodyweight")
ROLES = ("top", "backoff", "work", "speed", "amrap", "test")
BODY_COLUMNS = ("date", "kg", "source", "notes")
SLOT = re.compile(r"^\d{4}-\d{2}-\d{2} (am|pm)$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Rejected(Exception):
    pass


def read_items(path: Path) -> list[dict]:
    """The pending items: one <id>.json per document (an ArtifactData read with out_dir), or one JSON list of {id, ...}."""
    if path.is_file():
        items = json.loads(path.read_text(encoding="utf-8"))
        return [x for x in items if isinstance(x, dict)]
    out = []
    for p in sorted(path.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
        out.append({"id": p.stem, **data} if isinstance(data, dict) else {"id": p.stem, "status": "pending", "kind": None, "_unreadable": True})
    return out


def _text(v, limit=200) -> str:
    """A short single-line string: no newlines or control characters, no quotes that could end a YAML string."""
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(v or "")).strip()
    return s[:limit]


# ------------------------------------------------------------------ session changes


def change_entry(p: dict) -> dict:
    """The changes.yaml entry for a coach session_change payload (shape and ids checked; positions as text)."""
    t = p.get("type")
    planned, done = str(p.get("planned") or "").strip().lower(), str(p.get("done") or "").strip().lower()
    reason = _text(p.get("reason"))
    if t not in ("done_late", "move", "swap", "drop"):
        raise Rejected(f"unknown change type {t!r}")
    if not SLOT.match(planned):
        raise Rejected(f"planned {planned!r} isn't 'YYYY-MM-DD am|pm'")
    if t != "drop" and not SLOT.match(done):
        raise Rejected(f"done {done!r} isn't 'YYYY-MM-DD am|pm'")
    sid = str(p.get("session") or "").strip()
    if t != "swap" and not ID.match(sid):
        raise Rejected("session id missing or malformed")
    if t == "done_late":
        e = {"type": "done_late", "session": sid, "planned": planned, "done": done}
    elif t == "move":
        e = {"type": "move", "session": sid, "from": planned, "to": done}
    elif t == "swap":
        e = {"type": "swap", "a": planned, "b": done}
    else:
        if not reason:
            raise Rejected("a drop needs a reason")
        e = {"type": "drop", "session": sid, "date": planned}
    if reason:
        e["reason"] = reason
    return e


def _same_change(a, b) -> bool:
    keep = lambda c: {k: v for k, v in c.model_dump(by_alias=True).items() if k != "reason"}  # noqa: E731
    return keep(a) == keep(b)


def check_change(entry: dict, plan, athlete, today: dt.date) -> tuple:
    """Parse and apply the plan's changes plus this one; returns the new changes tuple. Raises Rejected."""
    try:
        (new,) = ch.parse([entry])
    except ch.ChangesError as e:
        raise Rejected(e.lines[0].split(": ", 1)[-1]) from e
    if any(_same_change(new, old) for old in plan.changes):
        raise Rejected("already in the changes log")
    problems = ch.past_problems((new,), (), today)
    if problems:
        raise Rejected(problems[0].split(": ", 1)[-1])
    candidate = tuple(plan.changes) + (new,)
    try:
        apply_changes(plan, athlete, candidate)
    except ch.ChangesError as e:
        raise Rejected(e.lines[0].split(": ", 1)[-1].replace(f"changes[{len(candidate) - 1}]: ", "")) from e
    return candidate


def yaml_line(e: dict) -> str:
    """One flow-style entry, as the file's own: - { type: move, session: x, from: 2026-10-26 am, ... }."""
    parts = [f"{k}: {json.dumps(v) if k == 'reason' else v}" for k, v in e.items()]
    return "- { " + ", ".join(parts) + " }"


# ------------------------------------------------------------------ sets and bodyweight


def set_rows(p: dict, today: dt.date, source: str) -> list[dict]:
    date, sid, lift = str(p.get("date") or ""), str(p.get("session") or ""), str(p.get("lift") or "")
    if not DATE.match(date):
        raise Rejected(f"date {date!r} isn't YYYY-MM-DD")
    if dt.date.fromisoformat(date) > today:
        raise Rejected(f"{date} is in the future")
    if not ID.match(sid) or not ID.match(lift):
        raise Rejected("session or lift id missing or malformed")
    sets = p.get("sets")
    if not isinstance(sets, list) or not 1 <= len(sets) <= 20:
        raise Rejected("needs 1 to 20 sets")
    rows = []
    for n, x in enumerate(sets, 1):
        if not isinstance(x, dict):
            raise Rejected(f"set {n} isn't an object")
        role, kg, reps, rpe = x.get("role"), x.get("kg"), x.get("reps"), x.get("rpe")
        if role not in ROLES:
            raise Rejected(f"set {n}: role {role!r} isn't one of {', '.join(ROLES)}")
        if not isinstance(kg, (int, float)) or isinstance(kg, bool) or not 0 <= kg <= 400:
            raise Rejected(f"set {n}: kg must be 0-400")
        if not isinstance(reps, int) or isinstance(reps, bool) or not 1 <= reps <= 50:
            raise Rejected(f"set {n}: reps must be a whole number 1-50")
        if rpe is not None and (not isinstance(rpe, (int, float)) or isinstance(rpe, bool) or not 5 <= rpe <= 10 or rpe * 2 != int(rpe * 2)):
            raise Rejected(f"set {n}: RPE must be 5-10 in half steps")
        rows.append({"date": date, "session": sid, "lift": lift, "role": role, "kg": f"{kg:g}", "reps": str(reps),
                     "rpe": "" if rpe is None else f"{rpe:g}", "notes": f"From Pedestal Coach ({source})"})  # fmt: skip
    return rows


def _set_key(r: dict) -> tuple:
    return (r["date"], r["session"], r["lift"], r["role"], float(r["kg"]), int(r["reps"]), r["rpe"] or "")


def check_sets(rows: list[dict], log_text: str, plan, athlete) -> None:
    """The log with these rows appended must read cleanly (lift in the session, session scheduled that day). Raises Rejected."""
    existing = list(csv.DictReader(io.StringIO(log_text)))
    have = [_set_key({**r, "kg": r["kg"] or 0, "reps": r["reps"] or 0}) for r in existing]
    if all(_set_key(r) in have for r in rows):
        raise Rejected("already in the strength log")
    buf = io.StringIO()
    w = csv.DictWriter(buf, STRENGTH_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "new.csv"
        path.write_text(buf.getvalue(), encoding="utf-8")
        try:
            log = read_strength_log(path, plan, athlete)
        except StrengthLogError as e:
            raise Rejected(re.sub(r"^line \d+(, \w+)?: ", "", e.errors[0])) from e
    if log.warnings:
        raise Rejected(re.sub(r"^line \d+: ", "", log.warnings[0]))


def body_row(p: dict, today: dt.date, source: str) -> dict:
    date, kg = str(p.get("date") or ""), p.get("kg")
    if not DATE.match(date):
        raise Rejected(f"date {date!r} isn't YYYY-MM-DD")
    if dt.date.fromisoformat(date) > today:
        raise Rejected(f"{date} is in the future")
    if not isinstance(kg, (int, float)) or isinstance(kg, bool) or not 30 <= kg <= 250:
        raise Rejected("kg must be 30-250")
    return {"date": date, "kg": f"{round(kg, 1):g}", "source": "coach", "notes": f"From Pedestal Coach ({source})"}


def read_body(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------ the run


def apply(items: list[dict], *, plan, athlete, today: dt.date, changes_path: Path, log_path: Path, body_path: Path) -> dict:
    """Check and write every pending item. Returns {items: {id: {status, reason}}, applied, rejected, skipped, files}."""
    result: dict[str, dict] = {}
    order = {k: i for i, k in enumerate(KINDS)}
    pending = []
    for x in items:
        iid = str(x.get("id") or "")
        if not ID.match(iid):
            continue  # nothing to report it against
        if x.get("status") != "pending":
            result[iid] = {"status": "skipped", "reason": f"status is {x.get('status')!r}"}
            continue
        if x.get("kind") not in KINDS or not isinstance(x.get("payload"), dict):
            result[iid] = {"status": "rejected", "reason": "unreadable item"}
            continue
        pending.append(x)
    pending.sort(key=lambda x: (order[x["kind"]], str(x.get("created") or ""), x["id"]))

    new_lines, new_sets, new_body = [], [], []
    log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ",".join(STRENGTH_COLUMNS) + "\n"
    body = read_body(body_path)
    for x in pending:
        p, source = x["payload"], "saved " + str(x.get("created") or "")[:10]
        try:
            if x["kind"] == "session_change":
                entry = change_entry(p)
                plan._changes = check_change(entry, plan, athlete, today)  # later items see it
                new_lines.append(yaml_line(entry))
            elif x["kind"] == "lift_sets":
                rows = set_rows(p, today, source)
                check_sets(rows, log_text + "".join(_csv_lines(new_sets)), plan, athlete)
                new_sets += rows
            else:
                row = body_row(p, today, source)
                if any(r["date"] == row["date"] and r["kg"] == row["kg"] for r in body + new_body):
                    raise Rejected("already in the body log")
                new_body.append(row)
            result[x["id"]] = {"status": "applied", "reason": ""}
        except Rejected as e:
            result[x["id"]] = {"status": "rejected", "reason": str(e)}

    files = []
    if new_lines:
        text = changes_path.read_text(encoding="utf-8") if changes_path.exists() else ""
        changes_path.write_text(text + ("" if text.endswith("\n") or not text else "\n") + "\n".join(new_lines) + "\n", encoding="utf-8")
        files.append(changes_path)
    if new_sets:
        log_path.write_text(log_text + ("" if log_text.endswith("\n") else "\n") + "".join(_csv_lines(new_sets)), encoding="utf-8")
        files.append(log_path)
    if new_body:
        rows = body + new_body
        buf = io.StringIO()
        w = csv.DictWriter(buf, BODY_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["date"]))
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(buf.getvalue(), encoding="utf-8")
        files.append(body_path)
    count = lambda s: sum(r["status"] == s for r in result.values())  # noqa: E731
    return {"applied": count("applied"), "rejected": count("rejected"), "skipped": count("skipped"), "items": result,
            "files": [str(f) for f in files]}  # fmt: skip


def _csv_lines(rows: list[dict]) -> list[str]:
    buf = io.StringIO()
    csv.DictWriter(buf, STRENGTH_COLUMNS, lineterminator="\n").writerows(rows)
    return [buf.getvalue()] if rows else []


def commit(files: list[str], message: str, *, push: bool, run=subprocess.run) -> str | None:
    """Commit only these files on main, then push. Returns the short hash."""
    def git(*args):
        r = run(["git", *args], capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout.strip()

    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise RuntimeError("not on main")
    git("commit", "-q", "-m", message, "--", *files)
    if push:
        git("push", "-q", "origin", "HEAD:main")
    return git("rev-parse", "--short", "HEAD")


def message(result: dict, items: list[dict]) -> str:
    kinds = {x["id"]: x.get("kind") for x in items}
    n = lambda k: sum(1 for i, r in result["items"].items() if r["status"] == "applied" and kinds.get(i) == k)  # noqa: E731
    parts = [f"{n('session_change')} session change(s)", f"{n('lift_sets')} lift set entr(ies)", f"{n('bodyweight')} bodyweight(s)"]
    return f"Coach inputs: {', '.join(parts)}" + (f"; {result['rejected']} rejected" if result["rejected"] else "") + \
        "\n\nApplied by the Pedestal run from items saved in Pedestal Coach.\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def main(argv=None, *, run=subprocess.run) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.coach.pending", description=__doc__.split("\n\n")[1])
    parser.add_argument("items", type=Path, help="directory of <id>.json pending items, or a JSON list")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--plan", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path)
    parser.add_argument("--changes", type=Path, default=Path("training/logs/changes.yaml"))
    parser.add_argument("--log", type=Path, default=Path("training/logs/strength.csv"))
    parser.add_argument("--body", type=Path, default=Path("training/logs/body.csv"))
    parser.add_argument("--commit", action="store_true", help="commit the changed logs (only those files)")
    parser.add_argument("--push", action="store_true", help="push main after committing")
    parser.add_argument("--results", type=Path, help="also write the JSON result here")
    args = parser.parse_args(argv)
    try:
        plan, athlete = load_checked(args.plan, args.athlete, changes_path=args.changes)
    except PlanLoadError as e:
        print(json.dumps({"error": " ".join(e.lines)}))
        return 1
    items = read_items(args.items) if args.items.exists() else []
    result = apply(items, plan=plan, athlete=athlete, today=args.today, changes_path=args.changes, log_path=args.log, body_path=args.body)
    result["commit"] = None
    if result["files"] and args.commit:
        try:
            result["commit"] = commit(result["files"], message(result, items), push=args.push, run=run)
        except RuntimeError as e:
            result["error"] = str(e)
    line = json.dumps(result)
    if args.results:
        args.results.write_text(line + "\n", encoding="utf-8")
    print(line)
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
