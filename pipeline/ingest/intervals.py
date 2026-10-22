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
        data = self._request(path, params)
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

    def _request(self, path: str, params: dict[str, str]) -> Any:
        for attempt in range(1, self._max_attempts + 1):
            last = attempt == self._max_attempts
            try:
                resp = self._http.get(path, params=params)
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
                raise IntervalsError(f"intervals.icu returned {resp.status_code} for {path}")
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
