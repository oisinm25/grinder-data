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
7. **brief**: `derived/today.md` to read and `derived/today.json` for anything else to use:
   the date, status (provisional or final) and when it was generated, readiness colour and reason, each session today (watt targets, or strength
   groups as kg × reps @ RPE cap with the reason), yesterday's grades, the flags from the
   latest Sunday review, and anything in the pipeline that failed

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
