# requests

The `requests` branch carries analysis requests. Pushing a request file to it runs the
`analyse` workflow, which analyses one workout and commits the result to the `data` branch
under `analysis/`. The daily run never reads this branch.

The branch is cut from `main`. GitHub runs a push-triggered workflow from the pushed branch's
own copy of `.github/workflows/analyse.yml`, so the branch has to carry it. When `analyse.yml`
changes on `main`, merge `main` into `requests`. The analysis itself always runs `main`'s code.

## Request file

One JSON file per request, in `requests/`, named by its timestamp so files never clash:

```
requests/2026-10-25T13-40-00Z.json
```

```json
{ "activity": "latest", "requested_at": "2026-10-25T13:40:00Z" }
```

- `activity`: `"latest"` (the newest activity on intervals.icu) or an intervals.icu activity
  id such as `"i201411654"`. Anything else is refused.
- `requested_at`: when it was asked for (informational).

With `"latest"`, if that activity was already analysed the result is `no_new_activity`.
A push without a new `requests/*.json` file (such as an edit to this README) does nothing.

## Result

On the `data` branch:

- `analysis/latest.json`: `status` (`ok`, `no_new_activity` or `error`), `generated_at`,
  `activity_id`, the breakdown, fitness before and after, projections, new bests and anchor
  flags.
- `analysis/<date>-<activity_id>.md` and `.json`: the full analysis.

It never changes the calendar, `today.json`, `config/athlete.yaml` or the plan: anchor flags
are suggestions for you to act on.

You can also run it by hand: Actions > analyse > Run workflow, with an activity id or `latest`.
