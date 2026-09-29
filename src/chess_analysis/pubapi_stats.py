"""Fetch run statistics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FetchStats:
    months_seen: int = 0
    months_fetched: int = 0
    months_304: int = 0
    months_missing: int = 0
    games_seen: int = 0
    games_stored: int = 0
