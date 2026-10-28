"""Small HTTP layer shared by every data source: retries, backoff and a TTL cache."""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, status: int | None, url: str, body: str = ""):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")


class Http:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 1.0,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session or requests.Session()
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self._cache: dict[tuple, tuple[float, Any]] = {}

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        headers: dict | None = None,
    ) -> Any:
        resp = self._send(method, url, params, json_body, headers)
        if resp.status_code == 204 or not (resp.text or "").strip():
            return {}
        return resp.json()

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self._send("GET", url, params, None, {"Accept": "text/plain, */*"}).text or ""

    def _send(self, method: str, url: str, params: dict | None, json_body: Any, headers: dict | None):
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json"}
        hdrs.update(headers or {})
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, headers=hdrs, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retries:
                    self._sleep(self.backoff * 2**attempt)
                    continue
                raise HttpError(None, url, str(exc)) from exc

            if resp.status_code in RETRY_STATUS and attempt < self.retries:
                retry_after = resp.headers.get("Retry-After") if resp.headers else None
                try:
                    delay = float(retry_after) if retry_after else self.backoff * 2**attempt
                except ValueError:
                    delay = self.backoff * 2**attempt
                self._sleep(min(delay, 30.0))
                continue
            if resp.status_code >= 400:
                raise HttpError(resp.status_code, url, resp.text or "")
            return resp
        raise HttpError(None, url, str(last_error))

    def get_json(
        self, url: str, params: dict | None = None, headers: dict | None = None, ttl: float | None = None
    ) -> Any:
        key = (url, tuple(sorted((params or {}).items())))
        now = time.monotonic()
        if ttl:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        data = self.request("GET", url, params=params, headers=headers)
        if ttl:
            self._cache[key] = (now + ttl, data)
        return data
