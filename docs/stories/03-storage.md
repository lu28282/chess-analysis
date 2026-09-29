# Story 3: Storage & game ingestion pipeline

**Status:** Done
**Depends on:** Story 2 (fetcher + schema v1)
**Commands introduced:** `chess-analysis sync-parsed` (or folded into `fetch` as post-processing)

## Goal

Make stored games query-ready for analysis: parsed, deduplicated, enriched with derived fields, safe against bad data.

## Tasks

- PGN parsing with python-chess at ingest time (after fetch)
  - extract move count, result in `{1-0, 0-1, 1/2-1/2, *}`
- Derived columns (on `games` or a joined `game_meta` table)
  - `my_color`, `my_result` (win/loss/draw) resolved via config username
  - `opponent`, `rating_delta` (opponent rating − my rating)
  - `opening_name` via ECO lookup table (bundled CSV of ECO codes)
  - `phase_marks` placeholder for later (opening/middlegame/endgame split happens in Story 4)
- Query helpers: filter by date range, color, result, opponent, time class, opening
- DB migrations: numbered migration scripts applied on startup (`schema_version`)
- Integrity: malformed PGN → row flagged `parse_error`, sync continues; never fail the whole run

## Acceptance criteria

- [ ] Every well-formed game is queryable by date range, color, result, opponent, time class, opening
- [ ] Malformed games are flagged in DB and do not break ingestion
- [ ] Schema migrations run automatically; adding a column later does not require manual DB surgery
- [ ] `my_result`/`my_color` derived from config username

## Notes

- Can ship in the same PR as Story 2 — the schema is needed at fetch time anyway.
- python-chess is also used by Story 4; introduce the dependency here.
