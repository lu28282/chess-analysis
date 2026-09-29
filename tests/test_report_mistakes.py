"""Story 5 tests: mistake-pattern report aggregation and self-contained output."""

from __future__ import annotations

import json
import sqlite3

import chess

from chess_analysis.report_common import board_html, replay_game
from chess_analysis.report_mistakes import collect_data, generate_report

PGN_BLUNDER = """[Event "Live Chess"]
[White "testuser"]
[Black "opponent"]
[Result "0-1"]
[Link "https://www.chess.com/game/live/123"]

1. e4 {[%clk 0:03:00]} e5 {[%clk 0:03:00]} 2. Nf3 {[%clk 0:00:20]} Nc6 {[%clk 0:02:59]} 3. Bc4 {[%clk 0:02:58]} Bc5 {[%clk 0:02:57]} 0-1
"""

MOVES = []


def _moves_meta(collapse_ply: int) -> list[dict]:
    """Build per-move analysis metadata: white blunders at `collapse_ply`."""
    sans = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]
    out = []
    for i, san in enumerate(sans, start=1):
        side = "white" if i % 2 == 1 else "black"
        collapse = i == collapse_ply
        cp_before, cp_after = (30, -700) if collapse else (30, 25)
        winp_b, winp_a = 52.8, 12.0 if collapse else 52.0
        out.append(
            {
                "ply": i,
                "side": side,
                "san": san,
                "uci": f"m{i}",
                "cp_before": cp_before,
                "cp_after": cp_after,
                "winp_before": round(winp_b, 2),
                "winp_after": round(winp_a, 2),
                "drop": 40.8 if collapse else 0.8,
                "classification": "blunder" if collapse else "good",
                "accuracy": 8.1 if collapse else 96.0,
                "clock_s": 20.0 if collapse else 180.0,
                **({"meta": {"motifs": ["hanging_piece", "timeout_adjacent"],
                             "material_balance": 0, "clock_s": 20.0,
                             "open_files": 0, "position_type": "closed",
                             "king_castled": False, "time_trouble": True}}
                   if collapse else {}),
            }
        )
    return out


def _seed(db):
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, white_rating,
                               black_rating, result, time_control, time_class, rules, my_color, my_result, parse_error)
           VALUES ('g1', '2024/04', 1714500000, ?, 'testuser', 'opponent', 1500,
                   1400, '0-1', '180', 'blitz', 'chess', 'white', 'loss', NULL)""",
        (PGN_BLUNDER,),
    )
    meta = _moves_meta(collapse_ply=3)  # white's Nf3 was the blunder
    db.execute(
        """INSERT INTO analysis (game_uuid, engine_version, signature, input_checksum, analyzed_at,
                                 accuracy_white, accuracy_black, phases, moves, blunders)
           VALUES ('g1', 'stub', 'sig', 'cs', '2024-05-01T00:00:00', 50.0, 96.0, ?, ?, ?)""",
        (
            json.dumps({"opening": {"white": 50.0, "black": 96.0}, "middlegame": {"white": None, "black": None}, "endgame": {"white": None, "black": None}}),
            json.dumps(meta),
            json.dumps([m for m in meta if m["classification"] in ("mistake", "blunder")]),
        ),
    )
    db.commit()


def test_collect_data_aggregates(db, config):
    _seed(db)
    data = collect_data(db, config)
    assert len(data["games"]) == 1
    assert data["games"][0]["accuracy"] == 50.0
    # my blunders only (white), with enrichments for client-side filtering
    assert len(data["blunders"]) == 1
    b = data["blunders"][0]
    assert b["side"] == "white"
    assert b["time_class"] == "blitz"
    assert b["rating_bracket"] == "1400-1599"
    assert b["opponent_rating"] == 1400
    # per-move normalization denominators
    assert data["moves_by_color_tc"]["white|blitz"] == 3
    # every game carries accuracy + time class for the client-side trend chart
    assert data["games"][0]["accuracy"] == 50.0
    assert data["games"][0]["time_class"] == "blitz"


def test_motif_examples_have_diagrams_and_links(db, config):
    _seed(db)
    data = collect_data(db, config)
    ex = data["motif_examples"]["hanging_piece"][0]
    assert ex["game_uuid"] == "g1"
    assert ex["ply"] == 3
    assert "<table" in ex["diagram"]  # board diagram rendered
    assert ex["fen"].startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3")  # position before Nf3
    assert ex["link"] == "https://www.chess.com/game/live/123"
    # examples carry the filter dimensions for client-side filtering
    assert ex["color"] == "white"
    assert ex["time_class"] == "blitz"


def test_themes_aggregate_and_labels_merge(db, config):
    _seed(db)
    db.execute("INSERT INTO pattern_labels (theme_key, label) VALUES (?, ?)",
               (json.dumps(["hanging_piece", "closed", "opening", "behind/equal"]), "Hanging pieces in closed openings"))
    db.commit()
    data = collect_data(db, config)
    assert data["themes"]
    t = data["themes"][0]
    assert t["motif"] == "hanging_piece"
    assert t["label"] == "Hanging pieces in closed openings"
    assert t["examples"][0]["game_uuid"] == "g1"
    assert "diagram" in t["examples"][0]


def test_report_is_self_contained_html(db, config, tmp_path):
    _seed(db)
    out = generate_report(db, config, tmp_path / "m.html")
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "Chart" in html  # chart.js inlined
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "https://cdn" not in html
    assert "https://api" not in html
    # the embedded data carries filter dimensions
    assert '"color"' in html or '"my_color"' in html
    # default output goes to reports_dir
    out2 = generate_report(db, config)
    assert out2.name == "mistake-patterns.html"
    assert out2.parent == config.reports_dir


def test_board_html_renders_start_position():
    html = board_html(chess.STARTING_FEN)
    assert html.count("♖") == 2 and html.count("♟") == 8


def test_replay_game():
    fen, link = replay_game(PGN_BLUNDER, 2)
    assert fen.startswith("rnbqkbnr/pppppppp/8/8/4P3")  # position before 1...e5
    assert link == "https://www.chess.com/game/live/123"
    assert replay_game("not a pgn", 1) is None
    assert replay_game(PGN_BLUNDER, 99) is None


def test_empty_db_report_still_renders(db, config, tmp_path):
    out = generate_report(db, config, tmp_path / "empty.html")
    assert out.exists()
    assert "Mistake patterns" in out.read_text(encoding="utf-8")
