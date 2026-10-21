# Plan design rules

How the winter plan is built and adjusted. These are the rules a design session (or the
weekly review) applies; `config/plan.yaml` is their output. Updated 21 Oct 2026.

## Goal hierarchy

1. **A goals win every clash**: 10 × 110 kg bench and the solo 312, both on 17 Apr 2027.
2. **C events are trained through** (ATHX, HYROX): 2–3 easier days before, no heavy lower
   body or long ride in the 48 h before, raced hard but not all-out, back training within 3 days.
3. **Cut rule:** a bench checkpoint missed by more than one rep, or two weekly check-ins in a
   row with falling power and rising fatigue → the next C event is cut first.
4. Festive 500 and the bike camp are training blocks, not events.

## Block structure

- 4-week blocks: weeks 1–3 load, week 4 deloads and tests.
- Each block rotates exercise variations within each quality (~4-week rotation).
- Phases: base (Oct–Dec) → build (Jan–Feb) → durability (Feb–Mar) → specific (Mar–Apr) → taper.
- FTP is built in winter; durability in spring.

## Weekly structure

- 5 gym sessions + 1 full rest day. Fewer than 4 doesn't fit the goals.
- Bench 3×/week: max effort (Mon), repetition (Thu), dynamic/speed (Fri).
- Lower body 2×/week.
- Strength and endurance separated by 6+ hours; quality work in the freshest slot.
- Heavy bench day at least 48 h from any ride over 5 h.
- From March, bench drops to 2×/week (heavy + reps) to protect long-ride recovery;
  full sharpness in the final two weeks.

## Grinding

- Every upper session opens with the explosive bench pull (bench-pull power predicts
  grinding output better than bench-press strength). Non-negotiable.
- Hand-bike sprints on days the upper body is fresh (after a lower-body morning).
- Hand-bike 4-min aerobic work can go on fatigue-tolerant days.
- Bike VO2 and hand-bike VO2 at least 48 h apart, never the same day.
- If 4-min hand-bike power falls two weeks running → cut the bench variation day first.
  Grinding output is the job; the bench target is the challenge.
- Track Grind Load (effort count, work minutes, efforts ≥85% LTHR) alongside hrTSS;
  hrTSS under-reads grinding.
- Machine ceiling: nothing under 60 s is measurable; test 1 min and 4 min only.

## Strength progression

- Never fixed weights. Every loaded lift has a named progression rule.
- Autoregulate: stop the set when bar speed clearly slows. No training to failure.
- Estimated max from reps: `e1RM = kg × (1 + reps/30)`, counting clean reps only.
- Basis for next week = what was actually achieved, logged in Daily Inputs.
- Re-entry and racing weeks: RPE 7 cap, 60 min sessions, nothing that leaves grinding sore.

## Bike progression

- Long ride grows 3 h → 7–8 h by the late-March dress rehearsal (~220 km of the loop, solo,
  carrying everything).
- Carbs on long rides: 60 → 90 g/h from January, 110 g/h by March.
- Race pacing: IF 0.65–0.70, no surges above ~330 W.

## Fuelling and body composition

- Fat loss ≤0.5 kg/week, deficit only on light days.
- No deficit during the Festive 500, the camp, or race weeks.
- If bike or grind power drops two weeks running, eat more.
- If bench stalls, hold bodyweight for a few weeks rather than pushing the cut.

## Review loop

- Sunday check-in: bench top sets and reps, pulling numbers, hand-bike outputs and Grind Load,
  7-day average bodyweight, niggles, sleep. Rides from Strava.
- Each week's loads and intensities are adjusted from achieved data against the checkpoints
  in `training/goals.md` and `strength_lines` in `config/plan.yaml`.
- Design-pending blocks are designed before they start (next: ATHX week and Recover, before 23 Nov).
