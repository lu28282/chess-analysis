"""Typer CLI: fetch, sync-parsed, analyze, report-mistakes, report-openings, build-opening-book, setup, serve."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from .config import load_config, validate_since
from .db import open_db

app = typer.Typer(add_completion=False, help="Personal chess game analysis tool.")
CONFIG_OPT = typer.Option(
    Path("/config/chess-analysis.toml"),
    "--config",
    "-c",
    envvar="CHESS_ANALYSIS_CONFIG",
    help="Path to the TOML config file.",
)
# serve must start even without a config (it shows the setup wizard), so no exists=True
CONFIG_OPT_CREATE = typer.Option(
    CONFIG_OPT.default,
    "--config",
    "-c",
    envvar="CHESS_ANALYSIS_CONFIG",
    help="Path to the TOML config file (created by the setup wizard if missing).",
)
VERBOSE_OPT = typer.Option(False, "--verbose", "-v", help="Debug logging.")


def _setup(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _load_or_exit(config_path: Path):
    """load_config with an actionable error instead of a raw traceback."""
    try:
        return load_config(config_path)
    except FileNotFoundError:
        typer.secho(
            f"config file not found: {config_path}\n"
            "  - quick start: `chess-analysis setup` (interactive helper), or\n"
            "  - copy config/chess-analysis.toml.example to that path and set `username`, or\n"
            "  - run the web UI (`docker compose up`) and use its setup wizard at /setup",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    except ValueError as exc:
        typer.secho(f"invalid config {config_path}: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def setup(
    username: Optional[str] = typer.Option(None, "--username", "-u", help="chess.com username (prompted if omitted)."),
    config_path: Path = CONFIG_OPT_CREATE,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Create the config file interactively (no-op if it already validates)."""
    _setup(verbose)
    from .settings import update_settings

    if config_path.exists():
        try:
            load_config(config_path)
            typer.echo(f"config already exists and validates: {config_path}")
            return
        except (FileNotFoundError, ValueError):
            typer.echo("existing config is incomplete — completing it")
    while not (username and username.strip()):
        username = typer.prompt("chess.com username")
    try:
        update_settings(config_path, {"username": username.strip()})
    except (ValueError, OSError) as exc:
        typer.secho(f"could not write config {config_path}: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(f"config written: {config_path} — next: fetch your games or run `serve`")


@app.command()
def fetch(
    months: Optional[int] = typer.Option(
        None, "--months", "-m", min=0, help="Only fetch the newest N monthly archives."
    ),
    since: Optional[str] = typer.Option(
        None, "--since", help="Only fetch archives from this month on (YYYY-MM or YYYY-MM-DD)."
    ),
    max_games: Optional[int] = typer.Option(
        None, "--max-games", "-g", min=0, help="Stop after roughly N new games (month-granular)."
    ),
    config_path: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Incrementally download the configured user's games into SQLite.

    Window limits default to the config's [fetch] settings; 0 / empty = all games.
    """
    _setup(verbose)
    from .pipeline import run_fetch

    try:
        since = validate_since(since) if since else None
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=2)
    cfg = _load_or_exit(config_path)
    conn = open_db(cfg.db_path)
    try:
        stats = run_fetch(conn, cfg, months=months, since=since, max_games=max_games)
        typer.echo(
            f"fetch complete: {stats.months_fetched} months fetched, "
            f"{stats.months_304} unchanged (304), {stats.games_stored} games stored"
        )
    finally:
        conn.close()


@app.command()
def sync_parsed(
    config_path: Path = CONFIG_OPT,
    uuids: Optional[list[str]] = typer.Argument(None, help="Specific game UUIDs (default: all)."),
    verbose: bool = VERBOSE_OPT,
) -> None:
    """(Re)parse stored games into derived, query-ready fields."""
    _setup(verbose)
    from .ingestion import sync_parsed as _sync

    cfg = _load_or_exit(config_path)
    conn = open_db(cfg.db_path)
    try:
        parsed, errors = _sync(conn, cfg, only_uuids=list(uuids) if uuids else None)
        typer.echo(f"sync-parsed complete: {parsed} parsed, {errors} flagged parse_error")
    finally:
        conn.close()


@app.command()
def analyze(
    limit: int = typer.Option(None, "--limit", "-l", min=0, help="Analyze at most N games this run."),
    game: Optional[str] = typer.Option(None, "--game", help="Analyze a single game UUID (ignores freshness)."),
    force: bool = typer.Option(False, "--force", help="Re-analyze even if cached results look fresh."),
    workers: Optional[int] = typer.Option(
        None,
        "--workers",
        "-w",
        min=0,
        help="Analyze N games in parallel, each with its own Stockfish process (overrides config; 0 = auto = one per CPU core).",
    ),
    config_path: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Stockfish game review of unanalyzed games (oldest first, resumable)."""
    _setup(verbose)
    from .analysis import run_analyze

    cfg = _load_or_exit(config_path)
    conn = open_db(cfg.db_path)
    try:
        if game and not conn.execute(
            "SELECT 1 FROM games WHERE uuid = ? AND parse_error IS NULL AND pgn != ''", (game,)
        ).fetchone():
            typer.echo(f"no analyzable game with uuid {game!r} found")
            raise typer.Exit(code=1)
        n = run_analyze(conn, cfg, limit=limit, game_uuid=game, force=force, workers=workers)
        typer.echo(f"analyze complete: {n} games reviewed")
    except KeyboardInterrupt:
        typer.echo("analyze interrupted — progress is saved; re-run to continue")
    finally:
        conn.close()


@app.command()
def report_mistakes(
    output: Path = typer.Option(None, "--output", "-o", help="Output HTML file."),
    config_path: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Generate the self-contained mistake-pattern HTML report."""
    _setup(verbose)
    from .report_mistakes import generate_report

    cfg = _load_or_exit(config_path)
    conn = open_db(cfg.db_path)
    try:
        path = generate_report(conn, cfg, output)
        typer.echo(f"report written: {path}")
    finally:
        conn.close()


@app.command()
def build_opening_book(
    source: str = typer.Option(
        "lichess",
        "--source",
        help="'lichess' (opening-explorer API, needs network) or 'local' (games already in the DB).",
    ),
    config_path: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Build the reference opening book (one-time, then reports are offline)."""
    _setup(verbose)

    cfg = _load_or_exit(config_path)
    conn = open_db(cfg.db_path)
    try:
        if source == "lichess":
            from .opening_book import build_book

            n = build_book(conn, cfg)
            if n == 0:
                typer.secho(
                    "No book positions stored: the lichess explorer API is unreachable "
                    "or gated (401). Try `--source local` to build from the games "
                    "already in your DB.",
                    fg=typer.colors.YELLOW,
                )
                raise typer.Exit(1)
            typer.echo(f"opening book built: {n} positions")
        elif source == "local":
            from .opening_book import build_local_book

            n = build_local_book(conn, cfg)
            if n == 0:
                typer.secho(
                    "No book positions stored: no parsed Jobava/Caro games in the DB yet "
                    "(run `fetch` and `sync-parsed` first).",
                    fg=typer.colors.YELLOW,
                )
                raise typer.Exit(1)
            typer.echo(f"opening book built from local games: {n} positions")
        else:
            typer.secho(f"unknown source: {source!r} (use 'lichess' or 'local')", fg=typer.colors.RED)
            raise typer.Exit(2)
    finally:
        conn.close()


@app.command()
def report_openings(
    output: Path = typer.Option(None, "--output", "-o", help="Output HTML file."),
    time_class: Optional[str] = typer.Option(
        None,
        "--time-class",
        "-t",
        help="Restrict the report to one game mode (bullet/blitz/rapid/daily); default: the configured ui.default_time_class.",
    ),
    config_path: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Generate the opening coach HTML report (Jobava London & Caro-Kann)."""
    _setup(verbose)
    from .report_openings import generate_report

    if time_class is not None and time_class not in ("bullet", "blitz", "rapid", "daily"):
        typer.secho(f"unknown time class: {time_class!r} (bullet/blitz/rapid/daily)", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    cfg = _load_or_exit(config_path)
    conn = open_db(cfg.db_path)
    try:
        path = generate_report(conn, cfg, output, time_class=time_class)
        typer.echo(f"report written: {path}")
    finally:
        conn.close()


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address for the web UI."),
    port: int = typer.Option(8000, "--port", help="Port for the web UI."),
    config_path: Path = CONFIG_OPT_CREATE,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Start the web UI: run pipeline steps, view reports, edit settings.

    Without a config file the UI shows a first-run setup wizard.
    """
    _setup(verbose)
    from .webui import serve as webui_serve

    webui_serve(host, port, config_path)


if __name__ == "__main__":
    app()
