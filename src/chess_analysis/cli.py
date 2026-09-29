"""Typer CLI: fetch, sync-parsed, analyze, report-mistakes, report-openings, build-opening-book."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from .config import load_config
from .db import open_db

app = typer.Typer(add_completion=False, help="Personal chess game analysis tool.")
CONFIG_OPT = typer.Option(
    Path("/config/chess-analysis.toml"),
    "--config",
    "-c",
    envvar="CHESS_ANALYSIS_CONFIG",
    help="Path to the TOML config file.",
    exists=True,
)
VERBOSE_OPT = typer.Option(False, "--verbose", "-v", help="Debug logging.")


def _setup(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def fetch(
    config_path: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Incrementally download all games of the configured username into SQLite."""
    _setup(verbose)
    from .pipeline import run_fetch

    cfg = load_config(config_path)
    conn = open_db(cfg.db_path)
    try:
        stats = run_fetch(conn, cfg)
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

    cfg = load_config(config_path)
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
        min=1,
        help="Analyze N games in parallel, each with its own Stockfish process (overrides config).",
    ),
    config_path: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Stockfish game review of unanalyzed games (oldest first, resumable)."""
    _setup(verbose)
    from .analysis import run_analyze

    cfg = load_config(config_path)
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

    cfg = load_config(config_path)
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

    cfg = load_config(config_path)
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
    config_path: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Generate the opening coach HTML report (Jobava London & Caro-Kann)."""
    _setup(verbose)
    from .report_openings import generate_report

    cfg = load_config(config_path)
    conn = open_db(cfg.db_path)
    try:
        path = generate_report(conn, cfg, output)
        typer.echo(f"report written: {path}")
    finally:
        conn.close()


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address for the web UI."),
    port: int = typer.Option(8000, "--port", help="Port for the web UI."),
    config_path: Path = CONFIG_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Start the web UI: run pipeline steps and view reports from the browser."""
    _setup(verbose)
    from .webui import serve as webui_serve

    webui_serve(host, port, config_path)


if __name__ == "__main__":
    app()
