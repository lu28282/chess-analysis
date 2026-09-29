# Story 4: Stockfish analysis engine

**Status:** Done
**Depends on:** Story 3 (query-ready games), local Stockfish binary
**Commands introduced:** `chess-analysis analyze [--limit N] [--game UUID]`

## Goal

chess.com-style game review: per-move classification, per-side accuracy, per-phase accuracy — cached so it never runs twice for the same game.

## Tasks

- UCI wrapper around the Stockfish binary **inside the container** (thin custom wrapper; no pip binding unless it clearly earns its keep)
  - engine path from config (container default: `/usr/games/stockfish` or wherever the Dockerfile installs it); version logged at run start
- Evaluation
  - ~1s per position by default (`eval_ms` in config, also depth cap)
  - eval before and after each played move
- Classification — chess.com-style win% model
  - cp eval → win% via logistic (chess.com formula); classify on win% drop for the mover:
    brilliancy / best / good / inaccuracy / mistake / blunder
  - thresholds in config so they can be tuned after comparing against chess.com's own review
- Accuracy per side per game + per phase (opening / middlegame / endgame, by move number bands)
- Per-blunder metadata (feeds Story 5)
  - for every classified mistake/blunder: motif tags (hanging piece, fork/pin/skewer available, king safety, back-rank, pawn concession), material balance, remaining clock (`%clk`), position features (open/closed, king placement)
  - stored alongside the move classification so aggregation later needs no engine
- Result caching: `analysis` table keyed by game `uuid` (+ engine version, budget, thresholds — a config change can invalidate old results)
  - `analyzed_at`, checksum of inputs to detect staleness
- Batch runner: analyze oldest-unanalyzed games first, `--limit N`, resumable (Ctrl-C safe: committed per game)

## Acceptance criteria

- [ ] A game can be fully reviewed: every move classified, eval graph data stored
- [ ] Accuracy per side roughly matches chess.com's own review for the same game (spot-check 2–3 games)
- [ ] Interrupted batch run resumes without re-analyzing finished games
- [ ] Changing thresholds/engine budget marks affected results stale (re-analysis is explicit)
- [ ] Results stored in DB keyed by game uuid
- [ ] Every blunder/mistake carries motif + position metadata

## Notes

- Budget math: a 60-move game at 1s/position ≈ 2 minutes (both sides evaluated). A 500-game backlog ≈ days — resumability matters.
- Runs as one-shot `docker compose run analyze --limit N` — repeat to chip away; the container exits when the batch is done, so idle CPU is zero. Resumability makes interrupting (`Ctrl-C`, closing the laptop) free.
- Lichess-style "knockout" helpers (accuracy from win% deltas) keep numbers comparable to chess.com.
