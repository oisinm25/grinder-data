"""derived/dashboard.html: one self-contained page built from dashboard.json.

Charts are inline SVG drawn here (no chart library, no scripts, no network requests), with a
<title> tooltip on every point. Colours are CSS variables, switched for dark mode by
`prefers-color-scheme`; the layout is a single column on a phone and a grid on wider screens.
"""

import datetime as dt
import html
import json

W, PAD_L, PAD_R, PAD_T, PAD_B = 640, 44, 12, 14, 26
SPORT_CLASS = {"bike": "c1", "strength": "c2", "conditioning": "c3", "handbike": "c4", "grinding": "c5", "sail": "c6",
               "run": "c7", "concept2": "c7", "sail_or_other": "c6", "walk": "c8", "other": "c8"}  # fmt: skip

CSS = """
:root { --bg:#f7f7f5; --card:#fff; --ink:#1d1d1b; --muted:#6b6b66; --grid:#e6e5e0; --band:rgba(46,125,50,.10);
  --c1:#2463c7; --c2:#c2410c; --c3:#7c3aed; --c4:#0f766e; --c5:#b45309; --c6:#0891b2; --c7:#4d7c0f; --c8:#78716c;
  --green:#2e7d32; --amber:#d97706; --red:#c62828; --none:#d6d3cd; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --card:#1e1e1c; --ink:#ecebe6; --muted:#a3a29b; --grid:#33322f;
  --band:rgba(129,199,132,.14); --c1:#6ea8ff; --c2:#fb923c; --c3:#a78bfa; --c4:#2dd4bf; --c5:#fbbf24; --c6:#22d3ee;
  --c7:#a3e635; --c8:#a8a29e; --green:#66bb6a; --amber:#fbbf24; --red:#ef5350; --none:#4a4945; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif }
main { max-width:1200px; margin:0 auto; padding:16px } h1 { font-size:20px; margin:4px 0 2px } .sub { color:var(--muted); margin:0 0 16px }
.grid { display:grid; grid-template-columns:1fr; gap:14px } @media (min-width:900px) { .grid { grid-template-columns:1fr 1fr } .wide { grid-column:1/-1 } }
section { background:var(--card); border-radius:12px; padding:14px 14px 10px; box-shadow:0 1px 2px rgba(0,0,0,.06); min-width:0 }
.sub { overflow-wrap:anywhere }
h2 { font-size:15px; margin:0 0 6px } p.note { color:var(--muted); font-size:13px; margin:6px 0 0 }
svg { width:100%; height:auto; display:block } svg text { fill:var(--muted); font-size:12px } .gl { stroke:var(--grid) }
@media (min-width:900px) { section:not(.wide) svg text { font-size:17px } }
@media (max-width:600px) { svg text { font-size:20px } }
.l { fill:none; stroke-width:2 } .thin { stroke-width:1.2; stroke-dasharray:4 3 } .pt { stroke:none }
.c1{stroke:var(--c1);fill:var(--c1)} .c2{stroke:var(--c2);fill:var(--c2)} .c3{stroke:var(--c3);fill:var(--c3)} .c4{stroke:var(--c4);fill:var(--c4)}
.c5{stroke:var(--c5);fill:var(--c5)} .c6{stroke:var(--c6);fill:var(--c6)} .c7{stroke:var(--c7);fill:var(--c7)} .c8{stroke:var(--c8);fill:var(--c8)}
.l.c1,.l.c2,.l.c3,.l.c4,.l.c5,.l.c6,.l.c7,.l.c8 { fill:none } .band { fill:var(--band); stroke:none } .ev { stroke:var(--muted); stroke-dasharray:2 3 }
.green{fill:var(--green)} .amber{fill:var(--amber)} .red{fill:var(--red)} .none{fill:var(--none)}
.legend { display:flex; flex-wrap:wrap; gap:10px; font-size:12px; color:var(--muted); margin:4px 0 0 }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:-1px }
table { width:100%; border-collapse:collapse; font-size:13px } td,th { text-align:left; padding:4px 6px; border-bottom:1px solid var(--grid) }
.pill { display:inline-block; padding:1px 8px; border-radius:10px; color:#fff; font-size:12px }
.pill.green{background:var(--green)} .pill.amber{background:var(--amber)} .pill.red{background:var(--red)} .pill.none{background:var(--c8)}
.big { font-size:16px; font-weight:600 } .empty { color:var(--muted); font-size:13px; padding:12px 0 }
"""


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def _d(iso: str) -> dt.date:
    return dt.date.fromisoformat(iso[:10])


def _fmt(day: dt.date) -> str:
    return f"{day.day} {day:%b}"


def _nice(lo: float, hi: float, n: int = 4) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 0.1
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = int(lo // step) * step
    out, v = [], start
    while v <= hi + step * 0.01:
        out.append(round(v, 2))
        v += step
    return out


class Chart:
    """A date x numeric chart: lines, bands, bars and markers, rendered as one SVG."""

    def __init__(self, x0: dt.date, x1: dt.date, y0: float, y1: float, h: int = 200, unit: str = ""):
        self.x0, self.x1 = x0, max(x1, x0 + dt.timedelta(days=1))
        self.ticks = _nice(y0, y1)
        self.y0, self.y1 = min(self.ticks[0], y0), max(self.ticks[-1], y1)
        self.h, self.unit, self.parts = h, unit, []

    def x(self, day: dt.date) -> float:
        span = (self.x1 - self.x0).days
        return PAD_L + (day - self.x0).days / span * (W - PAD_L - PAD_R)

    def y(self, v: float) -> float:
        return PAD_T + (1 - (v - self.y0) / ((self.y1 - self.y0) or 1)) * (self.h - PAD_T - PAD_B)

    def line(self, pts, cls, name, thin=False):
        pts = [(d, v) for d, v in pts if v is not None]
        if not pts:
            return
        path = " ".join(f"{'M' if i == 0 else 'L'}{self.x(d):.1f},{self.y(v):.1f}" for i, (d, v) in enumerate(pts))
        self.parts.append(f'<path class="l {cls}{" thin" if thin else ""}" d="{path}"><title>{esc(name)}</title></path>')
        if thin:  # reference lines (planned path, intervals.icu's CTL) stay plain
            return
        r = 2.2 if len(pts) < 60 else 1.4
        for d, v in pts:
            self.parts.append(f'<circle class="pt {cls}" cx="{self.x(d):.1f}" cy="{self.y(v):.1f}" r="{r}"><title>{esc(name)} {_fmt(d)}: {v:g}{self.unit}</title></circle>')

    def points(self, pts, cls, name, labels=None, r=3.5):
        for i, (d, v) in enumerate(pts):
            tip = labels[i] if labels else f"{_fmt(d)}: {v:g}{self.unit}"
            self.parts.append(f'<circle class="pt {cls}" cx="{self.x(d):.1f}" cy="{self.y(v):.1f}" r="{r}"><title>{esc(name)} {esc(tip)}</title></circle>')

    def band(self, x0: dt.date, x1: dt.date, lo: float, hi: float, tip: str = ""):
        x, w = self.x(x0), max(self.x(x1) - self.x(x0), 2)
        self.parts.append(f'<rect class="band" x="{x:.1f}" y="{self.y(hi):.1f}" width="{w:.1f}" height="{self.y(lo) - self.y(hi):.1f}"><title>{esc(tip)}</title></rect>')

    def vline(self, day: dt.date, label: str = ""):
        if not self.x0 <= day <= self.x1:
            return
        x = self.x(day)
        self.parts.append(f'<line class="ev" x1="{x:.1f}" x2="{x:.1f}" y1="{PAD_T}" y2="{self.h - PAD_B}"><title>{esc(label)}</title></line>')
        if label:
            right = x > W * 0.8  # near the right edge, the label goes to the left of its line
            anchor = ' text-anchor="end"' if right else ""
            self.parts.append(f'<text x="{x - 3 if right else x + 3:.1f}" y="{PAD_T + 9}"{anchor}>{esc(label)}</text>')

    def bar(self, day: dt.date, width_days: float, lo: float, hi: float, cls: str, tip: str):
        x = self.x(day) + 1
        w = max(self.x(day + dt.timedelta(days=width_days)) - self.x(day) - 3, 2)
        self.parts.append(f'<rect class="{cls} pt" x="{x:.1f}" y="{self.y(hi):.1f}" width="{w:.1f}" height="{max(self.y(lo) - self.y(hi), 0):.1f}"><title>{esc(tip)}</title></rect>')

    def svg(self, label: str) -> str:
        axes = []
        for t in self.ticks:
            y = self.y(t)
            axes.append(f'<line class="gl" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/><text x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">{t:g}</text>')
        span = (self.x1 - self.x0).days
        step = 7 if span <= 70 else 14 if span <= 140 else 28
        day = self.x0 + dt.timedelta(days=(7 - self.x0.weekday()) % 7)
        while day <= self.x1:
            axes.append(f'<text x="{self.x(day):.1f}" y="{self.h - 8}" text-anchor="middle">{_fmt(day)}</text>')
            day += dt.timedelta(days=step)
        return f'<svg viewBox="0 0 {W} {self.h}" role="img" aria-label="{esc(label)}">{"".join(axes)}{"".join(self.parts)}</svg>'


def legend(items) -> str:
    return '<div class="legend">' + "".join(f'<span><i class="{c}" style="background:var(--{c})"></i>{esc(n)}</span>' for n, c in items) + "</div>"


def card(title: str, body: str, note: str = "", wide: bool = False) -> str:
    return f'<section class="{"wide" if wide else ""}"><h2>{esc(title)}</h2>{body}' + (f'<p class="note">{esc(note)}</p>' if note else "") + "</section>"


def empty(msg: str) -> str:
    return f'<div class="empty">{esc(msg)}</div>'


# ------------------------------------------------------------------ the ten charts


def fitness_chart(f: dict) -> str:
    days = f["days"]
    if not days:
        return card("Fitness and fatigue", empty("No activities yet."), wide=True)
    planned = f["planned"]
    x0, x1 = _d(days[0]["date"]), _d((planned or days)[-1]["date"])
    vals = [v for d in days for v in (d["ctl"], d["atl"], d["form"], d["icu_ctl"]) if v is not None] + [p["ctl"] for p in planned]
    c = Chart(x0, x1, min(vals + [0]), max(vals), h=240)
    for b in f["blocks"]:
        if b["ctl_end"]:
            c.band(_d(b["end"]) - dt.timedelta(days=2), _d(b["end"]), b["ctl_end"][0], b["ctl_end"][1], f"{b['name']}: CTL {b['ctl_end'][0]}-{b['ctl_end'][1]} by the end")
    for b in f["blocks"]:
        c.vline(_d(b["start"]))
    labels: dict[str, list[str]] = {}
    for e in f["events"]:  # goals on the same day share one line and label
        labels.setdefault(e["date"], []).append(e["label"])
    for day, names in labels.items():
        c.vline(_d(day), " + ".join(names))
    c.line([(_d(p["date"]), p["ctl"]) for p in planned], "c1", "planned CTL", thin=True)
    c.line([(_d(d["date"]), d["icu_ctl"]) for d in days], "c8", "intervals.icu CTL", thin=True)
    c.line([(_d(d["date"]), d["atl"]) for d in days], "c2", "ATL (fatigue)")
    c.line([(_d(d["date"]), d["form"]) for d in days], "c4", "form")
    c.line([(_d(d["date"]), d["ctl"]) for d in days], "c1", "CTL (fitness)")
    last = days[-1]
    note = f"Today: CTL {last['ctl']}, ATL {last['atl']}, form {last['form']}. Seeded from intervals.icu on {f['seed']['date']} (CTL {f['seed']['ctl']}). Dashed lines: the plan's CTL path and intervals.icu's own CTL; shaded: a block's CTL target."
    return card("Fitness and fatigue", c.svg("CTL, ATL and form") + legend([("CTL", "c1"), ("ATL", "c2"), ("form", "c4"), ("intervals.icu CTL", "c8")]), note, wide=True)


def weekly_chart(weeks: list[dict]) -> str:
    if not weeks:
        return card("Weekly load", empty("No weeks yet."))
    sports = sorted({s for w in weeks for s in w["by_sport"]})
    top = max([w["total"] for w in weeks] + [w["band"][1] for w in weeks if w["band"]] + [w["planned"] for w in weeks] + [1])
    c = Chart(_d(weeks[0]["week"]), _d(weeks[-1]["week"]) + dt.timedelta(days=7), 0, top, h=220, unit=" TSS")
    for w in weeks:
        mon = _d(w["week"])
        if w["band"]:
            c.band(mon, mon + dt.timedelta(days=7), w["band"][0], w["band"][1], f"week of {_fmt(mon)}: target {w['band'][0]:g}-{w['band'][1]:g} TSS")
        base = 0
        for s in sports:
            v = w["by_sport"].get(s, 0)
            if v:
                c.bar(mon, 7, base, base + v, SPORT_CLASS.get(s, "c8"), f"week of {_fmt(mon)}: {s} {v:g} TSS")
                base += v
        c.points([(mon + dt.timedelta(days=3.5), w["planned"])], "c8", "planned", [f"week of {_fmt(mon)}: {w['planned']} TSS"], r=3)
    return card("Weekly load", c.svg("weekly load by sport") + legend([(s, SPORT_CLASS.get(s, "c8")) for s in sports] + [("planned", "c8")]),
                "Bars: actual TSS by sport (with the Grind Load correction); shaded: the plan's weekly band; dots: planned TSS.")  # fmt: skip


def yesterday_card(b: dict | None) -> str:
    if not b:
        return card("Yesterday", empty("No debrief for yesterday."))
    rows = []
    for i in b["items"]:
        name = i.get("summary") or i.get("name") or i.get("type")
        grade = i.get("grade", i.get("kind"))
        rows.append(f"<tr><td>{esc(name)}</td><td>{esc(grade)}</td></tr>")
    bars = ""
    reps = [(i["summary"], r) for i in b["items"] for r in i.get("reps", [])]
    if reps:
        top = max(max(r["band"][1], r["watts"]) for _, r in reps) * 1.08
        h = 22 * len(reps) + 10
        parts = []
        for k, (_, r) in enumerate(reps):
            y = 6 + k * 22
            sx = lambda w: PAD_L + w / top * (W - PAD_L - PAD_R)  # noqa: E731
            cls = {"held": "c4", "under": "c2", "over": "c5"}.get(r["status"], "c8")
            parts.append(f'<rect class="band" x="{sx(r["band"][0]):.1f}" y="{y}" width="{sx(r["band"][1]) - sx(r["band"][0]):.1f}" height="16"/>')
            parts.append(f'<rect class="{cls} pt" x="{PAD_L}" y="{y + 4}" width="{sx(r["watts"]) - PAD_L:.1f}" height="8"><title>rep {r["n"]}: {r["watts"]} W against {esc(r["target"])} ({r["status"]})</title></rect>')
            parts.append(f'<text x="{PAD_L - 6}" y="{y + 12}" text-anchor="end">{r["n"]}</text>')
        bars = f'<svg viewBox="0 0 {W} {h}" role="img" aria-label="interval bars">{"".join(parts)}</svg>'
    body = f'<div class="big">{esc(b["verdict"])}</div><p class="note">Note for today: {esc(b["note_for_today"])}</p>'
    body += f"<table>{''.join(rows)}</table>" if rows else ""
    return card(f"Yesterday ({_fmt(_d(b['date']))})", body + bars, "Bars: each rep's watts; shaded: its target band." if reps else "")


def bench_chart(bn: dict, today: dt.date) -> str:
    cps, pts = bn["checkpoints"], bn["points"]
    if not cps and not pts:
        return card("Bench line", empty("No bench data yet."))
    x0 = min([_d(p["date"]) for p in pts] + [_d(c["date"]) for c in cps])
    x1 = max([_d(c["date"]) for c in cps] + [today])
    vals = [p["e1rm"] for p in pts] + [c["e1rm"] for c in cps]
    c = Chart(x0, x1, min(vals) - 3, max(vals) + 3, h=210, unit=" kg")
    c.line([(_d(k["date"]), k["e1rm"]) for k in cps], "c8", "checkpoint path", thin=True)
    status_cls = {"met": "c4", "missed": "c2", "upcoming": "c8", "no data": "c8"}
    for k in cps:
        c.points([(_d(k["date"]), k["e1rm"])], status_cls[k["status"]], "checkpoint", [f"{_fmt(_d(k['date']))}: {k['e1rm']:g} kg ({k['status']})"], r=4.5)
    c.line([(_d(p["date"]), p["e1rm"]) for p in pts], "c1", "e1RM")
    c.vline(today, "today")
    goal = bn["goal"]
    note = f"Logged e1RM against the path to {goal['e1rm_needed_kg']:g} kg by {goal['date']}. Checkpoints: green met (within one rep), orange missed, grey upcoming." if goal and goal.get("e1rm_needed_kg") else ""
    return card("Bench line", c.svg("bench e1RM") + legend([("e1RM", "c1"), ("checkpoints", "c8")]), note)


def lifts_chart(lifts: dict) -> str:
    pts = [(lift, p) for lift, ps in lifts.items() for p in ps]
    if not pts:
        return card("Lower body and pulling", empty("No trap-bar or pulling sets with an e1RM yet."))
    c = Chart(min(_d(p["date"]) for _, p in pts), max(_d(p["date"]) for _, p in pts) + dt.timedelta(days=7),
              min(p["e1rm"] for _, p in pts) - 5, max(p["e1rm"] for _, p in pts) + 5, h=190, unit=" kg")  # fmt: skip
    cls = {"trap_bar_deadlift": "c2", "weighted_pullup": "c4", "bench_pull": "c3"}
    for lift, ps in lifts.items():
        c.line([(_d(p["date"]), p["e1rm"]) for p in ps], cls.get(lift, "c8"), lift.replace("_", " "))
    return card("Lower body and pulling", c.svg("trap-bar and pulling e1RM") + legend([(k.replace("_", " "), v) for k, v in cls.items()]))


def grinding_card(g: dict) -> str:
    rows = []
    for key, label in (("60s", "1-min"), ("240s", "4-min")):
        now, target = g["now"].get(key), g["target"].get(key)
        if now is None:
            continue
        pct = f"{now / target:.0%}" if target else ""
        bar = f'<svg viewBox="0 0 {W} 26"><rect class="band" x="{PAD_L}" y="4" width="{W - PAD_L - PAD_R}" height="18"/><rect class="c5 pt" x="{PAD_L}" y="8" width="{(now / target if target else 1) * (W - PAD_L - PAD_R):.1f}" height="10"><title>{label}: {now:g} W of {target or "?"} W</title></rect></svg>'
        rows.append(f"<tr><td>{label}</td><td>{now:g} W</td><td>{target or '-'} W</td><td>{pct}</td></tr><tr><td colspan=4>{bar}</td></tr>")
    body = "<table><tr><th>Best</th><th>Now</th><th>April target</th><th></th></tr>" + "".join(rows) + "</table>"
    return card("Grinding curve", body, "Now = athlete.yaml; targets from training/goals.md. Test history appears once grinding power is recorded.")


def bike_chart(bk: dict, today: dt.date) -> str:
    rides, late, ftps = bk["long_rides"], bk["late_ride_power"], bk["ftp_intervals_icu"]
    body, notes = "", []
    if rides:
        c = Chart(min(_d(r["date"]) for r in rides) - dt.timedelta(days=3), today, 0, max(r["minutes"] for r in rides) / 60 + 1, h=170, unit=" h")
        c.points([(_d(r["date"]), round(r["minutes"] / 60, 1)) for r in rides], "c1", "long ride", [f"{_fmt(_d(r['date']))}: {r['minutes'] / 60:.1f} h, {r['name']}" for r in rides], r=4)
        body += c.svg("long-ride duration")
        notes.append("Dots: rides of 2 h or more, in hours.")
    if late:
        notes.append("Late-ride power (last hour against the first): " + ", ".join(f"{_fmt(_d(x['date']))} {x['ratio']:.0%}" for x in late) + ".")
    ftp_bits = [f"athlete.yaml {bk['ftp_now']:g} W"] if bk["ftp_now"] else []
    if ftps:
        ftp_bits.append("intervals.icu " + ", ".join(f"{x['ftp']:g} W from {_fmt(_d(x['date']))}" for x in ftps))
    if bk["ftp_target"]:
        ftp_bits.append(f"April target {'-'.join(f'{v:g}' for v in bk['ftp_target'])} W")
    head = f'<div class="big">FTP: {esc("; ".join(ftp_bits))}</div>' if ftp_bits else ""
    return card("Bike FTP and durability", head + (body or empty("No rides of 2 h or more yet.")), " ".join(notes))


def readiness_strip(days: list[dict]) -> str:
    if not days:
        return card("Readiness", empty("No wellness data yet."), wide=True)
    n, cell = len(days), (W - PAD_L - PAD_R) / max(len(days), 1)
    parts = []
    for i, d in enumerate(days):
        cls = d["readiness"] or "none"
        tip = f"{d['date']}: {d['readiness'] or 'no call'}; HRV {d['hrv'] or '-'} (baseline {d['hrv_baseline'] or '-'}), RHR {d['resting_hr'] or '-'} (baseline {d['resting_hr_baseline'] or '-'})"
        parts.append(f'<rect class="{cls}" x="{PAD_L + i * cell:.1f}" y="6" width="{max(cell - 1.5, 1):.1f}" height="22" rx="2"><title>{esc(tip)}</title></rect>')
    strip = f'<svg viewBox="0 0 {W} 34" role="img" aria-label="readiness per day">{"".join(parts)}</svg>'
    hrv = [(_d(d["date"]), d["hrv"]) for d in days if d["hrv"]]
    base = [(_d(d["date"]), d["hrv_baseline"]) for d in days if d["hrv_baseline"]]
    chart = ""
    if hrv:
        vals = [v for _, v in hrv + base]
        c = Chart(_d(days[0]["date"]), _d(days[-1]["date"]), min(vals) - 5, max(vals) + 5, h=150, unit=" ms")
        c.line(base, "c8", "HRV baseline", thin=True)
        c.line(hrv, "c4", "HRV")
        chart = c.svg("HRV against baseline")
    last = days[-1]
    note = f"{n} days: green, amber, red, grey = no call. Latest {last['date']}: resting HR {last['resting_hr'] or '-'} (baseline {last['resting_hr_baseline'] or '-'})."
    return card("Readiness", strip + chart + legend([("green", "green"), ("amber", "amber"), ("red", "red"), ("HRV", "c4"), ("baseline", "c8")]), note, wide=True)


def compliance_table(weeks: list[dict]) -> str:
    if not weeks:
        return card("Compliance", empty("Nothing graded yet."))
    cols = ["held", "faded", "off_target", "done", "missed", "skipped", "strength_held", "strength_under", "strength_skipped"]
    head = "".join(f"<th>{c.replace('strength_', 'str ').replace('_', ' ')}</th>" for c in cols)
    rows = "".join(f"<tr><td>{_fmt(_d(w['week']))}</td>" + "".join(f"<td>{w.get(c, '')}</td>" for c in cols) + "</tr>" for w in weeks[-8:])
    return card("Compliance", f'<div style="overflow-x:auto"><table><tr><th>Week</th>{head}</tr>{rows}</table></div>', "Per week: endurance grades, then strength (str).")


def body_chart(b: dict) -> str:
    pts = b["weight"]
    if not pts:
        return card("Body composition", empty("No bodyweight logged in intervals.icu yet. DEXA points appear here when they exist."))
    c = Chart(_d(pts[0]["date"]), _d(pts[-1]["date"]), min(p["weight_kg"] for p in pts) - 1, max(p["weight_kg"] for p in pts) + 1, h=160, unit=" kg")
    c.points([(_d(p["date"]), p["weight_kg"]) for p in pts], "c8", "weight", r=2.5)
    c.line([(_d(p["date"]), p["avg7_kg"]) for p in pts], "c1", "7-day average")
    return card("Body composition", c.svg("bodyweight") + legend([("7-day average", "c1"), ("daily", "c8")]))


def render(d: dict) -> str:
    today = _d(d["date"])
    ch = d["charts"]
    r = d["today"].get("readiness") or {}
    colour = r.get("colour") or "none"
    status = f" · {d['status']}" if d.get("status") else ""
    head = (f'<h1>Pedestal · {today:%a} {today.day} {today:%b %Y}</h1><p class="sub"><span class="pill {colour}">{esc(r.get("colour") or "no call")}</span> '
            f'{esc(r.get("reason", ""))}{esc(status)} · generated {esc(d["generated_at"])}</p>')  # fmt: skip
    sections = [fitness_chart(ch["fitness"]), weekly_chart(ch["weekly_load"]), yesterday_card(ch["yesterday"]), bench_chart(ch["bench"], today),
                lifts_chart(ch["lifts"]), grinding_card(ch["grinding"]), bike_chart(ch["bike"], today), readiness_strip(ch["readiness"]),
                compliance_table(ch["compliance"]), body_chart(ch["body"])]  # fmt: skip
    data = json.dumps(d, separators=(",", ":")).replace("</", "<\\/")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
            f"<title>Pedestal dashboard</title><style>{CSS}</style></head><body><main>{head}<div class=grid>{''.join(sections)}</div>"
            f'<script type="application/json" id="dashboard-data">{data}</script></main></body></html>')  # fmt: skip
