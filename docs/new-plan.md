# New plan: cheat sheet

The full format is `docs/plan-format.md`; this is the one-page version. Start from
`plans/TEMPLATE.yaml` (it's a minimal valid plan), save as `plans/<plan_id>.yaml`, then:

```
conda run -n pedestal python -m pipeline.plan.adopt plans/<plan_id>.yaml        # check + preview
conda run -n pedestal python -m pipeline.plan.adopt plans/<plan_id>.yaml --yes  # adopt
```

Or from the phone: commit the file on a branch and open a pull request; the `plan-check`
workflow validates it and comments the diff. Adopting still needs the `--yes` run.

## The minimum valid plan

`schema_version: 1`, `plan_id`, `created`, `designed_in`, `valid_from` (= first block start),
`valid_to` (= last block end), at least one goal, `progressions` (may be `{}`), at least one
block, `week_templates`, and at least one session. A `designed` block needs `weeks`; a
`design_pending` one only dates, intent and `weekly_tss`.

## Names

| What | Values |
|---|---|
| Slots | `mon_am mon_pm tue_am tue_pm wed_am wed_pm thu_am thu_pm fri_am fri_pm sat_am sat_pm sun_am sun_pm` |
| Modalities | `bike run handbike concept2 grinding` (need `steps`), `strength` (needs `exercises`), `mobility`, `rest` |
| Step kinds | `warmup work rest cooldown steady test`, and `repeat` (with `count` and `steps`) |
| Group roles | `top backoff work speed amrap test` |
| Block status | `designed`, `design_pending` |
| Environment | `indoor outdoor either` |

## Target anchors (exactly one per step)

| Anchor | Example | Use for |
|---|---|---|
| `pct_ftp` | `{ pct_ftp: [0.92, 0.95] }` | bike, hand-bike (needs that modality's FTP in `athlete.yaml`) |
| `watts` | `{ watts: [380, 420] }` | concept2, grinding |
| `pct_curve` | `{ pct_curve: [grinding_60s, 0.85] }` | grinding (a curve point in `athlete.yaml`) |
| `hr_zone` | `{ hr_zone: 2 }` | run, until a pace anchor exists |
| `rpe` | `{ rpe: 4 }` | tests, warm-ups, easy grinding |

`cadence: [lo, hi]` can sit alongside any anchor.

## Progression types

| Type | Fields | Does |
|---|---|---|
| `linear` | `increment_kg max_rpe reset_after_misses reset_pct` | every rep at or below max_rpe: + step; a miss: repeat; N misses: drop reset_pct |
| `double` | `rep_range: [lo, hi] increment_kg max_rpe` | same weight, one more rep; all sets at `hi`: + step, back to `lo` |
| `wave` | `pcts: [...] e1rm_step_kg` | % of e1RM week by week; + step per full cycle |
| `rpe` | `target_rpe` | the weight that lands on that RPE from e1RM |
| `pct_e1rm` | `pct` | a fixed share of e1RM |

Weight basis: same lift and slot within 14 days, then `start_kg`, then e1RM, then "choose".

## Common mistakes

- `valid_from` / `valid_to` not matching the first block's start / last block's end.
- A designed block whose first week doesn't start on the block start.
- A lift id that isn't in `config/athlete.yaml` (add it there first).
- `reps: [8, 12]` without a `double` progression, or a `double` whose range doesn't match the group's.
- A `backoff` group without `pct_of_top`, or without a `top` group in the same exercise.
- Two anchors in one target (`pct_ftp` and `watts`), or none.
- `pct_ftp` on a modality with no FTP in `athlete.yaml` (e.g. `run`, `grinding`).
- Changing anything before today: past days, ended blocks, started weeks, past checkpoints.
  `adopt` refuses; plan the change from today on instead.
- Editing a session that already ran (e.g. new watts for `z2_60`): that rewrites the past, so
  `adopt` refuses. Add the new version under a new id (`z2_60_v2`) and swap it in from today.
- Putting FTP, LTHR or other anchors in the plan; they belong in `athlete.yaml`.
- Duplicate keys (two sessions with the same id): the loader refuses them.
