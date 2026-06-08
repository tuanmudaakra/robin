"""
base.py — BaseAPIClient: HTTP via urllib + rate limit + retry/backoff.

Tanpa dependency `requests`. Aman untuk dipakai semua client (Helius/Dex/Birdeye).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from config.settings import log


class RateLimiter:
    """Token-bucket sederhana: maksimal `rate` request per detik."""

    def __init__(self, rate_per_sec: float):
        self.min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


class BaseAPIClient:
    def __init__(self, base_url: str, rate_per_sec: float = 5.0,
                 default_headers: Optional[dict] = None, timeout: int = 15,
                 max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": "robin/2.1", "Accept": "application/json"}
        if default_headers:
            self.headers.update(default_headers)
        self.timeout = timeout
        self.max_retries = max_retries
        self.limiter = RateLimiter(rate_per_sec)
        self.name = self.__class__.__name__

    def _url(self, path: str, params: Optional[dict] = None) -> str:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
        return url

    def request(self, method: str, path: str, params: Optional[dict] = None,
                body: Optional[dict] = None) -> Optional[Any]:
        url = self._url(path, params)
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"

        backoff = 1.0
        for attempt in range(1, self.max_retries + 1):
            self.limiter.wait()
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    log.warning("%s %s -> HTTP %s (retry %d/%d)", self.name, url, e.code,
                                attempt, self.max_retries)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                log.warning("%s %s -> HTTP %s (no retry)", self.name, url, e.code)
                return None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                log.warning("%s %s -> %s (retry %d/%d)", self.name, url, e, attempt,
                            self.max_retries)
                time.sleep(backoff)
                backoff *= 2
        log.error("%s gagal setelah %d percobaan: %s", self.name, self.max_retries, url)
        return None

    def get(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Optional[dict] = None,
             params: Optional[dict] = None) -> Optional[Any]:
        return self.request("POST", path, params=params, body=body)
