# Pedestal (grinder-data)

Training plan, goals and the measurement pipeline for Oisin McClelland's grinder training.

- `training/goals.md` — winter 2026–27 A and C goals, checkpoints, review cadence
- `training/blocks.md` — block calendar and bike/grinding progression
- `training/design-rules.md` — the rules the plan is built and adjusted from
- `config/plan.yaml` — machine-readable plan (format: `docs/plan-format.md`)
- `config/athlete.yaml` — anchors the plan points at: FTPs, LTHR, grinding curve, strength rounding, readiness rules, lift ids

Design happens in the Claude Training project; Oisin reviews and commits.

Version: v0.1.0. v1.0 = phases 1–5 running end to end.

| Phase | What | State |
|---|---|---|
| 1 | Plan and athlete schemas, validator CLI | done |
| 2 | Plan expanded into dated daily sessions, targets resolved to watts | done |
| 3 | Ingest: intervals.icu activities and wellness, strength log, into `derived/` | done |
| 4 | to be specified | — |
| 5 | to be specified | — |

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
weights come with phase 3.

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

Run the tests:

```
conda run -n pedestal python -m pytest
```

## Secrets

Secrets live in a local `.env` (see `.env.example`); it is git-ignored. Code reads them
from environment variables only, and API clients take keys as arguments so tests run
with fakes and no network.
