# Training logs

Interim system of record for strength sets until Pedestal phase 3 ingests the Daily Inputs sheet.
Phase 3 reads `strength.csv` as history, so nothing logged here is lost.

`strength.csv` — one row per working set (or per top set / test set):
- `date` (YYYY-MM-DD), `session` (session id from `config/plan.yaml`), `lift` (lift id), `role` (top, backoff, work, speed, amrap, test)
- `kg`, `reps`, `rpe` (blank if not reported), `notes`
