# Story 10: Fetch window limits

**Status:** Done
**Depends on:** Story 8 (settings + run params)

## Goal

Fetching only the games you care about, not every monthly archive since the
account exists.

## Background (from user feedback)

A new user with a large archive had to wait for the full history before doing
anything; the wish was "only fetch the last X games".

## Tasks

- Config: new `[fetch]` section — `months` (newest N archives), `since`
  (YYYY-MM or YYYY-MM-DD lower bound), `max_games` (stop after ~N new games);
  `0` / `""` = no limit
- CLI: `fetch --months N --since YYYY-MM --max-games N` (override the config)
- Pipeline (`select_archives`): the archive list is filtered before walking;
  `max_games` stops the walk once enough games were stored
- Web UI: the fetch step card has input fields for months / since / max games
  (validated server-side in `_step_args`) — empty fields fall back to the
  configured defaults
- ETag/Last-Modified caching is untouched: archives inside the window still
  cost nothing when unchanged

## Acceptance criteria

- [x] `fetch --months 1` downloads only the newest monthly archive
- [x] `--since 2024-06` skips older archives; a YYYY-MM-DD bound collapses to its month
- [x] `--max-games` stops the walk after roughly N new games
- [x] The window is configurable as a default in `/settings` and per run in the UI
- [x] Invalid `since` values are rejected with a clear message

## Notes

- Chess.com serves whole monthly archives, so every bound is month-granular;
  `max_games` stops between months (a month is fetched in full).
