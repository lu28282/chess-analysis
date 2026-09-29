"""Story 3 tests: ingestion — parsing, derived fields, ECO, query helpers, resilience."""

from __future__ import annotations

import textwrap

import pytest

from chess_analysis.db import query_games
from chess_analysis.ingestion import (
    classify_opening,
    extract_clocks,
    load_eco_table,
    lookup_opening,
    parse_game,
    sync_parsed,
)
from tests.conftest import PGN_TEMPLATE, make_archive_game


def _row(db, uuid):
    return db.execute("SELECT * FROM games WHERE uuid=?", (uuid,)).fetchone()


def _insert(db, uuid, pgn, time_class="rapid"):
    # players/ratings are parsed from the DB columns by parse_game; keep them
    # consistent with the PGN headers used in tests
    import re

    def header(tag):
        m = re.search(rf'\[{tag} "([^"]*)"\]', pgn)
        return m.group(1) if m else None

    white, black = header("White"), header("Black")
    def elo(tag):
        v = header(tag)
        return int(v) if v and v.isdigit() else None
    db.execute(
        """
        INSERT INTO games (uuid, month, end_time, pgn, white, black, white_rating,
                           black_rating, result, time_control, time_class, rules)
        VALUES (?, '2024/04', 1714500000, ?, ?, ?, ?, ?, ?, '600', ?, 'chess')
        """,
        (uuid, pgn, white, black, elo("WhiteElo"), elo("BlackElo"), header("Result"), time_class),
    )
    db.commit()


GOOD_PGN = PGN_TEMPLATE.format(white="testuser", black="opponent", result="1-0",
                               white_elo=1500, black_elo=1400)


def test_parse_derived_fields(db, config):
    _insert(db, "g1", GOOD_PGN)
    parsed, errors = sync_parsed(db, config)
    assert (parsed, errors) == (1, 0)
    row = _row(db, "g1")
    assert row["parse_error"] is None
    assert row["my_color"] == "white"
    assert row["my_result"] == "win"
    assert row["opponent"] == "opponent"
    assert row["rating_delta"] == -100
    assert row["move_count"] == 4
    assert row["result_parsed"] == "1-0"
    assert row["opening_name"] is not None  # d4 d5 Bf4 is in the ECO table


def test_black_perspective(db, config):
    pgn = PGN_TEMPLATE.format(white="opponent", black="testuser", result="0-1",
                              white_elo=1400, black_elo=1500)
    _insert(db, "g2", pgn)
    sync_parsed(db, config)
    row = _row(db, "g2")
    assert row["my_color"] == "black"
    assert row["my_result"] == "win"
    assert row["rating_delta"] == -100
    assert row["opponent"] == "opponent"


def test_draw_and_unknown_results(db, config):
    pgn = PGN_TEMPLATE.format(white="testuser", black="opponent", result="1/2-1/2",
                              white_elo=1500, black_elo=1400)
    _insert(db, "g3", pgn)
    sync_parsed(db, config)
    assert _row(db, "g3")["my_result"] == "draw"


def test_variant_games_get_no_opening_classification(db, config):
    """Crazyhouse & co. must not be classified as Jobava/Caro (story 6 bug).

    Their pocket FENs (e.g. 'RNBQKBNR[]') would otherwise poison the book.
    """
    pgn = PGN_TEMPLATE.format(white="testuser", black="opponent", result="1-0",
                              white_elo=1500, black_elo=1400)
    _insert(db, "std", pgn)
    db.execute(
        "UPDATE games SET rules = 'crazyhouse' WHERE uuid = 'std'"
    )
    db.commit()
    parsed, errors = sync_parsed(db, config)
    assert (parsed, errors) == (1, 0)
    row = _row(db, "std")
    assert row["parse_error"] is None
    assert row["opening_class"] is None
    assert row["variation_key"] is None
    assert row["opening_code"] is None
    # move semantics stay untouched
    assert row["my_color"] == "white"
    assert row["move_count"] == 4


def test_malformed_pgn_flagged_not_fatal(db, config):
    _insert(db, "ok", GOOD_PGN)
    _insert(db, "bad", "this is not a pgn at all")
    _insert(db, "empty", "")
    parsed, errors = sync_parsed(db, config)
    assert parsed == 1
    assert errors == 2
    assert _row(db, "bad")["parse_error"] is not None
    assert _row(db, "ok")["parse_error"] is None


def test_query_helpers_filter_everything(db, config):
    _insert(db, "g1", GOOD_PGN)
    _insert(db, "g2", GOOD_PGN.replace("[TimeClass \"rapid\"]", '[TimeClass "blitz"]'), time_class="blitz")
    sync_parsed(db, config)
    assert len(query_games(db, color="white")) == 2
    assert len(query_games(db, color="black")) == 0
    assert len(query_games(db, time_class="blitz")) == 1
    assert len(query_games(db, result="win")) == 2
    assert len(query_games(db, opponent="opponent")) == 2
    assert len(query_games(db, opponent="OPPONENT")) == 2  # case-insensitive
    assert len(query_games(db, opening="Queen's pawn")) == 2  # d4 d5 Bf4
    assert len(query_games(db, opening="nonexistent")) == 0
    assert len(query_games(db, date_from="2024-01-01", date_to="2024-12-31")) == 2
    assert len(query_games(db, date_from="2025-01-01")) == 0


def test_eco_lookup_longest_prefix():
    table = load_eco_table()
    eco, name = lookup_opening(["e4", "c6", "d4", "d5"], table)
    assert eco is not None
    assert "Caro" in name


def test_eco_lookup_no_match_returns_none():
    table = [("A00", "test", ["z9"])]
    assert lookup_opening(["e4", "e5"], table) == (None, None)


def test_extract_clocks():
    import chess.pgn
    import io

    game = chess.pgn.read_game(io.StringIO(GOOD_PGN))
    clocks = extract_clocks(game)
    assert len(clocks) == 4
    assert clocks[0] == 600.0


def test_opening_classification_jobava():
    from chess_analysis.config import AppConfig

    cfg = AppConfig(username="u", db_path="x", user_agent="a", reports_dir="r",
                   engine_path="e", eval_ms=1, eval_depth=0, multipv=1, threads=1, hash_mb=1)
    jobava = ["d4", "Nf6", "Nc3", "d5", "Bf4"]
    assert classify_opening("white", jobava, cfg) == (
        "jobava_london",
        " ".join(jobava[:12]),
    )
    deviated = ["d4", "Nf6", "Nf3", "d5", "Bf4"]  # never played Nc3
    cls, key = classify_opening("white", deviated, cfg)
    assert cls == "jobava_avoided"
    assert key == " ".join(deviated[:12])
    other = ["e4", "e5", "Nf3", "Nc6"]
    assert classify_opening("white", other, cfg)[0] == "other"


def test_opening_classification_caro_kann():
    from chess_analysis.config import AppConfig

    cfg = AppConfig(username="u", db_path="x", user_agent="a", reports_dir="r",
                   engine_path="e", eval_ms=1, eval_depth=0, multipv=1, threads=1, hash_mb=1)
    caro = ["e4", "c6", "d4", "d5", "Nc3", "dxe4"]
    cls, key = classify_opening("black", caro, cfg)
    assert cls == "caro_kann"
    assert key == " ".join(caro[:12])
    anti_caro = ["e4", "c6", "e5"]  # Advance variation: still caro_kann family
    assert classify_opening("black", anti_caro, cfg)[0] == "caro_kann"
    avoided = ["e4", "e5"]  # played something else vs 1.e4
    assert classify_opening("black", avoided, cfg)[0] == "caro_avoided"
    not_e4 = ["d4", "d5"]
    assert classify_opening("black", not_e4, cfg)[0] == "other"


def test_migrations_run_twice_safely(config):
    from chess_analysis.db import open_db

    conn = open_db(config.db_path)
    conn.close()
    conn = open_db(config.db_path)  # second startup must be a no-op
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version >= 2
    conn.close()
