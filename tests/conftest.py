"""Shared test fixtures: config, in-memory DB, synthetic games."""

from __future__ import annotations

import sqlite3
import textwrap

import pytest

from chess_analysis.config import AppConfig, Thresholds, ExplorerConfig, OpeningConfig


@pytest.fixture()
def config(tmp_path) -> AppConfig:
    return AppConfig(
        username="testuser",
        db_path=tmp_path / "test.db",
        user_agent="chess-analysis-test",
        reports_dir=tmp_path / "reports",
        engine_path="/usr/games/stockfish",
        eval_ms=100,
        eval_depth=0,
        multipv=1,
        threads=1,
        hash_mb=16,
        thresholds=Thresholds(brilliant=0.0, best=0.6, good=5.0, inaccuracy=10.0, mistake=20.0, blunder=40.0),
        explorer=ExplorerConfig(endpoint="https://explorer.lichess.ovh/lichess", max_ply=12, min_games=20, top_moves=12, delay_ms=0, max_positions=600),
        opening=OpeningConfig(jobava_max_setup_ply=12, caro_kann_max_ply=12),
    )


@pytest.fixture()
def db(config) -> sqlite3.Connection:
    from chess_analysis.db import open_db

    conn = open_db(config.db_path)
    yield conn
    conn.close()


PGN_TEMPLATE = textwrap.dedent(
    """\
    [Event "Live Chess"]
    [Site "Chess.com"]
    [Date "2024.05.01"]
    [White "{white}"]
    [Black "{black}"]
    [Result "{result}"]
    [WhiteElo "{white_elo}"]
    [BlackElo "{black_elo}"]
    [TimeControl "600"]
    [TimeClass "rapid"]
    [Rules "chess"]
    [ECO "D01"]

    1. d4 {{[%clk 0:10:00]}} d5 {{[%clk 0:10:00]}} 2. Bf4 {{[%clk 0:09:59]}} Nf6 {{[%clk 0:09:58]}} {result}
    """
)


def make_archive_game(uuid: str, white: str = "testuser", black: str = "opponent",
                      result: str = "1-0", white_elo: int = 1500, black_elo: int = 1400,
                      pgn: str | None = None) -> dict:
    return {
        "uuid": uuid,
        "end_time": 1714500000,
        "pgn": pgn or PGN_TEMPLATE.format(white=white, black=black, result=result, white_elo=white_elo, black_elo=black_elo),
        "white": {"username": white, "rating": white_elo},
        "black": {"username": black, "rating": black_elo},
        "result": result,
        "time_control": "600",
        "time_class": "rapid",
        "rules": "chess",
        "eco": "D01",
    }
