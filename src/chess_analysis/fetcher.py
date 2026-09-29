"""Serial chess.com PubAPI client with ETag caching, backoff, and archive walking."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.chess.com/pub"
MAX_ATTEMPTS = 6
INITIAL_BACKOFF_S = 2.0


class PubApiClient:
    """Strictly serial HTTP client: one request in flight at a time, ever."""

    def __init__(self, user_agent: str, timeout_s: float = 30.0):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._timeout = timeout_s
        self.request_count = 0

    def _get(self, url: str, etag: str | None = None, last_modified: str | None = None) -> requests.Response:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        backoff = INITIAL_BACKOFF_S
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.request_count += 1
            try:
                resp = self._session.get(url, headers=headers, timeout=self._timeout)
            except requests.RequestException:
                if attempt == MAX_ATTEMPTS:
                    raise
                log.warning("network error on %s, retrying in %.1fs", url, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == MAX_ATTEMPTS:
                    resp.raise_for_status()
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                log.warning("%d on %s, backing off %.1fs", resp.status_code, url, wait)
                time.sleep(wait)
                backoff *= 2
                continue
            return resp
        raise RuntimeError("unreachable")

    def get_json(self, path_or_url: str) -> dict[str, Any] | None:
        url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
        resp = self._get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_month(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> tuple[str, list[dict[str, Any]] | None, str | None, str | None]:
        """Fetch one monthly archive. Returns (status, games, etag, last_modified).

        status: 'ok' with games, 'not_modified' (games None), or 'missing' (404 archive).
        """
        resp = self._get(url, etag=etag, last_modified=last_modified)
        if resp.status_code == 304:
            return "not_modified", None, etag, last_modified
        if resp.status_code == 404:
            return "missing", None, None, None
        resp.raise_for_status()
        new_etag = resp.headers.get("ETag")
        new_lm = resp.headers.get("Last-Modified")
        return "ok", resp.json().get("games", []), new_etag, new_lm


def list_archives(client: PubApiClient, username: str) -> list[str]:
    data = client.get_json(f"/player/{username}/games/archives")
    if data is None:
        raise ValueError(
            f"chess.com returned 404 for player {username!r} — check the configured username"
        )
    return list(data.get("archives", []))
