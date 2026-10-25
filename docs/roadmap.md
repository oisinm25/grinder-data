# Pedestal roadmap

Updated 24 Oct 2026. v1.0.0 runs the plan every morning in the cloud. Next come the
features that make it easier to change the plan and easier to see what's happening.

## Before any of this: close out v1.0

- [ ] Prescription basis fix: same-slot history → `start_kg` → e1RM → choose; first bench
      checkpoint 127.0 → 131.5 (Mon 26 bench 100 × 6, Thu 80 × 3 × 10–12).
- [ ] First workflow run green: `data` branch exists, `derived/today.md` written.
- [ ] Push the `v1.0.0` tag.
- [ ] Anchor update from the Block 0 tests (bike FTP, hand-bike FTP, grinding curve) in
      `config/athlete.yaml`.

---

## v1.1: adopt a new plan safely

**Why:** a new plan or a mid-block change should be one clear step, with a preview of what
changes, and no chance of breaking the past or the history the prescriptions depend on.

**How it works**
1. Design in the Training project. Claude produces the complete plan file in the format in
   `docs/plan-format.md`.
2. Save it as `plans/<plan_id>.yaml`. Start from `plans/TEMPLATE.yaml` for a new plan.
3. Run `python -m pipeline.plan.adopt plans/<file>.yaml` (or open a pull request: see below).
   It:
   - validates it against the schema and `athlete.yaml`;
   - checks every lift id exists, every session referenced exists, blocks don't overlap;
   - **refuses to change anything before today**: past dates stay as they were planned,
     so compliance and history stay honest;
   - prints a plain-English diff: blocks added, moved or removed, sessions changed per week,
     weekly TSS before and after, strength rules changed per lift;
   - shows the calendar changes it would publish (dry run);
   - on `--yes`: archives the old plan to `plans/archive/<plan_id>-<date>.yaml`, copies the new
     one to `config/plan.yaml`, records it in `plans/HISTORY.md`, and the next daily run
     publishes it.
4. **From the phone:** commit the new file on a branch and open a pull request. A
   `plan-check` workflow runs the same validation and posts the diff as a PR comment.
   Merge = adopt.

**Format:** `docs/plan-format.md` stays the authority. Add `docs/new-plan.md`: a one-page
cheat sheet (the minimum valid plan, the slot names, the progression types, target anchors,
common mistakes) and `plans/TEMPLATE.yaml` with comments.

---

## v1.2: daily debrief

**Why:** each morning should say how yesterday actually went, not just what today holds.

**`derived/debrief.md` / `debrief.json`**, written by the daily run and included in
`today.md`:
- **Per session:** planned against actual.
  - Bike, hand-bike, erg: each work interval against its target band (held, faded, off target),
    NP, IF, TSS, HR drift (decoupling) on steady rides.
  - Strength: prescribed against achieved per set, RPE, e1RM change, and what it means for next time
    ("hit 100 × 6 @ 8 → +2.5 next Monday").
  - Grinding: watts per rep against target, fade across reps, Grind Load (effort count, work
    minutes, efforts ≥85% LTHR).
  - Sailing: duration, HR load, Grind Load from the HR stream.
- **Day context:** readiness that morning, sleep, total load against plan.
- **One-line verdict** and **one note for today**, generated from rules, not free text
  (e.g. "Threshold faded in rep 2: today stays easy as planned").

---

## v1.3: training dashboard

**Why:** a clear picture of fitness, fatigue and progress toward the A goals, readable on
the phone.

**Load model:** CTL (42-day) and ATL (7-day) EMAs, form = CTL − ATL, from Pedestal's own
daily load: power TSS where available, hrTSS otherwise, plus a Grind Load correction for
grinding and sailing days (hrTSS under-reads short upper-body efforts). intervals.icu's own
CTL shown alongside for comparison.

**Charts**
1. **Fitness and fatigue:** CTL, ATL and form over the season, block boundaries and events
   (ATHX, Festive 500, HYROX, camp, 17 Apr) marked, plus the planned CTL path.
2. **Weekly load:** actual TSS by sport against the plan's `weekly_tss` band.
3. **Yesterday:** the debrief as a card, plus interval bars against target bands.
4. **Bench line:** logged e1RM against the checkpoint path to 147 kg, with each
   checkpoint's status.
5. **Lower-body and pulling lines:** trap-bar and pull e1RM trends.
6. **Grinding curve:** 1-min and 4-min bests over time against the April targets.
7. **Bike FTP and durability:** FTP tests, long-ride duration, late-ride power.
8. **Readiness strip:** green, amber or red per day, with HRV and resting HR against baseline.
9. **Compliance:** held, faded, missed per week.
10. **Body composition:** 7-day average bodyweight (DEXA points when they exist).

**Where it lives**
- `derived/dashboard.json`: chart-ready data, written daily on the `data` branch.
- `derived/dashboard.html`: one self-contained page (no network) built from it, for opening
  locally or attaching.
- The morning brief (Claude scheduled task) reads `dashboard.json` and renders the
  headline charts (fitness and fatigue, yesterday, bench line, readiness) in the styled brief.

**Email version** (email clients don't run JavaScript, and Gmail strips SVG, so the
interactive page can't be sent as the email body):
- `derived/email/*.png`: static charts at 600 px wide, 2× for sharp phone screens, white
  background, readable labels, each under ~150 KB. Fitness and fatigue, weekly load,
  bench line, readiness strip, yesterday's intervals.
- `derived/email/dashboard_email.html`: email-safe HTML (tables, inline CSS, no scripts),
  referencing the charts as `cid:` inline images: readiness, today's session, yesterday's
  debrief, then the charts.
- `derived/email/manifest.json`: list of image files with their `cid` and alt text.
- The morning brief sends one email to Gmail: its own day plan first, then the training
  section from `dashboard_email.html` with the PNGs inline, and `dashboard.html` attached
  for the full interactive view.
- If today's files are missing or stale, the email says so and leaves the charts out.

---

## v1.4: on-demand workout analysis ("run" from a chat)

**Why:** after a key session, see the breakdown and what it did to fitness straight away,
not the next morning.

**Trigger:** a chat with Claude (with `grinder-data` attached) pushes a request file to a
`requests` branch: `requests/<timestamp>.json` with `{"activity": "latest" | "<id>",
"requested_at": "..."}`. Workflow `analyse.yml` runs on push to `requests` (and on
`workflow_dispatch`), separate `concurrency` group from the daily run, no time window.

**`python -m pipeline.analyse --activity latest|<id>`:**
1. Ingest the last 3 days (activities, wellness, strength log).
2. If no activity newer than the last analysed one exists: write status `no_new_activity`.
3. Breakdown: intervals vs targets, NP, IF, TSS, HR drift, power-curve points; strength
   sets vs prescription and e1RM change; grinding reps, fade, Grind Load.
4. Fitness impact: session load; CTL, ATL and form before and after; projected form
   tomorrow and on the next key session using the plan's planned load; share of the
   week's `weekly_tss` done.
5. New bests and anchor flags (e.g. a 20-min effort implying FTP more than 3% above the
   anchor). Flags only: never edits `athlete.yaml` or the plan.
6. Writes `derived/analysis/<date>-<activity_id>.json` and `.md`, plus `latest.json`, and
   commits to the `data` branch.
7. Never publishes to the calendar and never touches `today.json`.

**In the chat:** Claude reads `latest.json`, shows the breakdown and charts, and
republishes one persistent "Workout debrief" page.

---

## Later ideas

- Daily Inputs sheet (Google Sheets) as the logging front end, pre-filled with prescriptions.
- Grinding machine power import, if the machine can export.
- Coach view and multi-athlete support (see `ABOUT.md`).
