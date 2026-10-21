# Plan file format — `config/plan.yaml`

The handshake between **design** (the Training project, where plans are thought through) and **execution** (the `grinder-data` pipeline, where they run and get measured). The authority is `pipeline/plan/schema.py`; this page explains it. A plan that doesn't fit the schema is refused before anything reaches the calendar, with the failing field named.

Check a plan: `python -m pipeline.plan.schema config/plan.yaml`

---

## Standing instruction for the Training project

> Every design session that produces or changes a training plan ends by emitting the **complete** `config/plan.yaml` in the format on this page, in one YAML code block, ready to commit. Not a summary, not a diff.
>
> - Keep `schema_version: 1`. Give the plan a new `plan_id` when it's a new macro plan; keep it when revising.
> - Strength is never fixed weights. Every loaded lift names a progression rule from `progressions`; give `start_kg` only where no history exists.
> - Use only modality names, slot names and target anchors listed here, and only lift ids listed in `config/athlete.yaml`. A new lift is added there first.
> - Blocks that aren't designed yet stay in the calendar with dates, intent and `weekly_tss`, marked `status: design_pending`.
> - Mark load targets `targets_provisional: true` until testing has confirmed the anchors behind them.
> - Every session should carry a `targets.tss` estimate (strength and grinding: ~45 TSS/h), so weekly targets compare like with like.
> - Never change FTP, LTHR or other anchors in the plan; they live in `config/athlete.yaml`.
> - Oisin reviews the file and commits it. Design produces it; the pipeline never edits it.

---

## Shape

```yaml
schema_version: 1
plan_id: winter-2026-27
created: 2026-10-13
designed_in: Training project
valid_from: 2026-10-14          # = first block start
valid_to: 2027-04-17            # = last block end
goals: [...]
strength_lines: [...]           # optional e1RM paths the weekly task checks against
progressions: {name: rule}
blocks: [...]                   # the calendar
week_templates: {name: {slot: session_id | [session_id, ...]}}
sessions: {session_id: session}
```

### Goals

```yaml
- id: bench-10x110
  priority: A                    # A | B | C
  date: 2027-04-17               # or omit and give a window, or leave both off if TBC
  description: 10 x 110 kg bench press, same day as the 312
  target: { lift: bench_press, kg: 110, reps: 10, e1rm_needed_kg: 147 }
```

The dashboard puts A goals first, with progress to each.

### Blocks and weeks

```yaml
- id: block1
  name: Accumulation
  start: 2026-10-26
  end: 2026-11-22
  intent: Free text the weekly task reads when judging drift.
  status: designed               # designed | design_pending
  weekly_tss: [650, 850]
  ctl_end: [75, 85]              # optional
  targets_provisional: true
  weeks:
    - start: 2026-11-09
      label: acc3
      template: accumulation
      weekly_tss: [700, 800]
      volume_pct: 1.0
      swap: { sat_am: long_ride_4h, fri_pm: bike_threshold_3x15 }
```

Blocks run in order without overlapping. A week runs from its `start` to the day before the next week (or the block end). `swap` replaces a template slot for that week only; `null` drops it. This is how sessions progress week to week without a new template each time.

**Slots:** `mon_am … sun_pm` (14). A slot holds one session id, or a list run in order (e.g. hand-bike intervals then grinding repeats).

### Sessions

Every session has `modality`, `summary` (the one line on the morning push), optional `environment` (`indoor | outdoor | either`), `targets`, and either `steps` or `exercises`.

**Modalities:** `bike`, `run`, `handbike`, `concept2`, `grinding` (need `steps`), `strength` (needs `exercises`), `mobility`, `rest`.

`readiness_exempt: true` stops an amber day quietly softening a test. Red still cuts it.

#### Endurance steps — bike, run, handbike, concept2, grinding

```yaml
steps:
  - { kind: warmup, duration_s: 900, target: { pct_ftp: [0.50, 0.75] } }
  - kind: repeat
    count: 2
    steps:
      - { kind: work, duration_s: 1200, target: { pct_ftp: [0.92, 0.95] } }
      - { kind: rest, duration_s: 300, target: { pct_ftp: [0.50, 0.60] } }
  - { kind: cooldown, duration_s: 600, target: { pct_ftp: [0.45, 0.55] } }
targets: { tss: 85, steps_held_pct: 0.95 }
```

Step kinds: `warmup, work, rest, cooldown, steady, test`, plus `repeat`. Each step's `target` has **exactly one** anchor:

| Anchor | Meaning | Use for |
|---|---|---|
| `pct_ftp: [lo, hi]` | fraction of that modality's FTP in `athlete.yaml` | bike, handbike, run (when pace anchor exists) |
| `watts: [lo, hi]` | absolute watts | concept2, grinding |
| `pct_curve: [ref, pct]` | fraction of a curve point, e.g. `[grinding_60s, 0.85]` | grinding |
| `hr_zone: n` | HR zone 1–5 | run until a pace anchor exists |
| `rpe: n` | effort only | tests, grinding easy, warm-ups |

`cadence: [lo, hi]` can sit alongside any anchor.

**Where it goes:** the pipeline writes the next 7 days to the intervals.icu calendar. intervals.icu uploads them to Garmin Connect (outdoor, on the watch) and to Zwift (indoor). The same steps are also written as fallback `.zwo` and `.fit` files to `exports/workouts/`. Grinding and anything with `rpe` or `pct_curve` steps go up with targets resolved to watts where possible, otherwise as notes.

#### Strength exercises

```yaml
exercises:
  - lift: bench_press              # id from athlete.yaml strength.lifts
    progression: bench_top_linear  # key in progressions; omit for by-feel or fixed
    start_kg: 100                  # only used when there is no history at all
    groups:
      - { role: top, sets: 1, reps: 6, rpe_cap: 8, rest_s: 240 }
      - { role: backoff, sets: 3, reps: 8, pct_of_top: 0.85, rest_s: 180 }
    note: Free text shown with the lift
```

Roles: `top, backoff, work, speed, amrap, test`. `reps` is a number, or `[lo, hi]` for double progression. A backoff's weight is `pct_of_top` × today's suggested top set.

---

## Progression rules

Named once in `progressions`, referenced per lift. A lift can change rule between blocks; that's a design decision, made in the Training project.

| Type | Fields | What it does |
|---|---|---|
| `linear` | `increment_kg`, `max_rpe`, `reset_after_misses`, `reset_pct` | Every rep at or below `max_rpe` → last weight + step. A miss → repeat. `reset_after_misses` misses in a row → drop `reset_pct`. |
| `double` | `rep_range: [lo, hi]`, `increment_kg`, `max_rpe` | Same weight, aim for one more rep than the weakest set. When every set reaches `hi` → + step, back to `lo`. |
| `wave` | `pcts: [...]`, `e1rm_step_kg` | Cycle through % of e1RM week by week; after each full cycle the e1RM steps up. |
| `rpe` | `target_rpe` | Weight that should land on the RPE: `e1RM / (1 + (reps + (10 − RPE)) / 30)`. |
| `pct_e1rm` | `pct` | Fixed share of the rolling e1RM. |

**Basis** (where the weight starts): the last time that lift was done in the same session slot, within 14 days. Failing that, the latest e1RM. Failing that, `start_kg`. Failing that, "choose a weight at the RPE cap and log it".

**Readiness:** green, the suggestion stands. Amber holds last weight, no increment, RPE cap 8. Red: 90% of last weight, RPE cap 7. Values in `athlete.yaml`.

**Rounding:** 2.5 kg (`athlete.yaml`).

Every suggestion is written to `derived/prescriptions.csv` with its basis set and the reason, e.g. *"hit 100x6 @RPE8: +2.5"*. The morning push shows it; the Today tab of the Daily Inputs sheet is pre-filled with it, and you log what you actually lifted beside it. That log is next week's basis.

---

## Planned against actual

Because sessions carry structured targets, the pipeline grades each one after it syncs back through intervals.icu, into `derived/compliance.csv`:

- **held**: step average inside the target band (±3%)
- **faded**: last work rep under 95% of the band's low end
- **missed / skipped**: not completed, or no matching activity
- **strength**: achieved kg, reps and RPE against the suggestion

The Sunday task reads this alongside load and recovery when it judges drift.
