# Story 9: First-run onboarding wizard

**Status:** Done
**Depends on:** Story 8 (settings write path, ConfigWatcher)

## Goal

A fresh clone plus `docker compose up` leads a new user to a working setup —
no config copying, no exit code 2.

## Background (from user feedback)

```
web-1  | Invalid value for '--config' (env var: 'CHESS_ANALYSIS_CONFIG'): Path
web-1  | '/config/chess-analysis.toml' does not exist.
web-1 exited with code 2
```

The web container crashed because the user had not copied
`chess-analysis.toml.example` and set a username — with no guidance at all.

## Tasks

- `serve` starts even without a config file
- While no valid config exists the UI is in **wizard mode**:
  - every page redirects to `/setup`
  - `/setup` asks exactly one question — the chess.com username — and writes a
    complete config (container defaults) via the Story-8 `POST /api/settings`
  - `GET /api/status` reports `setup_required` so clients can react
  - other API endpoints answer 409 `setup required`
- After the wizard saves, the `ConfigWatcher` serves the normal UI immediately
  (no container restart); the first suggested action is *fetch*
- CLI fallback for non-Docker users: `chess-analysis setup` (prompts for the
  username) and actionable error hints on every command when the config is
  missing or invalid

## Acceptance criteria

- [x] Fresh clone + `docker compose up` → browser shows the wizard, not a crash
- [x] Entering a username produces a working config and the full UI without a restart
- [x] `fetch` is runnable right after the wizard
- [x] CLI commands explain what to do when the config is missing (example file, setup, web wizard)
