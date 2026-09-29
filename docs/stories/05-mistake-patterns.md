# Story 5: Mistake-pattern report

**Status:** Done
**Depends on:** Story 3 (games), Story 4 (analysis results incl. blunder metadata)
**Commands introduced:** `chess-analysis report-mistakes [--output FILE]`

## Goal

Answer one question well: *what kind of mistakes do I typically make, and when?*

## Tasks

- Same delivery shape as previously planned: self-contained HTML, charts inline (Chart.js vendored), no server/network — written to the bind-mounted `./reports/` dir via `docker compose run report-mistakes`, opened directly in the browser
- Statistical views (from Story 4 data)
  - Blunder/mistake frequency by phase, move number, time class, color, rating bracket, opponent strength
  - Accuracy trend over time — am I improving?
  - Time-trouble view: blunders with low remaining clock vs. blunders with plenty of time (`%clk` from PGN)
  - Distribution of eval swings: are my blunders one-move catastrophes or slow accumulation of small errors?
- Mistake motifs (needs per-blunder metadata from Story 4)
  - Every stored blunder/mistake carries motif tags: hanging piece, missed fork/pin/skewer, king safety, back-rank, pawn structure concession, endgame technique, timeout-adjacent
  - Motif frequency table + examples: for each motif, the top N games/positions as a small diagram
- Recurring themes (the actual goal)
  - Cluster/aggregate blunder positions across games and surface recurring patterns, e.g. "missed queen-diagonal tactics in open positions", "endgame pawn-race misevaluation"
  - Method: aggregate motifs + position features (material balance, open/closed position, king position) and report the top recurring combinations — manual review of examples to name the pattern; no ML needed for v1
  - Each theme links to its example games

## Acceptance criteria

- [ ] Report opens offline in a browser
- [ ] I can see when I blunder most (phase/move/time class/time trouble)
- [ ] I can see what kinds of mistakes dominate (motif table)
- [ ] At least one actionable recurring theme is identifiable, with example games
- [ ] Views filterable by color and time class

## Notes

- Motif tagging happens at analysis time (Story 4) so the expensive part runs once; this story only aggregates.
- Theme naming stays human-in-the-loop for v1: the tool surfaces clusters, you read the examples and label them. Labels stored in DB so the report improves over time.
