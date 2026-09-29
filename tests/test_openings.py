"""Story 6 tests: reference book builder, book-walk, openings report."""

from __future__ import annotations

import json

import chess
import pytest

from chess_analysis import opening_book as ob
from chess_analysis.ingestion import sync_parsed
from chess_analysis.opening_book import (
    _allowed_expansion,
    build_book,
    build_local_book,
    load_book,
    mainline_san,
    walk_game_through_book,
)
from chess_analysis.report_openings import collect_data, generate_report


# ---------------------------------------------------------------- stub explorer

def _stub_data() -> dict[str, list[dict]]:
    """Canned lichess-explorer responses, keyed by position FEN."""
    data: dict[str, list[dict]] = {}

    def put(moves: list[str], entries: list[tuple[str, str, int, int, int]]) -> None:
        board = chess.Board()
        for uci in moves:
            board.push(chess.Move.from_uci(uci))
        data[board.fen()] = [
            {"uci": u, "san": s, "white": w, "draws": d, "black": b}
            for u, s, w, d, b in entries
        ]

    # --- jobava_london tree (root and white plies are constrained by the builder)
    put([], [("d2d4", "d4", 500, 50, 450), ("e2e4", "e4", 9000, 900, 8800)])
    put(["d2d4"], [("d7d5", "d5", 300, 30, 290), ("g8f6", "Nf6", 200, 20, 190)])
    put(["d2d4", "g8f6"], [("c1f4", "Bf4", 80, 8, 75), ("b1c3", "Nc3", 60, 6, 55)])
    put(["d2d4", "d7d5"], [("b1c3", "Nc3", 150, 15, 140), ("c2c4", "c4", 400, 40, 390)])
    put(["d2d4", "d7d5", "b1c3"], [
        ("g8f6", "Nf6", 200, 20, 190),   # most played -> mainline
        ("c7c5", "c5", 150, 15, 140),    # popular but never faced -> gap
    ])
    put(["d2d4", "d7d5", "b1c3", "g8f6"], [("c1f4", "Bf4", 90, 9, 85)])
    put(["d2d4", "d7d5", "b1c3", "g8f6", "c1f4"], [("e7e6", "e6", 50, 5, 45)])
    put(["d2d4", "d7d5", "b1c3", "g8f6", "c1f4", "e7e6"], [("e2e3", "e3", 30, 3, 28)])
    # --- caro_kann tree
    put(["e2e4"], [("c7c6", "c6", 800, 80, 770)])
    put(["e2e4", "c7c6"], [("d2d4", "d4", 700, 70, 680)])
    put(["e2e4", "c7c6", "d2d4"], [("d7d5", "d5", 600, 60, 580), ("e7e5", "e5", 30, 3, 28)])
    put(["e2e4", "c7c6", "d2d4", "d7d5"], [("e4d5", "exd5", 200, 20, 190)])
    return data


class StubExplorer:
    def __init__(self, endpoint, user_agent, delay_ms, timeout_s=30.0):
        self.request_count = 0
        self._data = _stub_data()

    def top_moves(self, fen, top_n):
        self.request_count += 1
        return self._data.get(fen, [])


@pytest.fixture()
def book_db(db, monkeypatch):
    monkeypatch.setattr(ob, "ExplorerClient", StubExplorer)
    build_book(db, _cfg_with_explorer())
    return db


# ---------------------------------------------------------------- builder tests

def test_allowed_expansion_rules():
    b = chess.Board()
    assert _allowed_expansion("jobava_london", b) == ["d4"]  # classifier requires 1.d4
    assert _allowed_expansion("caro_kann", b) == ["e4"]

    b.push_uci("d2d4")
    assert _allowed_expansion("caro_kann", b) == ["c6"]
    b.push_uci("c7c6")
    assert _allowed_expansion("caro_kann", b) is None

    j = chess.Board()
    for uci in ("d2d4", "d7d5", "b1c3", "g8f6"):
        j.push_uci(uci)
    assert _allowed_expansion("jobava_london", j) == ["Bf4"]
    j.push_uci("c1f4")
    assert _allowed_expansion("jobava_london", j) is None

    off = chess.Board()
    off.push_uci("e2e4")  # White already left the Jobava system
    assert _allowed_expansion("jobava_london", off) == []


def test_build_book_only_follows_system(db, monkeypatch):
    monkeypatch.setattr(ob, "ExplorerClient", StubExplorer)
    n = build_book(db, _cfg_with_explorer())
    assert n > 0

    book = load_book(db, "jobava_london")
    root = chess.Board().fen()
    assert set(book[root]) == {"d2d4"}  # e4 filtered out (not in the setup)

    # white's second/third moves are restricted to the setup
    after_d4_d5 = chess.Board()
    after_d4_d5.push_uci("d2d4")
    after_d4_d5.push_uci("d7d5")
    assert set(book[after_d4_d5.fen()]) == {"b1c3"}  # c4 filtered out

    # caro: 1.e4 c6 forced, then free
    caro = load_book(db, "caro_kann")
    root_moves = set(caro[chess.Board().fen()])
    assert root_moves == {"e2e4"}
    after_e4 = chess.Board()
    after_e4.push_uci("e2e4")
    assert set(caro[after_e4.fen()]) == {"c7c6"}

    # mainline follows the most-played moves
    assert mainline_san(db, "jobava_london")[:5] == ["d4", "d5", "Nc3", "Nf6", "Bf4"]
    assert mainline_san(db, "caro_kann") == ["e4", "c6", "d4", "d5", "exd5"]


def _cfg_with_explorer():
    from chess_analysis.config import ExplorerConfig

    class _Cfg:
        explorer = ExplorerConfig(
            endpoint="stub", max_ply=12, min_games=20, top_moves=12,
            delay_ms=0, max_positions=600,
        )
        user_agent = "test"

    return _Cfg()


# ---------------------------------------------------------------- walk tests

def test_walk_game_through_book():
    data = _stub_data()
    book: dict = {}

    def add(moves):
        board = chess.Board()
        for uci in moves:
            parent = board.fen()
            move = chess.Move.from_uci(uci)
            san = board.san(move)
            board.push(move)
            book.setdefault(parent, {})[uci] = {"san": san, "ply": len(moves)}

    add(["d2d4", "d7d5", "b1c3", "g8f6", "c1f4", "e7e6", "e2e3"])

    ucis = ["d2d4", "d7d5", "b1c3", "g8f6", "c1f4", "e7e6", "e2e3", "c7c5"]
    off_ply, off_side, visited = walk_game_through_book(ucis, book, 12)
    assert off_ply == 8
    assert off_side == "black"  # 4...c5 was black's off-book move
    assert len(visited) == 7

    # fully in-book game: no leave point
    off_ply2, off_side2, _ = walk_game_through_book(ucis[:7], book, 12)
    assert off_ply2 is None and off_side2 is None

    # max_ply caps the walk
    off_ply3, _, _ = walk_game_through_book(ucis, book, 4)
    assert off_ply3 is None


def test_build_local_book(db, config):
    # five Jobava games sharing 1.d4 d5 2.Nc3, diverging afterwards
    for i in range(5):
        db.execute(
            """INSERT INTO games (uuid, month, end_time, pgn, white, black, white_rating,
                                   black_rating, result, time_control, time_class, rules,
                                   my_color, my_result, opening_class, variation_key, parse_error)
               VALUES (?, '2024/05', 1714500000, ?, 'testuser', 'opponent', 1500,
                       1400, '1-0', '300', 'blitz', 'chess', 'white', 'win',
                       'jobava_london', 'd4 d5 Nc3', NULL)""",
            (f"loc-{i}", JOBAVA_PGN),
        )
    db.commit()
    sync_parsed(db, config)  # result_parsed is set by the parse step, as in real flow

    n = build_local_book(db, config)
    assert n > 0

    book = load_book(db, "jobava_london")
    root = chess.Board().fen()
    assert set(book[root]) == {"d2d4"}
    after_d4 = chess.Board()
    after_d4.push_uci("d2d4")
    assert set(book[after_d4.fen()]) == {"d7d5"}

    # provenance recorded
    src = db.execute("SELECT DISTINCT source FROM opening_book").fetchall()
    assert {r["source"] for r in src} == {"local"}


def test_build_local_book_skips_off_system(db, config):
    # caro games where white deviates from anti-Caro lines are walked up to the
    # forced plies only; the deviation move itself is never stored as an edge
    pgn = CARO_PGN.replace("3. exd5 cxd5", "3. Nf3 Bf5")  # white avoids anti-Caro lines
    for i in range(5):
        db.execute(
            """INSERT INTO games (uuid, month, end_time, pgn, white, black, white_rating,
                                   black_rating, result, time_control, time_class, rules,
                                   my_color, my_result, opening_class, variation_key, parse_error)
               VALUES (?, '2024/05', 1714500000, ?, 'opponent', 'testuser', 1500,
                       1400, '0-1', '300', 'blitz', 'chess', 'black', 'loss',
                       'caro_kann', 'e4 c6 d4', NULL)""",
            (f"loc-off-{i}", pgn),
        )
    db.commit()
    sync_parsed(db, config)  # result_parsed is set by the parse step, as in real flow
    build_local_book(db, config)
    caro = load_book(db, "caro_kann")
    root = chess.Board().fen()
    assert set(caro[root]) == {"e2e4"}
    after_e4 = chess.Board()
    after_e4.push_uci("e2e4")
    assert set(caro[after_e4.fen()]) == {"c7c6"}
    after_c6 = after_e4.copy()
    after_c6.push_uci("c7c6")
    assert set(caro[after_c6.fen()]) == {"d2d4"}
    after_d4 = after_c6.copy()
    after_d4.push_uci("d2d4")
    assert set(caro[after_d4.fen()]) == {"d7d5"}  # black's free 2...d5
    after_d5 = after_d4.copy()
    after_d5.push_uci("d7d5")
    # free choice from ply 5 on: white's 3.Nf3 IS a book edge (population >= 5)
    assert set(caro[after_d5.fen()]) == {"g1f3"}


# ---------------------------------------------------------------- report tests

JOBAVA_PGN = """[Event "Live Chess"]
[Site "Chess.com"]
[Date "2024.05.01"]
[White "testuser"]
[Black "opponent"]
[Result "1-0"]
[TimeClass "blitz"]

1. d4 d5 2. Nc3 Nf6 3. Bf4 e6 4. e3 c5 1-0
"""

CARO_PGN = """[Event "Live Chess"]
[Site "Chess.com"]
[Date "2024.05.01"]
[White "opponent"]
[Black "testuser"]
[Result "0-1"]
[TimeClass "blitz"]

1. e4 c6 2. d4 d5 3. exd5 cxd5 4. c3 Nf6 0-1
"""


def _moves_meta(pgn_sans, side_blunder_ply):
    out = []
    for i, san in enumerate(pgn_sans, start=1):
        side = "white" if i % 2 == 1 else "black"
        bad = i == side_blunder_ply
        out.append(
            {
                "ply": i,
                "side": side,
                "san": san,
                "classification": "blunder" if bad else "good",
                "drop": 45.0 if bad else 0.5,
                "accuracy": 8.0 if bad else 96.0,
            }
        )
    return out


def _seed_report_db(db, monkeypatch):
    monkeypatch.setattr(ob, "ExplorerClient", StubExplorer)
    build_book(db, _cfg_with_explorer())

    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, white_rating,
                               black_rating, result, time_control, time_class, rules,
                               my_color, my_result, opening_class, variation_key, parse_error)
           VALUES ('g-jobava', '2024/05', 1714500000, ?, 'testuser', 'opponent', 1500,
                   1400, '1-0', '300', 'blitz', 'chess', 'white', 'win',
                   'jobava_london', 'd4 d5 Nc3', NULL)""",
        (JOBAVA_PGN,),
    )
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, white_rating,
                               black_rating, result, time_control, time_class, rules,
                               my_color, my_result, opening_class, variation_key, parse_error)
           VALUES ('g-caro', '2024/05', 1714600000, ?, 'opponent', 'testuser', 1500,
                   1400, '0-1', '300', 'blitz', 'chess', 'black', 'loss',
                   'caro_kann', 'e4 c6 d4', NULL)""",
        (CARO_PGN,),
    )
    # jobava game: opponent (black) went off-book at ply 8 (4...c5) and blunders at ply 10
    jobava_meta = _moves_meta(["d4", "d5", "Nc3", "Nf6", "Bf4", "e6", "e3", "c5", "a3", "Qb6"], 10)
    db.execute(
        """INSERT INTO analysis (game_uuid, engine_version, signature, input_checksum, analyzed_at,
                                 accuracy_white, accuracy_black, phases, moves, blunders)
           VALUES ('g-jobava', 'stub', 'sig', 'cs', '2024-05-01T00:00:00', 90.0, 40.0, ?, ?, ?)""",
        (json.dumps({}), json.dumps(jobava_meta), json.dumps([jobava_meta[9]])),
    )
    db.commit()


def test_openings_report(db, config, monkeypatch, tmp_path):
    _seed_report_db(db, monkeypatch)

    data = collect_data(db, config)

    # book-off detection + punishment
    assert data["jobava"]["games_count"] == 1
    row = db.execute(
        "SELECT book_off_ply, book_off_side FROM games WHERE uuid = 'g-jobava'"
    ).fetchone()
    assert row["book_off_ply"] == 8 and row["book_off_side"] == "black"
    # opponent left the book at 4...c5 (ply 8) and blundered at ply 10:
    # punished, so not in the failed-punish drill list
    line = next(o for o in data["jobava"]["opponent_lines"] if o["games"] == 1)
    assert line["avg_off_ply"] == 8
    assert line["punished"] == 1
    assert line["unpunished"] == 0

    # section-level stats
    assert data["jobava"]["off_by_opponent"] == 1
    assert data["jobava"]["off_by_me"] == 0
    assert data["jobava"]["failed_punish"] == []  # it *was* punished
    assert data["caro_kann"]["off_by_me"] == 1  # I left with 3...cxd5

    # repertoire gaps: popular book lines never faced
    gap_paths = [gp["path"] for gp in data["jobava"]["gaps"]]
    assert any(p.endswith("d4 Nf6") for p in gap_paths)
    assert any(p.endswith("d4 d5 Nc3 c5") for p in gap_paths)

    # classification summary
    assert data["class_counts"]["jobava_london"] == 1
    assert data["class_counts"]["caro_kann"] == 1

    # self-contained HTML
    out = generate_report(db, config)
    text = out.read_text(encoding="utf-8")
    assert out.name == "openings.html"
    assert "<script>{{chartjs}}</script>" not in text
    assert "Chart" in text
    assert 'id="data-json"' in text
    assert data["jobava"]["mainline"][0] == "d4"
    # payload slimmed: full per-game SAN lists are not shipped to the browser
    payload_block = text.split('id="data-json" type="application/json">', 1)[1]
    payload_block = payload_block.split("</script>", 1)[0]
    assert '"games_count": 1' in payload_block
    assert '"sans"' not in payload_block


def test_failed_punish_lists_opponent_deviations(db, config, monkeypatch):
    """Opponent goes off-book without blundering -> lands in failed_punish."""
    _seed_report_db(db, monkeypatch)
    # overwrite the jobava analysis: opponent left at ply 8 but stays clean
    clean = _moves_meta(["d4", "d5", "Nc3", "Nf6", "Bf4", "e6", "e3", "c5", "a3", "Qb6"], 99)
    db.execute("UPDATE analysis SET moves = ? WHERE game_uuid = 'g-jobava'", (json.dumps(clean),))
    db.commit()

    data = collect_data(db, config)
    jobava = data["jobava"]
    assert jobava["off_by_opponent"] == 1
    assert jobava["failed_punish"][0]["uuid"] == "g-jobava"
    assert jobava["failed_punish"][0]["san"] == "c5"
    # per-line counters track it
    line = next(o for o in jobava["opponent_lines"] if o["games"] == 1)
    assert line["unpunished"] == 1
    assert line["punished"] == 0


def test_empty_book_never_marks_leaves(db, config):
    """With no book built, no game is reported as leaving it."""
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, white_rating,
                               black_rating, result, time_control, time_class, rules,
                               my_color, my_result, opening_class, variation_key, parse_error)
           VALUES ('g-e', '2024/05', 1714500000, ?, 'testuser', 'opponent', 1500,
                   1400, '1-0', '300', 'blitz', 'chess', 'white', 'win',
                   'jobava_london', 'd4 d5', NULL)""",
        (JOBAVA_PGN,),
    )
    db.commit()
    data = collect_data(db, config)
    assert data["jobava"]["games_count"] == 1
    assert data["jobava"]["off_by_me"] == 0
    assert data["jobava"]["off_by_opponent"] == 0
    row = db.execute("SELECT book_off_ply FROM games WHERE uuid = 'g-e'").fetchone()
    assert row["book_off_ply"] is None


def test_explorer_client_401_and_backoff(monkeypatch):
    """401 aborts the build; transient 5xx errors are retried, then give up."""
    import time as time_mod

    from chess_analysis.opening_book import ExplorerClient

    class FakeResp:
        def __init__(self, status):
            self.status_code = status

    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return FakeResp(calls["statuses"].pop(0))

    client = ExplorerClient("https://explorer.example", "ua", delay_ms=0)
    monkeypatch.setattr(client._session, "get", fake_get)
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)

    calls["statuses"] = [401]
    assert client.top_moves(chess.Board().fen(), 5) is None
    assert calls["n"] == 1

    calls["statuses"] = [404]
    assert client.top_moves(chess.Board().fen(), 5) == []

    calls["statuses"] = [500, 503, 500]  # MAX_RETRIES=3 attempts, then give up
    assert client.top_moves(chess.Board().fen(), 5) is None
    assert calls["n"] == 1 + 1 + 3


def test_walk_off_at_ply_one():
    off_ply, off_side, visited = walk_game_through_book(
        ["e2e4"], {"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1": {"d2d4": {"san": "d4"}}},
        12,
    )
    assert off_ply == 1
    assert off_side == "white"
    assert visited == []
