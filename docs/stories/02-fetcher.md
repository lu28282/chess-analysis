# Story 2: Fetcher — serial PubAPI client with ETag caching

**Status:** Done
**Depends on:** Story 1 (API review)
**Commands introduced:** `chess-analysis fetch`

## Goal

Download all games of the configured chess.com username(s) incrementally into SQLite, politely and idempotently.

## Tasks

- Docker bootstrap (nothing runs on the host except Docker)
  - `Dockerfile`: Python base image + Stockfish binary + project code + non-root user; pinned base digest
  - `compose.yaml`: bind mounts `./data/` (SQLite), `./config/` (read-only), `./reports/`; one-shot `run` targets for `fetch`, `analyze`, `report-mistakes`, `report-openings`
  - Stockfish is installed in the image at this stage so later stories need no host installs
- Project bootstrap: Typer CLI skeleton, `chess-analysis.toml` config (mounted from `./config/`)
  - config keys: `username`, `db_path`, `user_agent` (default: `chess-analysis (github.com/<me>)`)
- PubAPI client
  - Serial requests only — one in flight, wait for response before next
  - Custom `User-Agent` header
  - `If-None-Match` / `If-Modified-Since` per month archive; `304 Not Modified` = skip
  - `429` → exponential backoff and retry; `404` (empty month) → no-op, not an error
- Archive walker: `GET /pub/player/{u}/games/archives` → per-month game JSON
- SQLite schema v1
  - `games`: `uuid` (PK), `month`, `end_time`, `pgn`, `white`, `black`, `white_rating`, `black_rating`, `result`, `time_control`, `time_class`, `rules`, `eco`, `accuracies`
  - `sync_state`: month URL → `etag`, `last_modified`, `last_synced_at`
  - `schema_version` table from day one
- Incremental sync: upsert on `uuid`; a replayed/changed month is detected via ETag change and games are inserted/updated

## Acceptance criteria

- [ ] `chess-analysis fetch` pulls all games for the configured username into SQLite
- [ ] Re-running fetch when nothing changed issues requests but stores nothing (all 304s)
- [ ] A changed month's new games are detected and stored
- [ ] No parallel requests are ever issued (client has no async/concurrent fetch path)
- [ ] Config file drives username, DB path, UA
- [ ] `docker compose run fetch` works with only Docker installed on the host; no host Python/tooling required

## Notes

- Archives refresh at most every 12–24 h; fetch is cheap to re-run thanks to ETag caching.
- Data is public; no auth, no private data involved.
