"""intervals.icu API client: activities and wellness for a date range.

The key and athlete id are arguments, so tests pass fakes and a mock transport.
Auth is HTTP Basic with the literal username `API_KEY`; the key never appears in
a URL, a log line, an error message, the cache or `repr()`.

Requests that hit a rate limit (429), a server error (5xx) or a network failure are
retried with exponential backoff, honouring `Retry-After`. Every successful response
is cached raw under `cache_dir`, and `offline=True` reads the cache instead of the API.
"""

import datetime as dt
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://intervals.icu/api/v1"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_S = 60


class IntervalsError(RuntimeError):
    pass


class IntervalsClient:
    def __init__(
        self,
        api_key: str,
        athlete_id: str,
        *,
        cache_dir: Path | None = None,
        offline: bool = False,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        timeout_s: float = 30.0,
    ):
        self.athlete_id = athlete_id
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.offline = offline
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._http = httpx.Client(
            base_url=BASE_URL,
            auth=httpx.BasicAuth("API_KEY", api_key),
            headers={"User-Agent": "pedestal/0.1 (grinder-data)", "Accept": "application/json"},
            timeout=timeout_s,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"IntervalsClient(athlete_id={self.athlete_id!r}, offline={self.offline})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ endpoints

    def activities(self, oldest: dt.date, newest: dt.date) -> list[dict[str, Any]]:
        return self._get("activities", f"/athlete/{self.athlete_id}/activities", oldest, newest)

    def wellness(self, oldest: dt.date, newest: dt.date) -> list[dict[str, Any]]:
        return self._get("wellness", f"/athlete/{self.athlete_id}/wellness", oldest, newest)

    def activity_intervals(self, activity_id: str) -> dict[str, Any]:
        """{"icu_intervals": [{type: WORK|RECOVERY, average_watts, moving_time, ...}], ...}; cached per activity."""
        path = f"/activity/{activity_id}/intervals"
        if self.cache_dir is not None:
            cached = self.cache_dir / "intervals_icu" / "intervals" / f"{activity_id}.json"
            if self.offline:
                if not cached.exists():
                    raise IntervalsError(f"no cached intervals for activity {activity_id}")
                return json.loads(cached.read_text(encoding="utf-8"))["data"]
            data = self._request("GET", path)
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps({"path": path, "data": data}, indent=1), encoding="utf-8")
            return data
        return self._request("GET", path)

    # ------------------------------------------------------------------ calendar writes (phase 5)

    def events(self, oldest: dt.date, newest: dt.date) -> list[dict[str, Any]]:
        params = {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        return self._request("GET", f"/athlete/{self.athlete_id}/events", params)

    def create_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._request("POST", f"/athlete/{self.athlete_id}/events/bulk", json_body=events)

    def update_event(self, event_id: int, event: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/athlete/{self.athlete_id}/events/{event_id}", json_body=event)

    def delete_event(self, event_id: int) -> None:
        self._request("DELETE", f"/athlete/{self.athlete_id}/events/{event_id}", expect_json=False)

    # ------------------------------------------------------------------ plumbing

    def cache_path(self, name: str, oldest: dt.date, newest: dt.date) -> Path:
        assert self.cache_dir is not None
        return self.cache_dir / "intervals_icu" / f"{name}_{oldest}_{newest}.json"

    def _get(self, name: str, path: str, oldest: dt.date, newest: dt.date) -> Any:
        if self.offline:
            if self.cache_dir is None:
                raise IntervalsError("offline mode needs a cache directory")
            cached = self.cache_path(name, oldest, newest)
            if not cached.exists():
                raise IntervalsError(f"no cached {name} for {oldest} to {newest} ({cached})")
            return json.loads(cached.read_text(encoding="utf-8"))["data"]

        params = {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        data = self._request("GET", path, params)
        if self.cache_dir is not None:
            out = self.cache_path(name, oldest, newest)
            out.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "path": path,
                "params": params,
                "data": data,
            }
            tmp = out.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, indent=1), encoding="utf-8")
            tmp.replace(out)
        return data

    def _request(self, method: str, path: str, params=None, json_body=None, expect_json: bool = True) -> Any:
        # Writes are retried too: creates only happen for events not already on the calendar,
        # and updates and deletes are idempotent.
        for attempt in range(1, self._max_attempts + 1):
            last = attempt == self._max_attempts
            try:
                resp = self._http.request(method, path, params=params, json=json_body)
            except httpx.TransportError as e:
                if last:
                    raise IntervalsError(f"intervals.icu unreachable for {path}: {type(e).__name__}") from None
                self._sleep(2 ** (attempt - 1))
                continue

            if resp.status_code in RETRY_STATUS and not last:
                self._sleep(self._retry_delay(resp, attempt))
                continue
            if resp.status_code in (401, 403):
                raise IntervalsError(
                    f"intervals.icu refused {path} ({resp.status_code}). "
                    "Check INTERVALS_ICU_API_KEY and INTERVALS_ICU_ATHLETE_ID in .env."
                )
            if resp.status_code >= 400:
                raise IntervalsError(f"intervals.icu returned {resp.status_code} for {method} {path}")
            if not expect_json or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                raise IntervalsError(f"intervals.icu sent a non-JSON response for {path}") from None
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        header = resp.headers.get("Retry-After", "")
        if header.isdigit():
            return min(float(header), MAX_RETRY_AFTER_S)
        return float(2 ** (attempt - 1))
