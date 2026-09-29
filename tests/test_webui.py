"""Web UI tests: job serialization and the HTTP API."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def config_path(config, tmp_path):
    """TOML file matching the conftest AppConfig (same tmp_path instance)."""
    toml = f"""username = "testuser"
db_path = "{config.db_path}"
reports_dir = "{config.reports_dir}"
engine_path = "{config.engine_path}"
user_agent = "chess-analysis-test"
eval_ms = 100
"""
    p = tmp_path / "webui.toml"
    p.write_text(toml, encoding="utf-8")
    return p


@pytest.fixture()
def server(config, config_path, tmp_path):
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    from chess_analysis.webui import JobManager, make_handler

    manager = JobManager(config, config_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager, config))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, manager
    httpd.shutdown()


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"} if body else {}
    conn.request(method, path, body=json.dumps(body) if body else None, headers=headers)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload


def test_index_served(server):
    httpd, _ = server
    status, body = _request(httpd.server_port, "GET", "/")
    assert status == 200
    assert b"chess-analysis" in body
    assert b"/api/run/" in body  # the UI posts its actions


def test_status_reports_db_and_steps(server):
    httpd, _ = server
    status, body = _request(httpd.server_port, "GET", "/api/status")
    assert status == 200
    data = json.loads(body)
    assert data["job"] is None
    assert data["stats"]["games"] == 0
    assert "fetch" in data["steps"]
    assert data["reports"] == []


def test_unknown_step_rejected(server):
    httpd, _ = server
    status, body = _request(httpd.server_port, "POST", "/api/run/rm-rf", {})
    assert status == 404 or status == 409
    data = json.loads(body)
    assert data["ok"] is False


def test_run_report_mistakes_job(server, config):
    httpd, manager = server
    status, body = _request(httpd.server_port, "POST", "/api/run/report-mistakes")
    assert status == 200
    assert json.loads(body)["ok"] is True

    # exactly one job at a time
    status, body = _request(httpd.server_port, "POST", "/api/run/fetch")
    assert status == 409
    assert "still running" in json.loads(body)["message"]

    # wait for the job to finish
    deadline = time.time() + 30
    while time.time() < deadline:
        status, body = _request(httpd.server_port, "GET", "/api/status")
        data = json.loads(body)
        if data["job"] and data["job"]["done"]:
            break
        time.sleep(0.3)
    assert data["job"]["step"] == "report-mistakes"
    assert data["job"]["returncode"] == 0

    # report file now exists and is served
    assert (config.reports_dir / "mistake-patterns.html").is_file()
    status, body = _request(httpd.server_port, "GET", "/reports/mistake-patterns.html")
    assert status == 200
    assert b"<!DOCTYPE html>" in body

    # path traversal is contained to the reports dir
    status, _ = _request(httpd.server_port, "GET", "/reports/..%2fconfig%2fchess-analysis.toml")
    assert status == 404


def test_reports_listing(server, config):
    (config.reports_dir / "x.html").write_text("<p>hi</p>", encoding="utf-8")
    httpd, _ = server
    status, body = _request(httpd.server_port, "GET", "/api/status")
    data = json.loads(body)
    assert any(r["name"] == "x.html" for r in data["reports"])


# ---------------------------------------------------------------- game viewer


def _insert_analyzed_game(db, uuid, *, white="testuser", black="opponent", my_color="white",
                          blunders=None, moves=None, end_time=1714500000):
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, white_rating, black_rating,
                              result, time_class, rules, my_color, my_result, opponent)
           VALUES (?, '2024/05', ?, ?, ?, ?, 1500, 1400, '1-0', 'rapid', 'chess', ?, 'win', ?)""",
        (uuid, end_time, '[Link "https://www.chess.com/game/live/123"]\n\n1. d4 d5 2. Bf4 Nf6 1-0',
         white, black, my_color, black),
    )
    db.execute(
        """INSERT INTO analysis (game_uuid, engine_version, signature, input_checksum, analyzed_at,
                                 accuracy_white, accuracy_black, phases, moves, blunders)
           VALUES (?, 'sf-test', 'sig', 'chk', '2024-05-01T00:00:00+00:00', 80.0, 75.0, '{}', ?, ?)""",
        (uuid, json.dumps(moves or []), json.dumps(blunders or [])),
    )
    db.commit()


def test_viewer_page_served(server):
    httpd, _ = server
    status, body = _request(httpd.server_port, "GET", "/viewer")
    assert status == 200
    assert b"Game viewer" in body
    assert b"ChessJS" in body  # vendored chess.js inlined


def test_api_games_lists_analyzed_games_with_mistake_counts(server, db):
    _insert_analyzed_game(
        db, "v1",
        blunders=[{"side": "white", "ply": 3, "classification": "blunder"},
                  {"side": "black", "ply": 4, "classification": "mistake"}],  # not mine -> ignored
    )
    _insert_analyzed_game(db, "v2", my_color="black", end_time=1714600000)
    db.execute("INSERT INTO games (uuid, month, pgn) VALUES ('unanalyzed', '2024/05', '')")
    db.commit()
    httpd, _ = server
    status, body = _request(httpd.server_port, "GET", "/api/games")
    assert status == 200
    data = json.loads(body)
    assert [g["uuid"] for g in data] == ["v2", "v1"]  # newest first, unanalyzed excluded
    v1 = next(g for g in data if g["uuid"] == "v1")
    assert v1["mistakes"] == 1
    assert v1["opponent"] == "opponent"
    assert v1["color"] == "white"
    assert v1["accuracy"] == 80.0
    v2 = next(g for g in data if g["uuid"] == "v2")
    assert v2["mistakes"] == 0 and v2["color"] == "black"


def test_api_game_returns_moves_and_meta(server, db):
    moves = [
        {"ply": 1, "side": "white", "san": "d4", "uci": "d2d4", "classification": "best",
         "drop": 0.0, "cp_before": 20, "cp_after": 20, "winp_before": 50.0, "winp_after": 50.4,
         "accuracy": 97.0, "clock_s": 300},
        {"ply": 2, "side": "black", "san": "d5", "uci": "d7d5", "classification": "best",
         "drop": 0.0, "cp_before": 20, "cp_after": 20, "winp_before": 50.4, "winp_after": 50.4,
         "accuracy": 97.0, "clock_s": 299},
    ]
    _insert_analyzed_game(db, "v3", moves=moves)
    httpd, _ = server
    status, body = _request(httpd.server_port, "GET", "/api/game/v3")
    assert status == 200
    data = json.loads(body)
    assert data["white"] == "testuser"
    assert data["color"] == "white"
    assert data["link"] == "https://www.chess.com/game/live/123"
    assert len(data["moves"]) == 2
    assert data["moves"][0]["uci"] == "d2d4"
    # path traversal / garbage uuids are rejected
    status, _ = _request(httpd.server_port, "GET", "/api/game/..%2fconfig")
    assert status == 404
    status, _ = _request(httpd.server_port, "GET", "/api/game/nope")
    assert status == 404


# ---------------------------------------------------------------- ask stockfish


class StubConsultant:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def best_moves(self, fen, multipv):
        self.calls.append((fen, multipv))
        return self.result

    def close(self):
        pass


@pytest.fixture()
def server_sf(config, config_path, tmp_path):
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    from chess_analysis.webui import JobManager, make_handler

    stub = StubConsultant({
        "engine": "stub-1.0",
        "game_over": False,
        "lines": [
            {"uci": "g1f3", "san": "Nf3", "cp": 30, "mate": None, "winp": 51.1,
             "pv_san": ["Nf3", "Nf6"]},
        ],
    })
    manager = JobManager(config, config_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager, config, stub))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, stub
    httpd.shutdown()


def test_api_best_returns_engine_lines(server_sf):
    httpd, stub = server_sf
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    status, body = _request(httpd.server_port, "GET", f"/api/best?fen={fen.replace(' ', '%20')}")
    assert status == 200
    data = json.loads(body)
    assert data["engine"] == "stub-1.0"
    assert data["lines"][0]["san"] == "Nf3"
    assert data["lines"][0]["pv_san"] == ["Nf3", "Nf6"]
    assert stub.calls[0][1] == 3  # default multipv

    # multipv is clamped to 1..5
    status, body = _request(httpd.server_port, "GET", f"/api/best?fen={fen.replace(' ', '%20')}&multipv=99")
    assert status == 200
    assert stub.calls[-1][1] == 5


def test_api_best_rejects_missing_fen(server_sf):
    httpd, _ = server_sf
    status, body = _request(httpd.server_port, "GET", "/api/best")
    assert status == 400
    assert json.loads(body)["error"] == "missing fen"


def test_engine_consultant_validates_and_formats(config):
    from chess_analysis.webui import EngineConsultant

    class StubEngine:
        name = "stub-sf"

        def evaluate(self, board, *, eval_ms, eval_depth, multipv):
            return [{"cp": 55, "mate": None, "pv": ["g1f3", "g8f6", "e2e4"]}][:multipv]

    ec = EngineConsultant(config, engine_factory=lambda *a, **k: StubEngine())

    # invalid fen -> None (no engine spawn)
    assert ec.best_moves("not a fen", 3) is None
    # game over (checkmate) -> game_over, no engine spawn
    mate_fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    assert ec.best_moves(mate_fen, 3)["game_over"] is True
    # valid fen -> lines with SAN-converted PV
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    result = ec.best_moves(start, 2)
    line = result["lines"][0]
    assert result["engine"] == "stub-sf"
    assert line["san"] == "Nf3" and line["uci"] == "g1f3"
    assert line["pv_san"] == ["Nf3", "Nf6", "e4"]
    assert line["winp"] > 50  # +0.55 for white
    ec.close()


def test_engine_consultant_caches_answers(config, db):
    """Second question about the same position is served from `engine_advice`:
    the engine is only asked once per (position, engine, budget)."""
    from chess_analysis.webui import EngineConsultant

    calls = []

    class StubEngine:
        name = "stub-sf"

        def evaluate(self, board, *, eval_ms, eval_depth, multipv):
            calls.append((board.fen(), eval_ms, multipv))
            return [{"cp": 10, "mate": None, "pv": ["g1f3", "g8f6"]}]

    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    ec = EngineConsultant(config, engine_factory=lambda *a, **k: StubEngine())
    r1 = ec.best_moves(start, 3)
    assert len(calls) == 1
    # same question again -> cache hit, engine untouched
    assert ec.best_moves(start, 3) == r1
    assert len(calls) == 1
    # the answer is persisted with its engine + budget signature
    row = db.execute("SELECT fen, engine_version, multipv FROM engine_advice").fetchone()
    assert row["fen"] == start and row["engine_version"] == "stub-sf" and row["multipv"] == 3
    # a different position is computed anew
    other = "rnbqkbnr/pppp1ppp/8/4p2Q/4P3/8/PPPP1PPP/RNB1KBNR b KQkq - 2 3"
    ec.best_moves(other, 3)
    assert len(calls) == 2
    # a budget change invalidates the cache
    config.eval_ms = 250
    ec.best_moves(start, 3)
    assert len(calls) == 3
    ec.close()
    # an engine upgrade invalidates the cache even with the same budget
    class StubEngineV2(StubEngine):
        name = "stub-sf-2"

    ec2 = EngineConsultant(config, engine_factory=lambda *a, **k: StubEngineV2())
    assert ec2.best_moves(start, 3)["engine"] == "stub-sf-2"
    assert len(calls) == 4
    ec2.close()


def test_job_step_args(config, config_path, monkeypatch):
    """The opening-book button builds offline from local games (not the network explorer)."""
    from chess_analysis.webui import JobManager, STEP_ARGS

    assert STEP_ARGS["build-opening-book"] == ["--source", "local"]
    manager = JobManager(config, config_path)
    commands = []

    class FakeProc:
        def __init__(self):
            self._rc = None

        def poll(self):
            return self._rc

    def fake_popen(cmd, **kwargs):
        commands.append(cmd)
        proc = FakeProc()
        proc._rc = 0
        return proc

    monkeypatch.setattr("chess_analysis.webui.subprocess.Popen", fake_popen)
    assert manager.start("build-opening-book")[0] is True
    assert commands[0] == [sys.executable, "-m", "chess_analysis", "build-opening-book", "--source", "local"]
    assert manager.start("report-mistakes")[0] is True
    assert commands[1] == [sys.executable, "-m", "chess_analysis", "report-mistakes"]  # no extra args


# ---------------------------------------------------------------- mistake insights


def _insert_insight_game(db, uuid):
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, white_rating, black_rating,
                              result, time_class, rules, my_color, my_result, opponent)
           VALUES (?, '2024/05', 1714500000,
                   '[Link "https://www.chess.com/game/live/1"]

1. d4 {[%clk 0:04:00]} d5 {[%clk 0:03:59]} 2. Bf4 {[%clk 0:03:40]} Nf6 {[%clk 0:03:30]} 1-0',
                   'testuser', 'opponent', 1500, 1400, '1-0', 'rapid', 'chess', 'white', 'win', 'opponent')""",
        (uuid,),
    )
    moves = [{"side": s, "ply": p} for p, s in enumerate(["white", "black", "white", "black"], start=1)]
    blunders = [
        {"side": "white", "ply": 3, "san": "Bf4", "uci": "c1f4", "classification": "mistake", "drop": 25.0,
         "meta": {"motifs": ["hanging_piece"], "clock_s": 220, "position_type": "open", "material_balance": 0}},
        {"side": "black", "ply": 4, "san": "Nf6", "uci": "g8f6", "classification": "blunder", "drop": 40.0,
         "meta": {"motifs": ["king_safety"], "clock_s": 210, "position_type": "closed", "material_balance": -1}},
    ]
    db.execute(
        """INSERT INTO analysis (game_uuid, engine_version, signature, input_checksum, analyzed_at,
                                 accuracy_white, accuracy_black, phases, moves, blunders)
           VALUES (?, 'sf-test', 'sig', 'chk', '2024-05-01T00:00:00+00:00', 80.0, 60.0, '{}', ?, ?)""",
        (uuid, json.dumps(moves), json.dumps(blunders)),
    )
    db.commit()


def test_insights_page_served(server):
    httpd, _ = server
    status, body = _request(httpd.server_port, "GET", "/insights")
    assert status == 200
    assert b"Mistake insights" in body
    assert b"Chart" in b"" or b"chart" in body  # vendored Chart.js inlined


def test_api_mistakes_returns_visual_payload(server, db):
    import chess as pychess

    _insert_insight_game(db, "m1")
    httpd, _ = server
    status, body = _request(httpd.server_port, "GET", "/api/mistakes")
    assert status == 200
    data = json.loads(body)
    assert data["username"] == "testuser"
    assert data["labels"] == {}
    assert data["moves_by_color_tc"] == {"white|rapid": 2}  # my (white) moves only
    assert len(data["mistakes"]) == 1  # black's blunder excluded
    m = data["mistakes"][0]
    assert m["side"] == "white" and m["ply"] == 3
    assert m["motifs"] == ["hanging_piece"]
    assert m["uci"] == "c1f4"
    # fen_before = position before move 3 (after 1. d4 d5), replayable
    board = pychess.Board(m["fen_before"])
    assert board.fen().split()[:2] == ["rnbqkbnr/ppp1pppp/8/3p4/3P4/8/PPP1PPPP/RNBQKBNR", "w"]
    assert len(board.piece_map()) == 32  # full starting material after 1. d4 d5


def test_theme_label_roundtrip(server, db):
    _insert_insight_game(db, "m2")
    key = '["hanging_piece", "open", "opening", "ahead"]'
    httpd, _ = server
    # save
    status, body = _request(httpd.server_port, "POST", "/api/theme-label", {"key": key, "label": "my pattern"})
    assert status == 200
    assert json.loads(body)["ok"] is True
    status, body = _request(httpd.server_port, "GET", "/api/mistakes")
    assert json.loads(body)["labels"] == {key: "my pattern"}
    # clear with an empty label
    status, body = _request(httpd.server_port, "POST", "/api/theme-label", {"key": key, "label": ""})
    assert status == 200
    status, body = _request(httpd.server_port, "GET", "/api/mistakes")
    assert json.loads(body)["labels"] == {}
    # validation: missing key -> 400
    status, body = _request(httpd.server_port, "POST", "/api/theme-label", {"label": "x"})
    assert status == 400
    # invalid JSON body -> 400
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
    conn.request("POST", "/api/theme-label", body=b"not json", headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 400
    conn.close()


# ---------------------------------------------------------------- opening blunders


def _insert_opening_game(db, uuid):
    """Analyzed game with opening context; blunders inside and outside the
    opening phase, from both colors."""
    db.execute(
        """INSERT INTO games (uuid, month, end_time, pgn, white, black, white_rating, black_rating,
                              result, time_class, rules, my_color, my_result, opponent,
                              opening_code, opening_name, opening_class, variation_key)
           VALUES (?, '2024/05', 1714500000,
                   '[Link "https://www.chess.com/game/live/1"]

1. d4 {[%clk 0:04:00]} d5 {[%clk 0:03:59]} 2. Bf4 {[%clk 0:03:40]} Nf6 {[%clk 0:03:30]} 1-0',
                   'testuser', 'opponent', 1500, 1400, '1-0', 'rapid', 'chess', 'white', 'win', 'opponent',
                   'D01', 'London System', 'jobava_london', 'd2d4-d7d5')""",
        (uuid,),
    )
    moves = [{"side": s, "ply": p} for p, s in enumerate(["white", "black"] * 2, start=1)]
    blunders = [
        {"side": "white", "ply": 3, "san": "Bf4", "uci": "c1f4", "classification": "blunder", "drop": 45.0,
         "meta": {"motifs": ["hanging_piece"], "clock_s": 220, "position_type": "open", "material_balance": 0}},
        {"side": "white", "ply": 35, "san": "h4", "uci": "h2h4", "classification": "mistake", "drop": 25.0,
         "meta": {"motifs": ["king_safety"], "clock_s": 100, "position_type": "open", "material_balance": 0}},
        {"side": "black", "ply": 4, "san": "Nf6", "uci": "g8f6", "classification": "blunder", "drop": 40.0,
         "meta": {"motifs": ["king_safety"], "clock_s": 210, "position_type": "closed", "material_balance": -1}},
    ]
    db.execute(
        """INSERT INTO analysis (game_uuid, engine_version, signature, input_checksum, analyzed_at,
                                 accuracy_white, accuracy_black, phases, moves, blunders)
           VALUES (?, 'sf-test', 'sig', 'chk', '2024-05-01T00:00:00+00:00', 80.0, 60.0, '{}', ?, ?)""",
        (uuid, json.dumps(moves), json.dumps(blunders)),
    )
    db.commit()


def test_openings_page_served(server):
    httpd, _ = server
    status, body = _request(httpd.server_port, "GET", "/openings")
    assert status == 200
    assert b"Opening blunders" in body
    assert b"ChessJS" in body  # vendored chess.js inlined for practice mode


def test_api_opening_mistakes_filters_and_enriches(server, db):
    _insert_opening_game(db, "o1")
    httpd, _ = server
    status, body = _request(httpd.server_port, "GET", "/api/opening-mistakes")
    assert status == 200
    data = json.loads(body)
    mistakes = data["mistakes"]
    # ply 35 is not opening phase, ply 4 is the opponent's -> only ply 3 remains
    assert len(mistakes) == 1
    m = mistakes[0]
    assert m["ply"] == 3 and m["side"] == "white"
    assert m["opening_code"] == "D01"
    assert m["opening_name"] == "London System"
    assert m["opening_class"] == "jobava_london"
    assert m["variation_key"] == "d2d4-d7d5"
    assert m["fen_before"]  # replayable position before the mistake
    # the plain /api/mistakes payload is unchanged: all my phases, no opening fields
    status, body = _request(httpd.server_port, "GET", "/api/mistakes")
    data2 = json.loads(body)
    assert sorted(x["ply"] for x in data2["mistakes"]) == [3, 35]
    assert "opening_name" not in data2["mistakes"][0]
