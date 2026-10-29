"""Adopt a new or revised plan safely: validate, refuse changes to the past, show the diff.

    python -m pipeline.plan.adopt plans/<file>.yaml            # check and preview (dry run)
    python -m pipeline.plan.adopt plans/<file>.yaml --yes      # adopt it

1. Validates the file against the schema and config/athlete.yaml (lift ids, sessions,
   templates, blocks that don't overlap: everything `pipeline.plan.schema` checks).
2. Refuses any change before today (local date in athlete.yaml `schedule`), so compliance
   and history stay honest. Where the new plan covers a date before today, that day must
   expand to the same sessions with the same content; blocks that have ended, and the
   weeks of the current block that have started, must be unchanged; so must goals and
   strength-line checkpoints dated before today. A new plan that starts in the future
   doesn't cover the past at all, which is allowed: the old plan is archived.
3. Prints a plain-English diff: blocks added, moved or removed; sessions changed per week,
   with session counts and weekly TSS before and after; strength rules per lift; session
   library and strength-line changes.
4. Shows what the next daily publish would change on the calendar (dry run, next 7 days).
5. With --yes: archives the current plan to plans/archive/<plan_id>-<date>.yaml, copies the
   new one to config/plan.yaml and appends an entry to plans/HISTORY.md. Commit those, and
   the next daily run publishes it.

Exit codes: 0 previewed or adopted, 1 invalid or refused, 2 a file couldn't be read.
`--format markdown` writes the report for a pull request comment (the plan-check workflow).
"""

import argparse
import datetime as dt
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from pipeline.athlete import Athlete
from pipeline.plan.expand import expand
from pipeline.plan.schema import Plan, PlanLoadError, load_checked

PUBLISH_DAYS = 7


# ------------------------------------------------------------------ helpers


def _d(day: dt.date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def _dump(model) -> dict:
    return model.model_dump(mode="json")


def schedule(plan: Plan, athlete: Athlete, start: dt.date, end: dt.date) -> dict[dt.date, list[tuple[str, int, str]]]:
    """date -> [(slot, order, session_id)] for every date the plan covers in start..end."""
    if end < start:
        return {}
    # the plans as written: changes.yaml moves are date-scoped edits, not part of either plan
    return {d.date: [(s.slot, s.order, s.session_id) for s in d.sessions] for d in expand(plan, athlete, start, end, changes=())}


def _week_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


# ------------------------------------------------------------------ refusals


def refusals(old: Plan, new: Plan, athlete: Athlete, today: dt.date) -> list[str]:
    """Every way `new` changes something dated before today. Empty means it's safe."""
    out: list[str] = []
    yesterday = today - dt.timedelta(days=1)
    start = max(old.valid_from, new.valid_from)  # only dates both plans cover can conflict
    old_days, new_days = schedule(old, athlete, start, yesterday), schedule(new, athlete, start, yesterday)
    for day in sorted(set(old_days) | set(new_days)):
        a, b = old_days.get(day), new_days.get(day)
        if b is None:
            continue  # the new plan doesn't cover this day (e.g. a gap between its blocks)
        if a != b:
            out.append(f"{_d(day)} {day.year}: sessions change from {_sessions(a)} to {_sessions(b)}")
            continue
        for slot, _, sid in b:
            if sid in old.sessions and _dump(old.sessions[sid]) != _dump(new.sessions[sid]):
                out.append(f"{_d(day)} {day.year}: session {sid} ({slot}) is edited, and it already ran on that day")

    old_blocks = {b.id: b for b in old.blocks}
    for b in new.blocks:
        if b.start >= today:
            continue
        ob = old_blocks.get(b.id)
        if ob is None:
            out.append(f"block {b.id} starts {_d(b.start)}, before today, but isn't in the current plan")
        elif ob.start != b.start:
            out.append(f"block {b.id} moves its start from {_d(ob.start)} to {_d(b.start)}, before today")
        elif ob.end < today and _dump(ob) != _dump(b):
            out.append(f"block {b.id} ended {_d(ob.end)} and can't be edited")
        else:
            old_weeks = {w.start: w for w in ob.weeks or []}
            for w in b.weeks or []:
                if w.start < today and (w.start not in old_weeks or _dump(old_weeks[w.start]) != _dump(w)):
                    out.append(f"block {b.id} week {w.label} started {_d(w.start)} and can't be edited")
            for w in ob.weeks or []:
                if w.start < today and w.start not in {x.start for x in b.weeks or []}:
                    out.append(f"block {b.id} week {w.label} started {_d(w.start)} and can't be removed")
    for ob in old.blocks:
        if new.valid_from <= ob.start < today and ob.id not in {b.id for b in new.blocks}:
            out.append(f"block {ob.id} started {_d(ob.start)} and can't be removed")

    for line in new.strength_lines:
        old_line = next((x for x in old.strength_lines if x.lift == line.lift), None)
        old_pts = {d: v for d, v in (old_line.checkpoints if old_line else [])}
        for d, v in line.checkpoints:
            if d < today and old_pts.get(d) != v:
                was = f"{old_pts[d]:g}" if d in old_pts else "not there"
                out.append(f"{line.lift} checkpoint {_d(d)} {d.year} is in the past: {was} -> {v:g}")
    old_goals = {g.id: g for g in old.goals}
    for g in new.goals:
        if g.date and g.date < today and (g.id not in old_goals or _dump(old_goals[g.id]) != _dump(g)):
            out.append(f"goal {g.id} is dated {_d(g.date)} {g.date.year}, before today, and can't be added or edited")
    if old.plan_id == new.plan_id and old.valid_from < today and new.valid_from != old.valid_from:
        out.append(f"valid_from moves from {old.valid_from} to {new.valid_from}, but the plan has already started")
    return out


def _sessions(entries) -> str:
    if not entries:
        return "nothing"
    return ", ".join(f"{slot} {sid}" for slot, _, sid in entries)


# ------------------------------------------------------------------ the diff


def diff(old: Plan, new: Plan, athlete: Athlete, today: dt.date) -> list[tuple[str, list[str]]]:
    """Sections of the plain-English diff: [(title, lines)], only sections with changes."""
    sections: list[tuple[str, list[str]]] = []
    if old.plan_id != new.plan_id:
        sections.append(("Plan", [f"new plan: {old.plan_id} -> {new.plan_id}",
                                  f"valid {old.valid_from} to {old.valid_to} -> {new.valid_from} to {new.valid_to}"]))  # fmt: skip
    elif (old.valid_from, old.valid_to) != (new.valid_from, new.valid_to):
        sections.append(("Plan", [f"valid {old.valid_from} to {old.valid_to} -> {new.valid_from} to {new.valid_to}"]))

    # blocks
    lines = []
    old_b, new_b = {b.id: b for b in old.blocks}, {b.id: b for b in new.blocks}
    for bid, b in new_b.items():
        if bid not in old_b:
            lines.append(f"added {bid} ({b.name}): {_d(b.start)} to {_d(b.end)}, {b.status}")
            continue
        ob = old_b[bid]
        if (ob.start, ob.end) != (b.start, b.end):
            lines.append(f"moved {bid}: {_d(ob.start)}-{_d(ob.end)} -> {_d(b.start)}-{_d(b.end)}")
        if ob.status != b.status:
            lines.append(f"{bid}: {ob.status} -> {b.status}")
        if ob.name != b.name:
            lines.append(f"{bid}: renamed {ob.name!r} -> {b.name!r}")
        if ob.weekly_tss != b.weekly_tss:
            lines.append(f"{bid}: weekly_tss {_rng(ob.weekly_tss)} -> {_rng(b.weekly_tss)}")
        old_w = {w.start: w for w in ob.weeks or []}
        for w in b.weeks or []:
            ow = old_w.get(w.start)
            if w.start < today or ow is None:
                continue
            for field in ("label", "template", "weekly_tss", "volume_pct"):
                x, y = getattr(ow, field), getattr(w, field)
                if x != y:
                    show = _rng if field == "weekly_tss" else (lambda v: "none" if v is None else _v(v))
                    lines.append(f"{bid} week {w.label} ({_d(w.start)}): {field} {show(x)} -> {show(y)}")
        if {w.start for w in ob.weeks or [] if w.start >= today} != {w.start for w in b.weeks or [] if w.start >= today}:
            lines.append(f"{bid}: weeks from today start on {', '.join(_d(w.start) for w in b.weeks or [] if w.start >= today) or 'none'}")
    for bid, ob in old_b.items():
        if bid not in new_b:
            lines.append(f"removed {bid} ({ob.name}), was {_d(ob.start)} to {_d(ob.end)}")
    if lines:
        sections.append(("Blocks", lines))

    # sessions per week, from today on
    end = max(old.valid_to, new.valid_to)
    a, b = schedule(old, athlete, today, end), schedule(new, athlete, today, end)
    tss_of = lambda plan, entries: sum((plan.sessions[sid].targets.tss or 0) if plan.sessions[sid].targets else 0  # noqa: E731
                                       for _, _, sid in entries)  # fmt: skip
    weeks: dict[dt.date, list[dt.date]] = defaultdict(list)
    for day in sorted(set(a) | set(b)):
        weeks[_week_of(day)].append(day)
    lines = []
    for monday, days in weeks.items():
        changed = [d for d in days if a.get(d) != b.get(d)]
        edited = sorted({sid for d in days for _, _, sid in b.get(d, []) if sid in old.sessions
                         and _dump(old.sessions[sid]) != _dump(new.sessions[sid])})  # fmt: skip
        if not changed and not edited:
            continue
        def count(plan: Plan, days_map: dict) -> int:
            return sum(1 for d in days for _, _, sid in days_map.get(d, []) if plan.sessions[sid].modality != "rest")

        t0 = sum(tss_of(old, a.get(d, [])) for d in days)
        t1 = sum(tss_of(new, b.get(d, [])) for d in days)
        label = _week_label(new, monday) or _week_label(old, monday)
        lines.append(f"week of {_d(monday)}{f' ({label})' if label else ''}: {count(old, a)} -> {count(new, b)} sessions, "
                     f"TSS {t0:g} -> {t1:g}" + _target(new, monday))  # fmt: skip
        for d in changed:
            lines += [f"  {_d(d)}: {x}" for x in _day_changes(a.get(d), b.get(d))]
        if edited:
            lines.append(f"  edited sessions this week: {', '.join(edited)}")
    if old.valid_to > new.valid_to:
        lines.append(f"{_d(new.valid_to + dt.timedelta(days=1))} to {_d(old.valid_to)} {old.valid_to.year}: no longer planned")
    if lines:
        sections.append(("Sessions by week (from today)", lines))

    # strength rules per lift (sessions still to come)
    def rules(plan: Plan, days: dict) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = defaultdict(dict)
        for sid in {sid for entries in days.values() for _, _, sid in entries}:
            for ex in plan.sessions[sid].exercises or []:
                out[ex.lift][sid] = ex.progression or "by feel"
        return out

    ra, rb = rules(old, a), rules(new, b)
    lines = []
    for lift in sorted(set(ra) | set(rb)):
        for sid in sorted(set(ra.get(lift, {})) | set(rb.get(lift, {}))):
            x, y = ra.get(lift, {}).get(sid), rb.get(lift, {}).get(sid)
            if x != y:
                lines.append(f"{lift} in {sid}: {x or 'not planned'} -> {y or 'not planned'}")
    for name in sorted(set(old.progressions) | set(new.progressions)):
        x, y = old.progressions.get(name), new.progressions.get(name)
        if x is not None and y is not None and _dump(x) != _dump(y):
            changes = ", ".join(f"{k} {_v(_dump(x).get(k))} -> {_v(v)}" for k, v in _dump(y).items() if _dump(x).get(k) != v)
            users = sorted(f"{lift} in {sid}" for lift, m in rb.items() for sid, rule in m.items() if rule == name)
            lines.append(f"rule {name}: {changes}" + (f" (used by {', '.join(users)})" if users else ""))
    if lines:
        sections.append(("Strength rules", lines))

    # session library and strength lines
    lines = [f"added session {sid}" for sid in sorted(set(new.sessions) - set(old.sessions))]
    lines += [f"removed session {sid}" for sid in sorted(set(old.sessions) - set(new.sessions))]
    lines += [f"edited session {sid}" for sid in sorted(set(old.sessions) & set(new.sessions))
              if _dump(old.sessions[sid]) != _dump(new.sessions[sid])]  # fmt: skip
    if lines:
        sections.append(("Session library", lines))
    lines = []
    for line in new.strength_lines:
        old_line = next((x for x in old.strength_lines if x.lift == line.lift), None)
        if old_line is None or old_line.checkpoints != line.checkpoints:
            pts = ", ".join(f"{_d(d)} {v:g}" for d, v in line.checkpoints if d >= today)
            lines.append(f"{line.lift}: checkpoints from today: {pts}")
    if lines:
        sections.append(("Strength lines", lines))
    return sections


def _rng(r) -> str:
    return f"{r[0]:g}-{r[1]:g}" if r else "none"


def _v(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def _week_label(plan: Plan, monday: dt.date) -> str:
    for day in (monday + dt.timedelta(days=i) for i in range(7)):
        for b in plan.blocks:
            if b.start <= day <= b.end:
                w = max((w for w in b.weeks or [] if w.start <= day), key=lambda w: w.start, default=None)
                return f"{b.id} {w.label}" if w else b.id
    return ""


def _target(plan: Plan, monday: dt.date) -> str:
    for b in plan.blocks:
        if b.start <= monday + dt.timedelta(days=6) and monday <= b.end:
            w = max((w for w in b.weeks or [] if w.start <= monday + dt.timedelta(days=6)), key=lambda w: w.start, default=None)
            r = (w.weekly_tss if w and w.weekly_tss else b.weekly_tss)
            return f" (target {_rng(r)})" if r else ""
    return ""


def _day_changes(a, b) -> list[str]:
    """Slot by slot: 'sat_am long_ride_3h30 removed', 'sun_am handbike_easy_40 -> long_ride_3h30'."""
    def by_slot(entries):
        out = defaultdict(list)
        for slot, _, sid in entries or []:
            out[slot].append(sid)
        return out

    x, y = by_slot(a), by_slot(b)
    lines = []
    for slot in sorted(set(x) | set(y), key=lambda s: (s[-2:], s)):
        before, after = " + ".join(x.get(slot, [])), " + ".join(y.get(slot, []))
        if before == after:
            continue
        if not before:
            lines.append(f"{slot} {after} added")
        elif not after:
            lines.append(f"{slot} {before} removed")
        else:
            lines.append(f"{slot} {before} -> {after}")
    if b is None:
        lines.append("no longer covered by the plan")
    return lines


# ------------------------------------------------------------------ calendar preview


def publish_preview(old: Plan, new: Plan, athlete: Athlete, today: dt.date, derived: Path, days: int = PUBLISH_DAYS) -> list[str]:
    """What the next daily publish would do to Pedestal's events in today..today+days-1."""
    from pipeline.prescribe import load_history, load_readiness
    from pipeline.publish.__main__ import build, event_hash

    end = today + dt.timedelta(days=days - 1)
    history = load_history(derived / "strength.csv") if (derived / "strength.csv").exists() else None
    readiness = load_readiness(derived / "wellness.csv")
    before = {e["external_id"]: e for e in build(old, athlete, history, readiness, today, end)[0]}
    after = {e["external_id"]: e for e in build(new, athlete, history, readiness, today, end)[0]}

    def what(e):
        return f"{_d(dt.date.fromisoformat(e['start_date_local'][:10]))} {e['start_date_local'][11:16]} {e['type']}: {e['name']}"

    lines = [f"create {what(e)}" for k, e in after.items() if k not in before]
    lines += [f"update {what(e)}" for k, e in after.items() if k in before and event_hash(before[k]) != event_hash(e)]
    lines += [f"delete {what(e)}" for k, e in before.items() if k not in after]
    return lines or [f"no calendar changes in the next {days} days ({_d(today)} to {_d(end)})"]


# ------------------------------------------------------------------ adopting


def adopt(new_path: Path, current_path: Path, old: Plan, new: Plan, today: dt.date, summary: str, plans_dir: Path) -> Path:
    archive = plans_dir / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / f"{old.plan_id}-{today}.yaml"
    n = 2
    while target.exists():
        target = archive / f"{old.plan_id}-{today}-{n}.yaml"
        n += 1
    shutil.copyfile(current_path, target)
    shutil.copyfile(new_path, current_path)
    history = plans_dir / "HISTORY.md"
    if not history.exists():
        history.write_text("# Plan history\n\nEach adoption, newest last. Archived plans are in `plans/archive/`.\n", encoding="utf-8")
    with open(history, "a", encoding="utf-8") as f:
        f.write(f"\n## {today}: {new.plan_id}\n\n- adopted `{new_path.as_posix()}` (valid {new.valid_from} to {new.valid_to})\n"
                f"- previous plan archived as `{target.as_posix()}`\n- {summary}\n")  # fmt: skip
    return target


def render(sections, refused, preview, fmt: str, title: str) -> str:
    if fmt == "markdown":
        out = [f"### {title}", ""]
        if refused:
            out += ["**Refused: this changes the past.**", ""] + [f"- {r}" for r in refused] + [""]
        for name, lines in sections:
            out += [f"**{name}**", ""] + [f"- {ln.strip()}" if not ln.startswith("  ") else f"  - {ln.strip()}" for ln in lines] + [""]
        if not sections and not refused:
            out += ["No changes from the current plan.", ""]
        if preview is not None:
            out += ["**Calendar (next daily publish, dry run)**", ""] + [f"- {ln}" for ln in preview] + [""]
        return "\n".join(out)
    out = [title, ""]
    if refused:
        out += ["REFUSED: this changes the past:"] + [f"  - {r}" for r in refused] + [""]
    for name, lines in sections:
        out += [f"{name}:"] + [f"  {ln}" for ln in lines] + [""]
    if not sections and not refused:
        out += ["No changes from the current plan.", ""]
    if preview is not None:
        out += ["Calendar (next daily publish, dry run):"] + [f"  {ln}" for ln in preview] + [""]
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.plan.adopt", description="Adopt a plan safely.")
    parser.add_argument("plan", type=Path, help="the new plan file, e.g. plans/winter-2026-27.yaml")
    parser.add_argument("--yes", action="store_true", help="adopt it (default: preview only)")
    parser.add_argument("--current", type=Path, default=Path("config/plan.yaml"))
    parser.add_argument("--athlete", type=Path, default=Path("config/athlete.yaml"))
    parser.add_argument("--today", type=dt.date.fromisoformat, help="default: today in athlete.yaml's schedule timezone")
    parser.add_argument("--derived", type=Path, default=Path("derived"), help="for the calendar preview's weights")
    parser.add_argument("--plans", type=Path, default=Path("plans"), help="where archive/ and HISTORY.md live")
    parser.add_argument("--publish-days", type=int, default=PUBLISH_DAYS)
    parser.add_argument("--format", choices=("text", "markdown"), default="text")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    try:
        new, athlete = load_checked(args.plan, args.athlete)
    except PlanLoadError as e:
        head = "**Invalid plan**" if args.format == "markdown" else "INVALID"
        print(f"{head}: {args.plan}\n" + "\n".join(("- " + ln.strip()) if args.format == "markdown" else ln for ln in e.lines[1:] or e.lines))
        return 1 if e.exit_code == 1 else 2
    try:
        old, _ = load_checked(args.current, args.athlete)
    except PlanLoadError as e:
        print("\n".join(e.lines), file=sys.stderr)
        return 2
    today = args.today or dt.datetime.now(athlete.schedule.zone()).date()

    refused = refusals(old, new, athlete, today)
    sections = diff(old, new, athlete, today)
    preview = None if refused else publish_preview(old, new, athlete, today, args.derived, args.publish_days)
    title = f"Plan check: {args.plan.as_posix()} against {args.current.as_posix()} (today {today})"
    print(render(sections, refused, preview, args.format, title))
    if refused:
        return 1
    if not sections:
        print("nothing to adopt: the plan is the same as the current one")
        return 0
    if not args.yes:
        print("Dry run: nothing changed. Re-run with --yes to adopt it.")
        return 0
    summary = "; ".join(f"{name}: {len(lines)} change(s)" for name, lines in sections)
    archived = adopt(args.plan, args.current, old, new, today, summary, args.plans)
    print(f"Adopted: {args.current} replaced; previous plan archived as {archived}; {args.plans / 'HISTORY.md'} updated.")
    print("Commit config/plan.yaml and plans/; the next daily run publishes it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
