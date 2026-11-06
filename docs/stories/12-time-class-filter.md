# Story 12: Filter by game mode (time class)

**Status:** Done
**Depends on:** Story 8 (settings: configurable default filter)

## Goal

Mistakes in 2-minute games are different from mistakes in 10-minute games —
every view of the data can be scoped to bullet/blitz/rapid/daily.

## Background (from user feedback)

"wenn man nach spielmodi filtern kann. ich hab halt voll viele so 2 minuten
games gemacht, da macht man natürlich ganz andere Fehler als bei 10 Minuten".

`games.time_class` was stored and indexed all along; the mistake report and
insights page already had client-side filters. Missing: the game viewer, the
openings views, and a configurable default.

## Tasks

- New `[ui]` config key `default_time_class` ("" = all) — editable in
  `/settings`, preselected in every view it applies to
- Game viewer (`/viewer`): server-side filter (`/api/games?time_class=&color=`)
  plus time-class and color dropdowns in the game list
- Opening blunders page (`/openings`): client-side time-class filter over traps
  and patterns, with a live-updating summary
- Opening coach report (`report-openings`): server-side `--time-class`
  (defaults to the configured `ui.default_time_class`); the active filter is
  shown in the report header; book-off marks outside the filter scope are
  preserved
- Mistake report: keeps its client-side filter, now preselecting the default
- Insights page: preselects the default time class

## Acceptance criteria

- [x] The game viewer list can be filtered by time class and color
- [x] The openings page (traps + patterns) can be filtered by time class
- [x] The openings report can be generated for a single game mode; the header names the active filter
- [x] The default filter is configurable in the settings page and applies to all views
- [x] A filtered openings report run does not clear book-off marks of other game modes

## Notes

- chess.com `time_class` values: bullet, blitz, rapid, daily.
