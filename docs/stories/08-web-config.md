# Story 8: Web UI settings — everything configurable in the browser

**Status:** Done
**Depends on:** — (foundation for stories 9, 10, 11, 12)
**Commands introduced:** `chess-analysis setup` (config helper)

## Goal

The TOML config file stays the **single source of truth**, and the web UI gets
a settings page that reads and writes it. No CLI or file editing required for
any day-to-day configuration.

## Background (from user feedback)

Everything was configured via a hand-edited TOML file, and the config bind mount
was even read-only. Users hit raw Typer errors instead of a UI.

## Tasks

- `src/chess_analysis/settings.py` (new)
  - `SCHEMA`: every editable setting and its type — unknown keys are rejected
  - `update_settings(path, payload)`: partial payload overlaid onto the current
    file (complete defaults when no file exists yet), normalized, validated by
    building a full `AppConfig`, then written **atomically** (tmp + rename)
  - Never touches the file on invalid input
- Config model (`config.py`)
  - `config_from_dict` extracted from `load_config` for reuse as the validator
  - new sections: `[fetch]` (months/since/max_games window), `[ui]`
    (default_time_class); `workers` now accepts `0 = auto (one per core)`
- Web UI
  - `GET /settings` — form for every setting: username, user-agent, engine
    budget, workers, thresholds, fetch window, time-class default, explorer,
    opening limits
  - `GET /api/settings` — current values (file merged over defaults) + schema
  - `POST /api/settings` — validate + write; the `ConfigWatcher` picks up the
    new file immediately (running jobs always read the config from disk when
    they start, so changes apply to the next run)
  - `ConfigWatcher`: reloads the config on file change; a broken file keeps
    the last good config and surfaces the error on the settings page
  - per-run parameters: `POST /api/run/<step>` accepts `{"params": {...}}`;
    `fetch` takes months/since/max_games, `analyze` takes limit/workers
    (validated in `_step_args`)
  - `EngineConsultant` respawns the shared Stockfish when engine path, threads,
    or hash change
- Compose: `./config` mount changed from read-only to read-write
- CLI: raw "Path does not exist" errors replaced with actionable hints;
  new `setup` command creates the config interactively

## Acceptance criteria

- [x] Every config value is editable from `/settings` and persists to the TOML file
- [x] Saving invalid input is rejected with a field-level message; the file is unchanged
- [x] Saved settings apply to the next job run without restarting the container
- [x] Fetch/analyze runs can be parameterized from the UI (window, limit, workers)
- [x] A broken config file shows its error in the UI instead of killing it

## Notes

- Comments in a manually maintained TOML are lost when the page rewrites the
  file (tomli-w limitation); the generated header points at the example file
  for documentation.
