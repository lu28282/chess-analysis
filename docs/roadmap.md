# Roadmap: Chess Game Analysis Tool

Derived from the Chess.com API review (story 1, complete — see `story-chess-api-review.md`).

**Stack:** Python · chess.com PubAPI · SQLite · python-chess · Stockfish · HTML reports — **all inside Docker; nothing runs natively on the host**

## Runtime model: Docker + Docker Compose

Everything executes in containers; the host only needs Docker and a browser.

- **Single image** containing Python, the tool, and the Stockfish binary; **compose profiles** select the command as one-shot jobs:
  - `docker compose run fetch` — incremental game sync (serial PubAPI client)
  - `docker compose run analyze [--limit N]` — Stockfish review of unanalyzed games (on-demand, resumable)
  - `docker compose run report-mistakes` / `report-openings` — HTML report generation
- **Persistence via bind mounts**, no volumes inside the image:
  - `./data/chess-analysis.db` → SQLite (games + analysis)
  - `./config/chess-analysis.toml` → read-only config
  - `./reports/` → generated HTML, opened directly in the browser (no server)
- **CPU is the only cost:** the analyze profile runs only when invoked; the container is one-shot, so no idle CPU. Backlog is chipped away with repeated `docker compose run analyze --limit N`.
- Image pinned in `compose.yaml`; `fetch` and `report` jobs have no network needs beyond chess.com (fetch) and none at all (reports, once the opening book is built).

## Product goals1. **Find and fix my typical mistakes** — recurring blunder patterns with examples
2. **Improve my two openings** — Jobava London (White) and Caro-Kann (Black vs 1.e4, all anti-Caro lines)

## Global decisions

- **Storage:** SQLite only — single DB file, PGN stored as text, schema versioned
- **Analysis:** full review, ~1s/position (chess.com-review granularity), results cached per game
- **Classification:** chess.com-style — eval swings converted to win% deltas
- **Reporting:** self-contained HTML reports with charts, offline
- **Fetching:** serial requests only, custom User-Agent, ETag caching
- **Config:** TOML config file (`chess-analysis.toml`) — username(s), DB path, engine budget, report output dir; sane defaults
- **CLI:** Typer — subcommands `fetch`, `analyze`, `report-mistakes`, `report-openings` with typed options and auto-help

## Stories

| # | Story | File | Depends on |
|---|---|---|---|
| 1 | Chess.com API review (done) | `story-chess-api-review.md` | — |
| 2 | Fetcher — serial PubAPI client with ETag caching | [`stories/02-fetcher.md`](stories/02-fetcher.md) | 1 |
| 3 | Storage & ingestion pipeline | [`stories/03-storage.md`](stories/03-storage.md) | 2 |
| 4 | Stockfish analysis engine + per-blunder metadata | [`stories/04-analysis.md`](stories/04-analysis.md) | 3 |
| 5 | Mistake-pattern report | [`stories/05-mistake-patterns.md`](stories/05-mistake-patterns.md) | 4 |
| 6 | Opening coach — Jobava London & Caro-Kann | [`stories/06-opening-coach.md`](stories/06-opening-coach.md) | 3, 4 |

## Dependency order

```
Story 2 (fetcher) → Story 3 (ingestion) → Story 4 (analysis) ─┬→ Story 5 (mistake patterns)
                                                             └→ Story 6 (opening coach)
```

Stories 2+3 can ship in one PR (schema needed at fetch time anyway).
Story 4 needs a local Stockfish install and produces the per-blunder metadata both reports aggregate.
Stories 5 and 6 are independent of each other; Story 6 additionally builds a reference opening book from free lichess data (one-time, then offline).
