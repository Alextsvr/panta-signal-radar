"""Minimal read-only Panta API client.

Confirmed facts (see docs/api_notes.md):
  * base URL  https://live-api.panta.market/api/v1  (trailing slashes REQUIRED)
  * auth      X-Api-Key: pk_test_... | pk_live_...   (no key -> 401 UNAUTHORIZED)
  * read rate limit ~120 requests / 60 s per account (X-RateLimit-* headers, Retry-After on 429)
Only GET endpoints are implemented on purpose.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

import requests

from .config import Settings, load_settings

RETRY_STATUSES = {429, 500, 502, 503, 504}


class PantaAPIError(RuntimeError):
    """Error that is safe to print: contains no headers and no API key."""

    def __init__(self, endpoint: str, status: int | None, code: str, message: str):
        self.endpoint, self.status, self.code, self.message = endpoint, status, code, message
        super().__init__(f"{endpoint} -> HTTP {status if status is not None else '-'} [{code}] {message}")


class PantaClient:
    def __init__(self, settings: Settings | None = None, min_interval_s: float = 0.55,
                 max_retries: int = 3, session: requests.Session | None = None):
        self.settings = settings or load_settings()
        if getattr(self.settings, "public_mode", False):
            raise PantaAPIError("config", None, "PUBLIC_MODE",
                                "direct Panta API access is disabled in public read-only mode")
        if not self.settings.api_key:
            raise PantaAPIError("config", None, "NO_API_KEY", "PANTA_API_KEY is not set in .env")
        self.min_interval_s = min_interval_s  # 0.55 s keeps us under 120 req/min
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json",
                                     "X-Api-Key": self.settings.api_key,
                                     "User-Agent": "panta-signal-radar/0.1"})
        self._last_call = 0.0
        self.calls = 0
        self.last_rate_limit: dict[str, str] = {}

    # ------------------------------------------------------------------ core
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        path = "/" + path.strip("/") + "/"  # Panta requires trailing slashes
        url = self.settings.base_url + path
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        attempt = 0
        while True:
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                resp = self.session.get(url, params=params, timeout=self.settings.timeout_s)
            except requests.Timeout:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(2 ** attempt)
                    continue
                raise PantaAPIError(path, None, "TIMEOUT", f"no response within {self.settings.timeout_s}s") from None
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(2 ** attempt)
                    continue
                raise PantaAPIError(path, None, "NETWORK", type(exc).__name__) from None

            self.last_rate_limit = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                attempt += 1
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                time.sleep(min(delay, 60))
                continue
            return self._parse(path, resp)

    @staticmethod
    def _parse(path: str, resp: requests.Response) -> Any:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.ok:
            if body is None:
                raise PantaAPIError(path, resp.status_code, "MALFORMED", "response is not JSON")
            return body
        code, msg = f"HTTP_{resp.status_code}", (resp.text or "")[:200]
        if isinstance(body, dict):
            code = str(body.get("code") or code)
            msg = str(body.get("message") or body.get("detail") or msg)[:200]
        if resp.status_code in (401, 403):
            msg += " (check PANTA_API_KEY in .env)"
        raise PantaAPIError(path, resp.status_code, code, msg)

    # ------------------------------------------------------------ endpoints
    def get_categories(self) -> list[str]:
        body = self._get("categories")
        return list(body.get("categories") or []) if isinstance(body, dict) else []

    def get_markets_page(self, limit: int = 50, cursor: str | None = None,
                         category: str | None = None, phase: str | None = None) -> dict:
        body = self._get("markets", {"limit": min(int(limit), 50), "cursor": cursor,
                                     "category": category, "phase": phase})
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise PantaAPIError("/markets/", 200, "MALFORMED", "expected {'items': [...]}")
        return body

    def iter_markets(self, max_pages: int = 20, **filters: Any) -> Iterator[dict]:
        cursor, seen_cursors = None, set()
        for _ in range(max_pages):
            page = self.get_markets_page(cursor=cursor, **filters)
            yield from page["items"]
            cursor = page.get("nextCursor")
            if not cursor or cursor in seen_cursors:
                return
            seen_cursors.add(cursor)

    def get_markets(self, max_pages: int = 20, **filters: Any) -> list[dict]:
        return list(self.iter_markets(max_pages=max_pages, **filters))

    def get_market(self, market_id: str) -> dict:
        return self._get(f"markets/{requests.utils.quote(market_id, safe='')}")

    def get_market_trades(self, market_id: str, limit: int = 200) -> list[dict]:
        body = self._get(f"markets/{requests.utils.quote(market_id, safe='')}/trades",
                         {"limit": min(int(limit), 200)})
        return list(body.get("items") or []) if isinstance(body, dict) else []
