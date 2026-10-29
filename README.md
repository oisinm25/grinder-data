# Pedestal (grinder-data)

Training plan, goals and the measurement pipeline for Oisin McClelland's grinder training.

- `training/goals.md` — winter 2026–27 A and C goals, checkpoints, review cadence
- `training/blocks.md` — block calendar and bike/grinding progression
- `training/design-rules.md` — the rules the plan is built and adjusted from
- `config/plan.yaml` — machine-readable plan (format: `docs/plan-format.md`)
- `config/athlete.yaml` — anchors the plan points at: FTPs, LTHR, grinding curve, strength rounding, readiness rules, lift ids

Design happens in the Claude Training project; Oisin reviews and commits.

Version: v1.0.0, all phases running end to end, every morning, in the cloud.

| Phase | What | State |
|---|---|---|
| 1 | Plan and athlete schemas, validator CLI | done |
| 2 | Plan expanded into dated daily sessions, targets resolved to watts | done |
| 3 | Ingest: intervals.icu activities and wellness, strength log, into `derived/` | done |
| 4 | Strength prescriptions: progression rules, basis, readiness, rounding | done |
| 5 | Publish the next days to intervals.icu (dry run by default), `.zwo`/`.fit` exports | done |
| 6 | Planned against actual: compliance grading, Sunday drift check | done |
| 7 | Automation: `pipeline.run`, the daily brief, ntfy failure alerts, GitHub Actions in the cloud | done |

## Setup

Everything runs in the conda env `pedestal`:

```
conda run -n pedestal pip install -e ".[dev]"
git config core.hooksPath .githooks
```

The second line turns on the pre-commit check (once per clone). It refuses commits that
stage `.env`, `.venv/`, `derived/` or `secrets/`, or anything that looks like an API key
or `github_pat_` token.

## Use

Check a plan (also checks `config/athlete.yaml`, found beside it):

```
conda run -n pedestal python -m pipeline.plan.schema config/plan.yaml
```

List sessions by date, with targets in watts and planned TSS against each week's target
(default: today plus 6 days):

```
conda run -n pedestal python -m pipeline.plan.expand config/plan.yaml --from 2026-10-26 --days 7
conda run -n pedestal python -m pipeline.plan.expand config/plan.yaml --from 2026-10-26 --to 2026-11-22 --csv derived/schedule.csv
```

Watts marked `*` come from a provisional anchor (the hand-bike FTP until it's tested).
`rpe` and `hr_zone` targets stay as written. Strength sessions list their lifts and sets;
weights come from `pipeline.prescribe`.

Ingest intervals.icu activities and wellness, plus `training/logs/strength.csv`, into
`derived/activities.csv`, `derived/wellness.csv` and `derived/strength.csv` (needs
`INTERVALS_ICU_API_KEY` and `INTERVALS_ICU_ATHLETE_ID` in `.env`; raw responses are cached
in `cache/`; re-runs replace rows in the date range, never duplicate):

```
conda run -n pedestal python -m pipeline.ingest --from 2026-10-14
conda run -n pedestal python -m pipeline.ingest --from 2026-10-14 --offline   # from the cache
```

Each wellness day gets a computed `readiness` (green / amber / red, with the reason) from
HRV, resting HR and sleep against a rolling 28-day baseline (`pipeline/readiness.py`,
thresholds in `athlete.yaml` `readiness.rules`); wellness is fetched from 28 days before
`--from` so the first day has a baseline. Activities are mapped by title, then type
(`map_sport` in `pipeline/ingest/tidy.py`); a plain `Workout` is flagged for review.

Suggest strength weights (kg, reps and RPE cap per group, with the reason) into
`derived/prescriptions.csv`:

```
conda run -n pedestal python -m pipeline.prescribe --from 2026-10-26 --days 7
```

Publish sessions to the intervals.icu calendar, which forwards them to Garmin and Zwift.
Without `--push` it's a dry run: the events go to `exports/intervals_events_*.json` and
nothing is sent. Fallback `.fit` (endurance) and `.zwo` (bike) files go to
`exports/workouts/` either way. Only events whose `external_id` starts `pedestal:` are
ever updated or deleted:

```
conda run -n pedestal python -m pipeline.publish --from 2026-10-26 --days 7
conda run -n pedestal python -m pipeline.publish --from 2026-10-26 --days 7 --push
```

Grade the week against the plan into `derived/compliance.csv` and run the Sunday drift
check into `derived/weekly.csv` (run ingest first; `--no-intervals` skips power grading):

```
conda run -n pedestal python -m pipeline.review --week-ending 2026-11-01
```

Run the tests:

```
conda run -n pedestal python -m pytest
```

## Changing the plan

Design in the Training project, save the complete file as `plans/<plan_id>.yaml` (a new plan
starts from `plans/TEMPLATE.yaml`; cheat sheet: `docs/new-plan.md`), then:

```
conda run -n pedestal python -m pipeline.plan.adopt plans/<plan_id>.yaml        # check + preview
conda run -n pedestal python -m pipeline.plan.adopt plans/<plan_id>.yaml --yes  # adopt
```

It validates the file, refuses any change before today, prints a plain-English diff
(blocks, sessions and TSS per week, strength rules per lift) and the calendar changes the
next daily run would make. `--yes` archives the old plan to `plans/archive/`, copies the new
one to `config/plan.yaml` and records it in `plans/HISTORY.md`; commit those. From the phone,
open a pull request with the plan file: the `plan-check` workflow comments the same diff.

## Moving a session

To move, swap or drop a session without touching the plan, add an entry to
`training/logs/changes.yaml`:

```yaml
- { type: move, session: upper_me, from: 2026-10-26 am, to: 2026-10-27 am, reason: "skipped Monday" }
- { type: swap, a: 2026-10-27 pm, b: 2026-10-28 pm }
- { type: drop, session: z2_60, date: 2026-10-28 pm, reason: "travel" }
- { type: done_late, session: upper_me, planned: 2026-10-26 am, done: 2026-10-27 pm }
```

`done_late` records after the fact that a session was done later than planned. It applies like
a move, but it's the one kind you can add for past dates. Its slot shows "done late: Tue PM",
and the session shows "(done late, planned Mon)".

`pipeline.plan.expand` applies the entries in file order. This affects everything downstream:
- **Calendar:** the next publish updates the affected days only.
- **Compliance and debrief:** a moved session is graded on its new date. The slot it left shows
  "moved to Tue AM", or "dropped" with the reason.
- **Prescriptions:** a moved session keeps the same session-and-slot history, so a moved
  `upper_me` builds on the last `upper_me`. Readiness comes from the new day.
- **Brief, debrief and dashboard:** these show "(moved from Mon)".

Check the file after adding an entry:

```
conda run -n pedestal python -m pipeline.plan.changes
```

The check validates the entries and refuses a new entry that touches a date before today.
Entries already committed are history and stay. It also warns, without blocking, when the
change breaks a design rule:
- strength and endurance under 6 h apart
- two upper max-effort sessions within 48 h
- bike VO2 and hand-bike VO2 within 48 h

An entry that can't apply, such as moving a session that isn't in that slot after the
entries before it, stops the daily run until it is fixed.

## Analyse a workout now

After a key session, don't wait for the morning:

```
conda run -n pedestal python -m pipeline.analyse --activity latest      # or an activity id
```

It ingests the last 3 days, then writes `derived/analysis/<date>-<activity_id>.md` and `.json`
and `latest.json`: the breakdown against the planned session, power-curve points, fitness
before and after, form tomorrow and going into the next key session, the week's share of
its `weekly_tss`, new bests, and anchor flags (e.g. a 20-min best x 0.95 more than 3% above
the FTP in `athlete.yaml`). Flags only: it never edits `athlete.yaml`, the plan, the
calendar or `today.json`. With `latest`, an activity already analysed gives
`no_new_activity`.

In the cloud, push a request file to the `requests` branch (format: `requests/README.md` on that
branch, or `docs/requests-branch-README.md`) or run Actions > analyse > Run workflow; the
result is committed to the `data`
branch under `analysis/`.

## Refresh the dashboard

The Pedestal dashboard (a Claude Dashboard artifact,
https://claude.ai/artifact/UWyBZJSZzpSpHvJoai4mPi) shows the data branch as 21 attached
datasets. Its header says when it was last refreshed. To refresh it, type **run** in the
Training chat. Any Claude session with this repo, the conda env `pedestal` and the Artifact and
ArtifactData tools can do it. The steps:

1. **Request, wait, export:**

   ```
   conda run -n pedestal python -m pipeline.dashboard.refresh
   ```

   It pushes `requests/<UTC timestamp>.json` (`{"activity": "latest"}`) to the `requests`
   branch, which runs the `analyse` workflow. It then polls origin/data every 20 s, for up to
   12 minutes, until `analysis/latest.json` has a `generated_at` at or after the request.
   Finally it runs `export_rows` from origin/data, plus the plan in `config/` for the week rows,
   into `derived/rows/`, one file per dataset. It prints one JSON line:
   `{requested_at, finished_at, result, activity_id, status_detail, rows, datasets}`. `result` is
   `ok`, `no_new_activity`, `error` or `timed_out`. The rows are exported in every case except a
   git failure, so the dashboard also picks up the morning run's data. It pushes only to
   `requests`: never to main, the plan, `athlete.yaml` or the calendar. Use `--no-request` to
   export without asking for an analysis, or `--activity <id>` for one workout.
   `python -m pipeline.dashboard.export_rows --ref origin/data` runs the export alone.
2. **Upload each file:** for every `derived/rows/<id>.json`, run the Artifact tool's publish with
   `url` set to the dashboard, `asset: true` and `file_path` set to that file. A JSON file goes
   up one per call. Keep each reply's `/_blob/<asset id>`.
3. **Point the datasets at the new files:** list the dashboard's `datasets` collection with
   ArtifactData, for each document's `version`. Then send one `batch` of 21 `update` writes, each
   pinned with `if_version`. Each write sets `source` to
   `{"kind": "file", "url": "/_blob/<asset id>", "name": "<id>.json"}` and `updated` to
   `{"at": "<ISO UTC now>", "by": "Pedestal refresh (Claude)"}`. The dataset ids are the file
   names: summary, today_sessions, latest_session, yesterday_items, fitness_days,
   fitness_planned, blocks, events, weekly_load, bench_points, bench_checkpoints, lifts,
   grinding, bike, readiness_days, compliance, body, and the four "This week" ones: week_outline,
   fitness_week, zones_week, heavy_reps_week (step 1's `datasets` lists them all). Don't change
   the page (`files/index.html`).
4. **Tidy up:** delete the assets the datasets used to point at, with the Artifact tool's
   delete (`url` and the old asset id as `path`). Delete only those.
5. **Report** in one line from step 1's JSON (times in Europe/Madrid):
   - `ok`: "Requested 18:32, done 18:39".
   - `no_new_activity`: "No new activity yet, try again in a few minutes".
   - `error`: the error.
   - `timed_out`: "the analysis didn't arrive within 12 minutes; the dashboard shows the last
     one".

The page reads only these datasets. Every figure on it comes from them, and `summary.exported_at`
is the "Last refreshed" time. The "This week" section uses four of them:

- `week_outline`: today-3 to today+3, from the plan, `wellness.csv`, `compliance.csv` and
  `activities.csv`.
- `fitness_week`: CTL, ATL and form over those days, actual to today and then projected.
- `zones_week`: from `zones.csv`. Ingest writes it from each activity's `icu_zone_times` and
  `icu_hr_zone_times`. Where streams exist, the seconds are recomputed with athlete.yaml's
  anchors: bike or hand-bike FTP for power, and LTHR with Friel's 7 zones for HR.
- `heavy_reps_week`: from `heavy_reps.csv`, the work, top and test sets at or above 90% of the
  lift's e1RM as it stood that day.

All of them are rebuilt by every refresh.

## Every morning, in the cloud

`.github/workflows/daily.yml` runs on GitHub Actions, so nothing depends on a PC being on.
It fires at 04:30, 04:45, 05:00, 05:30, 05:45 and 06:00 UTC, which covers a 06:30-07:00
morning window in Madrid in both summer and winter time. `python -m pipeline.run daily`
then decides for itself:

- it exits straight away outside the window set in `config/athlete.yaml`:
  `schedule: { timezone: Europe/Madrid, window: ["06:30", "07:00"] }` (end inclusive; change
  the timezone when you travel);
- it exits straight away once today's `derived/today.json` says `"status": "final"`;
- otherwise it runs. After ingest, today is **final** if its wellness row has sleep, or
  **provisional** ("no sleep yet") if not, with readiness from whatever HRV and resting HR
  have arrived. A provisional day runs again at the next slot, until it's final or the
  window closes.

The daily run:

1. **ingest** the last 14 days of intervals.icu activities and wellness, and the strength log
2. **readiness** for today (green / amber / red, with the reason)
3. **prescribe** strength for the next 7 days
4. **publish** the next 7 days to the intervals.icu calendar: only `pedestal:` events, and
   only those whose content changed (each carries a content hash); otherwise "no changes"
5. **review** yesterday into `compliance.csv` (on Monday that completes last week's drift record)
6. on Sundays, **weekly**: the drift review, into `derived/week.md` (the same as `pipeline.run weekly`)
7. **debrief** yesterday into `derived/debrief.md` and `debrief.json`: intervals against their
   bands, NP, IF, TSS and HR drift; strength sets against the prescription, e1RM change and
   next time's weight; grinding reps, fade and Grind Load; sailing and other unplanned
   sessions; the day's readiness, sleep and load; a one-line verdict and a note for today,
   from the rules in `pipeline/review/debrief.py` (`python -m pipeline.review.debrief --print`)
8. **brief**: `derived/today.md` to read and `derived/today.json` for anything else to use,
   with yesterday's debrief in it: the date, status (provisional or final) and when it was
   generated, readiness colour and reason, each session today (watt targets, or strength
   groups as kg × reps @ RPE cap with the reason), the flags from the latest Sunday review,
   and anything in the pipeline that failed
9. **dashboard**: `derived/dashboard.json` (chart-ready data for ten charts),
   `derived/dashboard.html` (one self-contained page: open it from the `data` branch, works on
   a phone, light and dark) and `derived/email/` (PNG charts, `dashboard_email.html` and
   `manifest.json` for an email). The load model, CTL 42 days, ATL 7 days, form = CTL - ATL
   from power TSS or hrTSS plus a Grind Load correction, is documented in
   `pipeline/dashboard/load.py` (`python -m pipeline.dashboard` builds it on its own)

**Read the brief** on the `data` branch: in the GitHub app open the repo, switch the
branch to `data`, and open `today.md` (and `week.md` on Sundays).

Each step logs a one-line status; a failed step doesn't stop the others (publish is skipped
if prescribe failed, so the calendar keeps its last good weights). **ntfy is only for
failures**: when a step fails and `NTFY_TOPIC` is set, one line names the failed steps, with
a link to the run, and nothing else: no training data, no error detail. Each failed step
alerts at most once a day (`today.json` keeps the list). The workflow run also goes red, and
GitHub emails you.

**State** lives on the `data` branch: the workflow checks it out into `derived/`, runs, and
commits it back as the Actions bot: the CSVs, `today.md`, `today.json`, `week.md` and full
step logs in `derived/logs/`. `main` is never written by the workflow, and `derived/` stays
git-ignored on `main`. The `.fit`/`.zwo`
files are kept as a run artifact for 7 days.

**Secrets to add** (repo Settings > Secrets and variables > Actions > New repository secret):

| Secret | What |
|---|---|
| `INTERVALS_ICU_API_KEY` | intervals.icu > Settings > Developer settings > API key |
| `INTERVALS_ICU_ATHLETE_ID` | your athlete id, e.g. `i12345` |
| `NTFY_TOPIC` | optional: a long, unguessable ntfy topic name for failure alerts; subscribe to it in the ntfy app. Without it, alerts are off |

Optional repo **variable** (same page, Variables tab): `NTFY_SERVER` for a self-hosted ntfy
server (default `https://ntfy.sh`).

**Run it now**: Actions > daily > Run workflow. A manual run ignores the window and goes
straight away (it still exits if today is already final).

**Pause it**: Actions > daily > ... > Disable workflow; enable it the same way.

**Log lifts**: add rows to `training/logs/strength.csv` on `main`, from the GitHub app
(open the file, edit, commit) or through the Training project. Format:
`training/logs/README.md`. The next run reads it.

Locally, the same run without touching the calendar or sending alerts, then today's brief,
and a test alert to check your ntfy subscription:

```
conda run -n pedestal python -m pipeline.run daily --ignore-window --dry-run --no-alert
conda run -n pedestal python -m pipeline.brief --print
conda run -n pedestal python -m pipeline.notify --test
```

## Secrets

Locally, secrets live in `.env` (see `.env.example`), which is git-ignored; in the cloud
they're GitHub Actions secrets. Code reads them from environment variables only, never
prints or logs them (the run scrubs their values from all output), and API clients take
keys as arguments so tests run with fakes and no network.
