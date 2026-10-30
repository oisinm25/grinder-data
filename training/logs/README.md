# Training logs

Interim system of record for strength sets until Pedestal phase 3 ingests the Daily Inputs sheet.
Phase 3 reads `strength.csv` as history, so nothing logged here is lost.

`strength.csv` — one row per working set (or per top set / test set):
- `date` (YYYY-MM-DD), `session` (session id from `config/plan.yaml`), `lift` (lift id), `role` (top, backoff, work, speed, amrap, test)
- `kg`, `reps`, `rpe` (blank if not reported), `notes`

`body.csv`: one row per bodyweight, written by the Pedestal run from what Pedestal Coach saved
(or by hand). Columns `date` (YYYY-MM-DD), `kg`, `source` (coach or hand) and `notes`. Ingest
uses it in place of intervals.icu's weight that day; the last row for a date wins.

`changes.yaml`: session moves, swaps, drops and done-late sessions on top of the plan (README
"Moving a session"). The Pedestal run appends what the coach saved.
