"""Should this hour's scheduled run go? Standard library only, so the workflow can ask before installing.

The workflow fires every hour. The morning run happens once a day (a run with failures retries
the next hour), inside a local-time window
(`PEDESTAL_WINDOW`, default "05-11": from 05:00 until 11:00 in `TZ`):

- before the window, or once today is done (a run with no failures, or the final one): no
- inside the window: yes, and the run waits for today's HRV and sleep (`final=false`)
- in the window's last hour, or up to two hours after it (in case GitHub skipped a slot):
  yes, as the fallback that runs whether or not the data has arrived (`final=true`)
- a manual run (`--forced`): yes, final

State is derived/state/morning.json on the data branch: {"last_done": "2026-10-23", ...}.

    python -m pipeline.gate --state derived/state/morning.json [--forced]

Prints the decision; under GitHub Actions it also writes `run=` and `final=` to $GITHUB_OUTPUT.
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

DEFAULT_WINDOW = "05-11"
CATCH_UP_HOURS = 2


def parse_window(text: str) -> tuple[int, int]:
    start, end = (int(x) for x in text.split("-"))
    if not 0 <= start < end <= 24:
        raise ValueError(f"PEDESTAL_WINDOW must look like 05-11 (start before end, 0-24); got {text!r}")
    return start, end


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def decide(now: dt.datetime, window: tuple[int, int], state: dict, forced: bool = False) -> tuple[bool, bool, str]:
    """(run, final, why)."""
    today = now.date().isoformat()
    start, end = window
    if forced:
        return True, True, "manual run"
    if state.get("last_done") == today:
        return False, False, f"already ran today ({state.get('done_at', '?')})"
    hour = now.hour
    if hour < start:
        return False, False, f"before the morning window ({now:%H:%M}, window {start:02d}-{end:02d})"
    if hour >= end + CATCH_UP_HOURS:
        return False, False, f"past the morning window and its catch-up ({now:%H:%M}); no run today"
    final = hour >= end - 1
    return True, final, ("last chance in the window: run with or without sleep data" if final
                         else "in the morning window: run once today's HRV and sleep are in")  # fmt: skip


def main(argv=None, now: dt.datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.gate")
    parser.add_argument("--state", type=Path, default=Path("derived/state/morning.json"))
    parser.add_argument("--window", default=os.environ.get("PEDESTAL_WINDOW") or DEFAULT_WINDOW)
    parser.add_argument("--forced", action="store_true", help="a manual run: go now, as the final run")
    args = parser.parse_args(argv)
    now = now or dt.datetime.now()  # local time: the workflow sets TZ
    run, final, why = decide(now, parse_window(args.window), read_state(args.state), args.forced)
    print(f"{now:%Y-%m-%d %H:%M} {os.environ.get('TZ', 'local')}: {'run' if run else 'skip'}" + (" (final)" if run and final else "") + f" - {why}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"run={str(run).lower()}\nfinal={str(final).lower()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
