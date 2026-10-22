"""Failure alerts through ntfy: one line naming the failed steps, and nothing else.

ntfy carries no training data, no readiness, no weights, no error detail: only the date,
the names of the steps that failed, and (under GitHub Actions) a link to the run. The
morning brief itself is derived/today.md on the data branch. Alerts go to the topic in
`NTFY_TOPIC`; without it they're off. Anyone who knows a public ntfy.sh topic can read
it, so the topic is treated as a secret: never printed, logged or put in an error message.
`NTFY_SERVER` (optional, not secret) points at a self-hosted server.

    python -m pipeline.notify --test     # send a test alert to check the subscription
"""

import argparse
import datetime as dt
import os
import sys
import time

import httpx

from pipeline.settings import load_env

DEFAULT_SERVER = "https://ntfy.sh"


class NotifyError(RuntimeError):
    pass


def run_url(env) -> str:
    """Link to this GitHub Actions run, if there is one (not secret: the repo is yours)."""
    server, repo, run_id = env.get("GITHUB_SERVER_URL"), env.get("GITHUB_REPOSITORY"), env.get("GITHUB_RUN_ID")
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else ""


def alert_text(day: dt.date, steps: list[str], url: str = "") -> str:
    """'Pedestal Mon 26 Oct: publish, review failed. <run link>' - step names only, one line."""
    names = ", ".join(steps)
    return f"Pedestal {day:%a} {day.day} {day:%b}: {names} failed." + (f" {url}" if url else "")


def send(topic: str, message: str, *, server: str = DEFAULT_SERVER, transport: httpx.BaseTransport | None = None,
         sleep=time.sleep) -> None:  # fmt: skip
    """POST one line to ntfy. Errors never mention the topic."""
    if "\n" in message:
        raise ValueError("alerts are one line")
    headers = {"Title": "Pedestal pipeline", "Tags": "warning", "Priority": "4"}
    if "https://" in message:
        headers["Click"] = message.rsplit(" ", 1)[-1]
    with httpx.Client(base_url=server, transport=transport, timeout=20) as http:
        for attempt in range(1, 4):
            try:
                resp = http.post(f"/{topic}", content=message.encode("utf-8"), headers=headers)
            except httpx.TransportError as e:
                if attempt == 3:
                    raise NotifyError(f"ntfy unreachable ({type(e).__name__})") from None
                sleep(2 ** (attempt - 1))
                continue
            if resp.status_code < 300:
                return
            if resp.status_code < 500 or attempt == 3:
                raise NotifyError(f"ntfy refused the alert ({resp.status_code})")
            sleep(2 ** (attempt - 1))


def alert(day: dt.date, steps: list[str], *, env=None, transport=None, sleep=time.sleep) -> str | None:
    """Send the failure alert. Returns the text sent, or None when NTFY_TOPIC isn't set (alerts off)."""
    if env is None:
        load_env()
        env = os.environ
    topic = (env.get("NTFY_TOPIC") or "").strip()
    if not topic:
        return None
    text = alert_text(day, steps, run_url(env))
    send(topic, text, server=env.get("NTFY_SERVER") or DEFAULT_SERVER, transport=transport, sleep=sleep)
    return text


def main(argv=None, *, env=None, transport=None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.notify", description="Send a test failure alert.")
    parser.add_argument("--test", action="store_true", required=True, help="send a test alert")
    parser.parse_args(argv)
    try:
        sent = alert(dt.date.today(), ["test (nothing is wrong)"], env=env, transport=transport, sleep=sleep)
    except NotifyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if sent is None:
        print("error: NTFY_TOPIC is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 2
    print("sent a test alert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
