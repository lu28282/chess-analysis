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
| 7 | Multi-arch Docker image (Apple Silicon) | [`stories/07-apple-silicon.md`](stories/07-apple-silicon.md) | — |
| 8 | Web UI settings — everything configurable in the browser | [`stories/08-web-config.md`](stories/08-web-config.md) | — |
| 9 | First-run onboarding wizard | [`stories/09-onboarding.md`](stories/09-onboarding.md) | 8 |
| 10 | Fetch window limits | [`stories/10-fetch-limits.md`](stories/10-fetch-limits.md) | 8 |
| 11 | Analysis throughput & progress | [`stories/11-analysis-ux.md`](stories/11-analysis-ux.md) | 8 |
| 12 | Filter by game mode (time class) | [`stories/12-time-class-filter.md`](stories/12-time-class-filter.md) | 8 |

## Dependency order

```
Story 2 (fetcher) → Story 3 (ingestion) → Story 4 (analysis) ─┬→ Story 5 (mistake patterns)
                                                              └→ Story 6 (opening coach)

Story 7 (multi-arch image)  — independent
Story 8 (web config) ─┬→ Story 9 (onboarding wizard)
                      ├→ Story 10 (fetch limits)
                      ├→ Story 11 (analysis UX)
                      └→ Story 12 (time-class filter)
```

Stories 2+3 shipped in one PR; stories 7–12 came out of first user feedback
(a Mac user's setup struggles and feature wishes) — see the story files for
the original quotes.
