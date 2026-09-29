"""Fetch pipeline: walk archives, upsert games, parse them, persist sync state."""

from __future__ import annotations

import logging
import sqlite3

from .config import AppConfig
from .fetcher import PubApiClient, list_archives
from .ingestion import load_sync_state, store_sync_state, sync_parsed, upsert_games
from .pubapi_stats import FetchStats

log = logging.getLogger(__name__)


def run_fetch(conn: sqlite3.Connection, config: AppConfig) -> FetchStats:
    """Incremental, serial, idempotent fetch of all games for the configured username."""
    stats = FetchStats()
    client = PubApiClient(config.user_agent)
    archives = list_archives(client, config.username)
    stats.months_seen = len(archives)
    state = load_sync_state(conn)

    for month_url in archives:
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
