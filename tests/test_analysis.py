"""Story 4 tests: classification, accuracy, phases, motifs, caching, resumability."""

from __future__ import annotations

import json

import chess

from chess_analysis.analysis import (
    OPENING_PLIES,
    classify,
    drop_for_mover,
    hanging_pieces,
    king_on_back_rank,
    king_pressure,
    move_accuracy,
    move_is_fork,
    pawn_concession,
    phase_of,
    pinned_pieces,
    review_game,
    run_analyze,
    win_percent,
)
from chess_analysis.config import Thresholds

T = Thresholds(brilliant=0.0, best=0.6, good=5.0, inaccuracy=10.0, mistake=20.0, blunder=40.0)


class StubEngine:
    """Deterministic fake engine: evaluates via a fixed FEN->cp map."""

    def __init__(self, name="stub-1.0"):
        self.name = name
        self.eval_by_fen: dict[str, int] = {}

    def evaluate(self, board, *, eval_ms=1000, eval_depth=0, multipv=1):
        cp = self.eval_by_fen.get(board.fen(), 20)
        return [{"cp": cp, "mate": None, "pv": []}]

    def close(self):
        pass


# ---------------------------------------------------------------- math


def test_win_percent_zero_is_50():
    assert win_percent(0) == 50.0
    assert win_percent(1000) > 90
    assert win_percent(-1000) < 10


def test_drop_for_mover_flips_for_black():
    assert drop_for_mover(60, 50, "white") == 10
    assert drop_for_mover(40, 50, "black") == 10  # black improved when white win% fell
    assert drop_for_mover(50, 60, "white") == 0  # never negative


def test_classify_bands():
    assert classify(0.1, False, False, 0, T) == "best"
    assert classify(0.3, True, False, 0, T) == "best"
    assert classify(3.0, False, False, 0, T) == "good"
    assert classify(7.0, False, False, 0, T) == "good"
    assert classify(12.0, False, False, 0, T) == "inaccuracy"
    assert classify(25.0, False, False, 0, T) == "mistake"
    assert classify(45.0, False, False, 0, T) == "blunder"


def test_classify_brilliant_needs_best_and_sacrifice_and_winning():
    t = Thresholds(brilliant=1.0, best=0.6, good=5.0, inaccuracy=10.0, mistake=20.0, blunder=40.0)
    assert classify(0.0, True, True, 150, t) == "brilliant"
    assert classify(0.0, True, False, 150, t) == "best"  # not a sacrifice
    assert classify(0.0, True, True, 50, t) == "best"  # not winning enough
    assert classify(0.0, False, True, 150, t) == "best"  # not engine best


def test_is_sacrifice_requires_giveaway():
    from chess_analysis.analysis import is_sacrifice

    # queen lands on a pawn-attacked square with no capture: sacrifice
    board = chess.Board("4k3/8/6p1/8/8/7Q/8/K7 w - - 0 1")
    move = chess.Move.from_uci("h3f5")  # Qf5 attacked by the g6 pawn
    assert move in board.legal_moves
    assert is_sacrifice(board, move)
    # a safe queen move is not a sacrifice
    safe = chess.Move.from_uci("h3h2")
    assert safe in board.legal_moves
    assert not is_sacrifice(board, safe)
    # queen grabs a king-defended pawn: giveaway exchange
    board2 = chess.Board("4k3/3p4/8/8/3Q4/8/8/4K3 w - - 0 1")
    cap = chess.Move.from_uci("d4d7")  # Qxd7 Kxd7
    assert cap in board2.legal_moves
    assert is_sacrifice(board2, cap)
    # equal value capture is never a sacrifice
    board3 = chess.Board("r3k3/8/8/8/8/8/8/R3K3 w - - 0 1")
    fair = chess.Move.from_uci("a1a8")  # Rxa8
    assert fair in board3.legal_moves
    assert not is_sacrifice(board3, fair)


def test_skewer_available():
    from chess_analysis.analysis import skewer_available

    # black rook d1 x-rays white pawn d3 to the white queen d8
    board = chess.Board("3Qk3/8/8/8/8/3P4/8/3rK3 b - - 0 1")
    assert skewer_available(board, chess.WHITE)
    clean = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    assert not skewer_available(clean, chess.WHITE)


def test_move_accuracy_monotonic():
    assert move_accuracy(0.0) > move_accuracy(10.0) > move_accuracy(30.0)
    assert 0.0 <= move_accuracy(100.0) <= 100.0


def test_phase_bands():
    assert phase_of(1) == "opening"
    assert phase_of(OPENING_PLIES) == "opening"
    assert phase_of(21) == "middlegame"
    assert phase_of(61) == "endgame"


# ---------------------------------------------------------------- motifs


def test_hanging_pieces():
    board = chess.Board("4k3/8/8/8/4b3/8/8/4K2R b - - 0 1")
    # rook on h1 attacked by bishop b4, undefended
    assert "h1" in hanging_pieces(board, chess.WHITE)
    # the black bishop itself is not attacked by any white piece
    assert hanging_pieces(board, chess.BLACK) == []


def test_move_is_fork():
    board = chess.Board("r3k3/8/8/1N6/8/8/8/4K3 w - - 0 1")
    move = chess.Move.from_uci("b5c7")  # Nc7+ forks Ke8 and Ra8
    assert move in board.legal_moves
    assert move_is_fork(board, move)
    quiet = chess.Move.from_uci("b5a3")
    assert quiet in board.legal_moves
    assert not move_is_fork(board, quiet)


def test_pinned_pieces():
    board = chess.Board("4k3/8/8/8/8/4r3/4P3/4K2R w K - 0 1")
    # white pawn e2 pinned? No pin (rook attacks pawn square behind is king: e-file: rook e3, pawn e2, king e1 -> yes pin!)
    assert pinned_pieces(board, chess.WHITE) == 1


def test_king_pressure_and_back_rank():
    board = chess.Board("4k3/8/8/8/8/5r2/8/4K3 b - - 0 1")
    assert king_pressure(board, chess.WHITE) == 1
    assert king_on_back_rank(board, chess.WHITE)


def test_pawn_concession_isolated_and_doubled():
    # dxe3 creates doubled e-pawns (and an isolated c-pawn) that did not exist before
    before = chess.Board("4k3/8/8/8/4P3/4p3/2PP4/4K3 w - - 0 1")
    move = chess.Move.from_uci("d2e3")
    assert move in before.legal_moves
    after = before.copy(stack=False)
    after.push(move)
    assert pawn_concession(before, move, after)
    # healthy chain pawn move is fine
    before2 = chess.Board("4k3/8/8/8/8/8/2PP4/4K3 w - - 0 1")
    move2 = chess.Move.from_uci("c2c3")
    after2 = before2.copy(stack=False)
    after2.push(move2)
    assert not pawn_concession(before2, move2, after2)
    # pre-existing weakness is not attributed to a later pawn move
    before3 = chess.Board("4k3/8/8/8/8/8/3P4/4K3 w - - 0 1")  # d2 already isolated
    move3 = chess.Move.from_uci("d2d4")
    after3 = before3.copy(stack=False)
    after3.push(move3)  # d4 still isolated, but not *created* here
    assert not pawn_concession(before3, move3, after3)


# ---------------------------------------------------------------- review + caching


STUB_PGN = """[Event "Test"]
[White "testuser"]
[Black "opponent"]
[Result "1-0"]

1. d4 {[%clk 0:10:00]} d5 {[%clk 0:10:00]} 2. Bf4 {[%clk 0:09:59]} Nf6 {[%clk 0:09:58]} 3. e3 {[%clk 0:09:50]} e6 {[%clk 0:09:55]} 1-0
"""


def test_review_game_produces_full_classification_data(config):
    engine = StubEngine()
    # white blunders on move 3 (eval collapses before->after), black fine
    board = chess.Board()
    seq = [board.fen()]
    b = board.copy()
    for san in ["d4", "d5", "Bf4", "Nf6", "e3", "e6"]:
        b.push_san(san)
        seq.append(b.fen())
    engine.eval_by_fen = {seq[0]: 20, seq[1]: 20, seq[2]: 20, seq[3]: 20, seq[4]: 20, seq[5]: -500}
    result = review_game(STUB_PGN, engine, config)
    assert len(result["moves"]) == 6
    assert result["accuracy_white"] is not None
    assert result["accuracy_black"] is not None
    assert set(result["phases"]) == {"opening", "middlegame", "endgame"}
    e3 = [m for m in result["moves"] if m["san"] == "e3"][0]
    assert e3["classification"] in ("mistake", "blunder")
    assert e3["drop"] > 20
    # every move has classification, evals, accuracy
    for m in result["moves"]:
        assert m["classification"] in ("brilliant", "best", "good", "inaccuracy", "mistake", "blunder")
        assert "cp_before" in m and "cp_after" in m and "accuracy" in m


def test_run_analyze_caches_and_resumes(config, db, monkeypatch):
    import sqlite3 as sq

    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, result, time_class, rules)
           VALUES ('u1', '2024/04', 1714500000, ?, 'testuser', 'opponent', '1-0', 'rapid', 'chess')""",
        (STUB_PGN,),
    )
    db.commit()
    engine_holder = {}

    class CountingStub(StubEngine):
        def __init__(self):
            super().__init__("stub-count")
            self.analyzed_games = 0

        def evaluate(self, board, **kw):
            return [{"cp": 20, "mate": None, "pv": []}]

    def fake_engine(*args, **kwargs):
        e = CountingStub()
        engine_holder.setdefault("engines", []).append(e)
        return e

    import chess_analysis.analysis as analysis_mod

    monkeypatch.setattr(analysis_mod, "Engine", fake_engine)
    n1 = run_analyze(db, config, limit=None)
    assert n1 == 1
    # second run: fresh, so nothing re-analyzed (engine may spawn to check version)
    n2 = run_analyze(db, config)
    assert n2 == 0
    assert db.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 1


def test_run_analyze_threshold_change_invalidates(config, db, monkeypatch):
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, result, time_class, rules)
           VALUES ('u2', '2024/04', 1714500000, ?, 'testuser', 'opponent', '1-0', 'rapid', 'chess')""",
        (STUB_PGN,),
    )
    db.commit()

    class Dummy(StubEngine):
        def evaluate(self, board, **kw):
            return [{"cp": 20, "mate": None, "pv": []}]

    import chess_analysis.analysis as analysis_mod

    monkeypatch.setattr(analysis_mod, "Engine", lambda *a, **k: Dummy())

    assert run_analyze(db, config) == 1
    row = db.execute("SELECT signature, input_checksum FROM analysis WHERE game_uuid='u2'").fetchone()
    assert row["signature"] == config.analysis_signature
    # change thresholds -> signature mismatch -> stale -> re-analyzed
    config.thresholds.blunder = 35.0
    assert run_analyze(db, config) == 1


def test_run_analyze_engine_version_change_invalidates(config, db, monkeypatch):
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, result, time_class, rules)
           VALUES ('u5', '2024/04', 1714500000, ?, 'testuser', 'opponent', '1-0', 'rapid', 'chess')""",
        (STUB_PGN,),
    )
    db.commit()

    class StubV1(StubEngine):
        def __init__(self):
            super().__init__("stub-1.0")

        def evaluate(self, board, **kw):
            return [{"cp": 20, "mate": None, "pv": []}]

    class StubV2(StubV1):
        def __init__(self):
            StubEngine.__init__(self, "stub-2.0")

    import chess_analysis.analysis as analysis_mod

    versions = iter([StubV1(), StubV2()])
    monkeypatch.setattr(analysis_mod, "Engine", lambda *a, **k: next(versions))
    assert run_analyze(db, config) == 1  # analyzed with stub-1.0
    # engine "upgraded": version mismatch -> stale -> re-analyzed even though
    # signature and checksum are unchanged
    assert run_analyze(db, config) == 1
    row = db.execute("SELECT engine_version FROM analysis WHERE game_uuid='u5'").fetchone()
    assert row["engine_version"] == "stub-2.0"


def test_run_analyze_bad_game_does_not_kill_batch(config, db, monkeypatch):
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, result, time_class, rules)
           VALUES ('bad', '2024/04', 1714500000, '', 'testuser', 'opponent', '1-0', 'rapid', 'chess')"""
    )
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, result, time_class, rules)
           VALUES ('good', '2024/04', 1714500001, ?, 'testuser', 'opponent', '1-0', 'rapid', 'chess')""",
        (STUB_PGN,),
    )
    db.commit()

    class Dummy(StubEngine):
        def evaluate(self, board, **kw):
            return [{"cp": 20, "mate": None, "pv": []}]

    import chess_analysis.analysis as analysis_mod

    monkeypatch.setattr(analysis_mod, "Engine", lambda *a, **k: Dummy())
    assert run_analyze(db, config) == 1  # only the good game
    assert db.execute("SELECT COUNT(*) FROM analysis WHERE game_uuid='good'").fetchone()[0] == 1


def test_run_analyze_limit_respects_zero(config, db, monkeypatch):
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, result, time_class, rules)
           VALUES ('u3', '2024/04', 1714500000, ?, 'testuser', 'opponent', '1-0', 'rapid', 'chess')""",
        (STUB_PGN,),
    )
    db.commit()
    assert run_analyze(db, config, limit=0) == 0
    assert db.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 0


def _insert_games(conn, uuids):
    for i, uuid in enumerate(uuids):
        conn.execute(
            """INSERT INTO games (uuid, month, end_time, pgn, white, black, result, time_class, rules)
               VALUES (?, '2024/04', ?, ?, 'testuser', 'opponent', '1-0', 'rapid', 'chess')""",
            (uuid, 1714500000 + i, STUB_PGN),
        )
    conn.commit()


def _stub_engine(monkeypatch):
    class Dummy(StubEngine):
        def evaluate(self, board, **kw):
            return [{"cp": 20, "mate": None, "pv": []}]

    import chess_analysis.analysis as analysis_mod

    monkeypatch.setattr(analysis_mod, "Engine", lambda *a, **k: Dummy())


def test_run_analyze_parallel_workers_analyze_all_games(config, db, monkeypatch):
    _insert_games(db, ["p1", "p2", "p3"])
    _stub_engine(monkeypatch)
    assert run_analyze(db, config, workers=2) == 3
    assert db.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 3
    # second parallel run: everything fresh, nothing re-analyzed
    assert run_analyze(db, config, workers=2) == 0


def test_run_analyze_parallel_respects_limit_and_skips_bad_games(config, db, monkeypatch):
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, result, time_class, rules)
           VALUES ('pbad', '2024/04', 1714500000, '', 'testuser', 'opponent', '1-0', 'rapid', 'chess')"""
    )
    _insert_games(db, ["pg1", "pg2", "pg3"])
    _stub_engine(monkeypatch)
    assert run_analyze(db, config, workers=3, limit=2) == 2
    assert db.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM analysis WHERE game_uuid='pbad'").fetchone()[0] == 0


def test_run_analyze_parallel_threshold_change_invalidates(config, db, monkeypatch):
    _insert_games(db, ["pt1", "pt2"])
    _stub_engine(monkeypatch)
    assert run_analyze(db, config, workers=2) == 2
    config.thresholds.blunder = 35.0
    assert run_analyze(db, config, workers=2) == 2
    row = db.execute("SELECT signature FROM analysis WHERE game_uuid='pt1'").fetchone()
    assert row["signature"] == config.analysis_signature


def test_blunders_carry_motif_metadata(config, db, monkeypatch):
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, result, time_class, rules)
           VALUES ('u4', '2024/04', 1714500000, ?, 'testuser', 'opponent', '1-0', 'rapid', 'chess')""",
        (STUB_PGN,),
    )
    db.commit()

    class BlunderStub(StubEngine):
        def evaluate(self, board, **kw):
            # eval collapses after white's 5th... use eval_by_fen on real sequence
            return [{"cp": self.eval_by_fen.get(board.fen(), 20), "mate": None, "pv": []}]

    b = chess.Board()
    seq = [b.fen()]
    for san in ["d4", "d5", "Bf4", "Nf6", "e3", "e6"]:
        b.push_san(san)
        seq.append(b.fen())
    engine = BlunderStub()
    engine.eval_by_fen = {seq[0]: 20, seq[1]: 20, seq[2]: 20, seq[3]: 20, seq[4]: 20, seq[5]: -900}

    monkeypatch.setattr("chess_analysis.analysis.Engine", lambda *a, **k: engine)
    assert run_analyze(db, config) == 1
    row = db.execute("SELECT blunders FROM analysis WHERE game_uuid='u4'").fetchone()
    blunders = json.loads(row["blunders"])
    assert blunders, "the e3 blunder must be recorded"
    b0 = blunders[0]
    assert "meta" in b0
    meta = b0["meta"]
    assert "motifs" in meta and isinstance(meta["motifs"], list) and meta["motifs"]
    assert "material_balance" in meta
    assert "clock_s" in meta
    assert meta["clock_s"] is not None  # %clk annotations were present
