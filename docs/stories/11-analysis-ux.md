# Story 11: Analysis throughput & progress

**Status:** Done
**Depends on:** Story 8 (settings + run params)

## Goal

A 786-game backlog gives an immediate ETA, uses all CPU cores by default, and
reports progress while it runs.

## Background (from user feedback)

"schon 4 games analyzed von 786 — das kann mal durch die ganze Nacht rödeln".
Workers existed but defaulted to 1, and the run gave no sense of progress.

## Tasks

- `workers = 0` (new default) = **auto**: one analyze worker per CPU core;
  explicit values still win (`--workers`, settings, or per-run UI input)
- Backlog estimate logged at run start:
  `analyze backlog: ~782 games need review (786 analyzable stored, 4 with cached results), 8 worker(s))`
  — stale cached results (changed engine/budget/thresholds) count as needing review
- Progress line after every finished game:
  `progress: 37 done, 0 skipped of ~782 — 62m elapsed, ETA ~19h (35.6 games/h)`
  (sequential and parallel runners; the web UI job panel shows the log tail live)
- Web UI: the analyze step card has limit/workers inputs; `workers` is
  exposed in `/settings` with the 0 = auto semantics
- The analyze button in the UI therefore works for the "run it overnight on
  the home server" workflow without SSH or CLI knowledge

## Acceptance criteria

- [x] Default analyze run uses all cores (workers auto-detected, one Stockfish each)
- [x] Backlog size and worker count are logged at start
- [x] Every finished game logs a progress line with elapsed time, ETA, and rate
- [x] Limit and workers can be set per run from the web UI and persist as defaults in settings
- [x] Resumability and freshness checks unchanged (progress logging never skips or re-analyzes)

## Notes

- The backlog estimate counts a stored analysis as done only when its budget/
  thresholds signature and PGN checksum match — an engine-version change alone
  is invisible before spawning, so the estimate is an upper bound in that case.
