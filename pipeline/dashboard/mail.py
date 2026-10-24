"""The email version: derived/email/*.png, dashboard_email.html and manifest.json.

Email clients don't run JavaScript and Gmail strips SVG, so the email gets static PNG charts:
600 px wide at 2x (1200 px), white background, readable labels, each kept under ~150 KB. The
HTML is email-safe: tables, inline CSS, no scripts, images as `cid:` references listed in
manifest.json (with alt text) for whatever sends the email to attach inline.
"""

import datetime as dt
import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

WIDTH_IN, DPI = 6.0, 200  # 6 in x 200 dpi = 1200 px: 600 px at 2x
MAX_BYTES = 150_000
COLOURS = {"ctl": "#2463c7", "atl": "#c2410c", "form": "#0f766e", "icu": "#78716c", "plan": "#2463c7",
           "green": "#2e7d32", "amber": "#d97706", "red": "#c62828", "none": "#d6d3cd", "band": "#e8f3e9"}  # fmt: skip
SPORT_COLOURS = {"bike": "#2463c7", "strength": "#c2410c", "conditioning": "#7c3aed", "handbike": "#0f766e",
                 "grinding": "#b45309", "sail": "#0891b2", "sail_or_other": "#0891b2"}  # fmt: skip


def _d(iso: str) -> dt.date:
    return dt.date.fromisoformat(iso[:10])


def _style(ax, title: str):
    ax.set_title(title, loc="left", fontsize=11, fontweight="bold", color="#1d1d1b")
    ax.grid(axis="y", color="#e6e5e0", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(labelsize=8, colors="#4b4b47")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))


def _save(fig, path: Path) -> Path:
    fig.patch.set_facecolor("white")
    fig.tight_layout()
    for dpi in (DPI, 150, 100):  # rare: a busy chart drops to 1.5x, then 1x, rather than send a heavy image
        fig.savefig(path, dpi=dpi, facecolor="white", pil_kwargs={"optimize": True})
        if path.stat().st_size <= MAX_BYTES:
            break
    plt.close(fig)
    return path


def fitness_png(f: dict, path: Path) -> Path | None:
    days = f["days"]
    if not days:
        return None
    fig, ax = plt.subplots(figsize=(WIDTH_IN, 2.6))
    x = [_d(d["date"]) for d in days]
    ax.plot(x, [d["ctl"] for d in days], color=COLOURS["ctl"], lw=2, label="CTL (fitness)")
    ax.plot(x, [d["atl"] for d in days], color=COLOURS["atl"], lw=1.6, label="ATL (fatigue)")
    ax.plot(x, [d["form"] for d in days], color=COLOURS["form"], lw=1.6, label="form")
    icu = [(xx, d["icu_ctl"]) for xx, d in zip(x, days) if d["icu_ctl"] is not None]
    if icu:
        ax.plot(*zip(*icu), color=COLOURS["icu"], lw=1, ls="--", label="intervals.icu CTL")
    ahead = [p for p in f["planned"] if _d(p["date"]) <= x[-1] + dt.timedelta(days=42)]
    if ahead:
        ax.plot([_d(p["date"]) for p in ahead], [p["ctl"] for p in ahead], color=COLOURS["plan"], lw=1, ls=":", label="planned CTL (6 weeks)")
    ax.axhline(0, color="#bdbcb6", lw=0.8)
    _style(ax, "Fitness and fatigue")
    ax.legend(fontsize=7, frameon=False, ncol=3, loc="upper left")
    return _save(fig, path)


def weekly_png(weeks: list[dict], path: Path) -> Path | None:
    weeks = [w for w in weeks if not w["future"]][-8:]
    if not weeks:
        return None
    fig, ax = plt.subplots(figsize=(WIDTH_IN, 2.4))
    x = [_d(w["week"]) + dt.timedelta(days=3) for w in weeks]
    bottom = [0.0] * len(weeks)
    for sport in sorted({s for w in weeks for s in w["by_sport"]}):
        vals = [w["by_sport"].get(sport, 0) for w in weeks]
        ax.bar(x, vals, width=5, bottom=bottom, color=SPORT_COLOURS.get(sport, "#78716c"), label=sport.replace("_", " "))
        bottom = [b + v for b, v in zip(bottom, vals)]
    for xx, w in zip(x, weeks):
        if w["band"]:
            ax.fill_between([xx - dt.timedelta(days=3), xx + dt.timedelta(days=3)], w["band"][0], w["band"][1], color=COLOURS["band"], zorder=0)
    ax.scatter(x, [w["planned"] for w in weeks], color="#1d1d1b", s=12, zorder=3, label="planned")
    _style(ax, "Weekly load (TSS)")
    ax.legend(fontsize=7, frameon=False, ncol=4, loc="upper left")
    return _save(fig, path)


def bench_png(bn: dict, today: dt.date, path: Path) -> Path | None:
    if not bn["points"] and not bn["checkpoints"]:
        return None
    fig, ax = plt.subplots(figsize=(WIDTH_IN, 2.4))
    cps = bn["checkpoints"]
    if cps:
        ax.plot([_d(c["date"]) for c in cps], [c["e1rm"] for c in cps], color="#78716c", lw=1, ls="--", label="checkpoint path")
        colour = {"met": COLOURS["green"], "missed": COLOURS["atl"], "upcoming": "#a8a29e", "no data": "#a8a29e"}
        ax.scatter([_d(c["date"]) for c in cps], [c["e1rm"] for c in cps], c=[colour[c["status"]] for c in cps], s=22, zorder=3)
    if bn["points"]:
        ax.plot([_d(p["date"]) for p in bn["points"]], [p["e1rm"] for p in bn["points"]], color=COLOURS["ctl"], lw=2, marker="o", ms=3, label="e1RM")
    ax.axvline(today, color="#bdbcb6", lw=0.8)
    _style(ax, "Bench line (e1RM, kg)")
    ax.legend(fontsize=7, frameon=False, loc="upper left")
    return _save(fig, path)


def readiness_png(days: list[dict], path: Path) -> Path | None:
    if not days:
        return None
    fig, ax = plt.subplots(figsize=(WIDTH_IN, 1.5))
    x = [_d(d["date"]) for d in days]
    ax.bar(x, [1] * len(days), width=0.9, color=[COLOURS.get(d["readiness"] or "none") for d in days])
    ax.set_yticks([])
    _style(ax, "Readiness (green, amber, red; grey = no call)")
    ax.grid(False)
    ax.spines["left"].set_visible(False)
    return _save(fig, path)


def intervals_png(debrief: dict | None, path: Path) -> Path | None:
    reps = [(i["summary"], r) for i in (debrief or {}).get("items", []) for r in i.get("reps", [])]
    if not reps:
        return None
    fig, ax = plt.subplots(figsize=(WIDTH_IN, 0.5 + 0.32 * len(reps)))
    ys = list(range(len(reps)))[::-1]
    for y, (_, r) in zip(ys, reps):
        ax.barh(y, r["band"][1] - r["band"][0], left=r["band"][0], height=0.8, color=COLOURS["band"])
        ax.barh(y, r["watts"], height=0.38, color={"held": COLOURS["form"], "under": COLOURS["atl"], "over": "#b45309"}.get(r["status"], "#78716c"))
        ax.text(r["watts"] + 3, y, f"{r['watts']} W", va="center", fontsize=7, color="#1d1d1b")
    ax.set_yticks(ys, [f"rep {r['n']}" for _, r in reps])
    ax.tick_params(labelsize=8, colors="#4b4b47")
    ax.set_title("Yesterday's intervals (W; shaded = target band)", loc="left", fontsize=11, fontweight="bold")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    return _save(fig, path)


ALT = {
    "fitness_fatigue": "Fitness (CTL), fatigue (ATL) and form over the season, with intervals.icu's CTL and the planned path.",
    "weekly_load": "Weekly training load by sport against the plan's weekly band.",
    "bench_line": "Bench press e1RM against the checkpoint path to the April goal.",
    "readiness_strip": "Readiness per day: green, amber, red or no call.",
    "yesterday_intervals": "Yesterday's interval watts against each rep's target band.",
}


def _p(text: str, style: str = "") -> str:
    return f'<p style="margin:0 0 8px;font:14px/1.45 Arial,sans-serif;color:#1d1d1b;{style}">{text}</p>'


def email_html(d: dict, images: list[dict]) -> str:
    esc = lambda x: html.escape(str(x))  # noqa: E731
    today = _d(d["date"])
    r = d["today"].get("readiness") or {}
    colour = {"green": "#2e7d32", "amber": "#d97706", "red": "#c62828"}.get(r.get("colour") or "", "#78716c")
    rows = [f'<tr><td style="padding:16px 16px 4px"><h1 style="margin:0;font:bold 18px Arial,sans-serif;color:#1d1d1b">Training · {today:%a} {today.day} {today:%b}</h1></td></tr>']
    status = f" ({esc(d['status'])})" if d.get("status") else ""
    rows.append(f'<tr><td style="padding:4px 16px">{_p(f"<span style=\'display:inline-block;padding:2px 8px;border-radius:10px;background:{colour};color:#fff\'>{esc(r.get("colour") or "no call")}</span> {esc(r.get("reason", ""))}{status}")}</td></tr>')
    sessions = d["today"].get("sessions") or []
    lines = []
    for s in sessions:
        line = f"<b>{esc(s['slot'][-2:].upper())}</b> {esc(s['summary'])}"
        for g in s.get("strength") or []:
            kg = f"{g['kg']:g} kg" if g.get("kg") is not None else "choose"
            line += f"<br>&nbsp;&nbsp;{esc(g['name'])} {esc(g['role'])}: {g['sets']} × {g['reps']} @ {esc(kg)}"
        lines.append(_p(line))
    rows.append(f'<tr><td style="padding:4px 16px"><h2 style="margin:8px 0;font:bold 15px Arial,sans-serif">Today</h2>{"".join(lines) or _p("Rest day.")}</td></tr>')
    y = d["charts"].get("yesterday")
    if y:
        items = "".join(_p(f"{esc(i.get('summary') or i.get('name') or i.get('type'))}: <b>{esc(i.get('grade', i.get('kind')))}</b>", "font-size:13px") for i in y["items"])
        rows.append(f'<tr><td style="padding:4px 16px"><h2 style="margin:8px 0;font:bold 15px Arial,sans-serif">Yesterday</h2>{_p("<b>" + esc(y["verdict"]) + "</b>")}{_p("Note for today: " + esc(y["note_for_today"]))}{items}</td></tr>')
    for img in images:
        rows.append(f'<tr><td style="padding:8px 16px"><img src="cid:{img["cid"]}" width="568" style="display:block;width:100%;max-width:568px;height:auto;border:0" alt="{esc(img["alt"])}"></td></tr>')
    rows.append(f'<tr><td style="padding:8px 16px 16px">{_p(f"Generated {esc(d["generated_at"])}. The full interactive dashboard is attached as dashboard.html.", "font-size:12px;color:#6b6b66")}</td></tr>')
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Training</title></head>'
            '<body style="margin:0;padding:0;background:#f4f4f2"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f2">'
            '<tr><td align="center" style="padding:12px"><table role="presentation" width="600" cellpadding="0" cellspacing="0" '
            f'style="width:100%;max-width:600px;background:#ffffff;border-radius:8px">{"".join(rows)}</table></td></tr></table></body></html>')  # fmt: skip


def write(d: dict, derived: Path) -> dict:
    out = derived / "email"
    out.mkdir(parents=True, exist_ok=True)
    ch, today = d["charts"], _d(d["date"])
    made = []
    for cid, fn in (("fitness_fatigue", lambda p: fitness_png(ch["fitness"], p)), ("weekly_load", lambda p: weekly_png(ch["weekly_load"], p)),
                    ("bench_line", lambda p: bench_png(ch["bench"], today, p)), ("readiness_strip", lambda p: readiness_png(ch["readiness"], p)),
                    ("yesterday_intervals", lambda p: intervals_png(ch["yesterday"], p))):  # fmt: skip
        path = out / f"{cid}.png"
        if fn(path):
            made.append({"file": path.name, "cid": cid, "alt": ALT[cid], "bytes": path.stat().st_size})
        elif path.exists():
            path.unlink()  # yesterday's chart mustn't ride along with today's email
    (out / "dashboard_email.html").write_text(email_html(d, made), encoding="utf-8")
    manifest = {"date": d["date"], "status": d.get("status"), "generated_at": d["generated_at"], "html": "dashboard_email.html",
                "attachment": "../dashboard.html", "images": made}  # fmt: skip
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest
