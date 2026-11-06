"""Fetch pipeline: walk archives, upsert games, parse them, persist sync state."""

from __future__ import annotations

import logging
import sqlite3

from .config import AppConfig
from .fetcher import PubApiClient, list_archives
from .ingestion import load_sync_state, store_sync_state, sync_parsed, upsert_games
from .pubapi_stats import FetchStats

log = logging.getLogger(__name__)


def _archive_month(url: str) -> str:
    """'.../player/u/games/2024/05' -> '2024-05' (archives are always YYYY/MM)."""
    parts = url.rstrip("/").rsplit("/", 2)
    if len(parts) != 3 or not (parts[-2].isdigit() and parts[-1].isdigit()):
        return ""  # not a monthly archive URL — excluded from since-window checks
    return f"{parts[-2]}-{parts[-1]}"


def select_archives(archives: list[str], *, months: int, since: str) -> list[str]:
    """Limit the chronological archive list to the fetch window.

    `months` keeps the newest N archives; `since` (YYYY-MM) drops everything
    older. Chess.com serves whole months, so every bound is month-granular.
    """
    if since:
        # YYYY-MM-DD bounds collapse to their month (archives are month-granular)
        archives = [u for u in archives if _archive_month(u) >= since[:7]]
    if months > 0:
        archives = archives[-months:]
    return archives


def run_fetch(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    months: int | None = None,
    since: str | None = None,
    max_games: int | None = None,
) -> FetchStats:
    """Incremental, serial, idempotent fetch of the configured user's games.

    The window defaults to the config's `fetch` settings (0 / "" = no limit);
    explicit arguments override them. `max_games` stops the walk once enough
    new games were stored (month-granular: a month is fetched in full).
    """
    fetch = config.fetch
    months = fetch.months if months is None else months
    since = fetch.since if since is None else since
    max_games = fetch.max_games if max_games is None else max_games
    if months < 0 or max_games < 0:
        raise ValueError("--months and --max-games must be >= 0")
    stats = FetchStats()
    client = PubApiClient(config.user_agent)
    archives = list_archives(client, config.username)
    archives = select_archives(archives, months=months, since=since)
    stats.months_seen = len(archives)
    state = load_sync_state(conn)
    window = []
    if months:
        window.append(f"months={months}")
    if since:
        window.append(f"since={since}")
    if max_games:
        window.append(f"max_games={max_games}")
    log.info("fetch window: %s", ", ".join(window) if window else "all archives")

    for month_url in archives:
        if max_games and stats.games_stored >= max_games:
            log.info("max_games reached (%d stored) — stopping", stats.games_stored)
            break
        prev = state.get(month_url)
        etag = prev["etag"] if prev else None
        last_modified = prev["last_modified"] if prev else None
        status, games, new_etag, new_lm = client.get_month(month_url, etag=etag, last_modified=last_modified)
        if status == "not_modified":
            stats.months_304 += 1
            store_sync_state(conn, month_url, new_etag, new_lm)
            conn.commit()
            continue
        if status == "missing":
            stats.months_missing += 1
            continue
        assert games is not None
        stats.games_seen += len(games)
        upserted_uuids = [g["uuid"] for g in games if g.get("uuid")]
        written = upsert_games(conn, month_url, games)
        stats.games_stored += written
        sync_parsed(conn, config, only_uuids=upserted_uuids)
        store_sync_state(conn, month_url, new_etag, new_lm)
        conn.commit()
        stats.months_fetched += 1
        log.info("fetched %s: %d games", month_url, written)

    log.info(
        "fetch done: %d months (%d fetched, %d x304, %d missing), %d games stored",
        stats.months_seen,
        stats.months_fetched,
        stats.months_304,
        stats.months_missing,
        stats.games_stored,
    )
    return stats
