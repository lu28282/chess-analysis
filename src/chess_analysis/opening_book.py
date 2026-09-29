"""Story 6: reference opening book built from the lichess explorer API."""

from __future__ import annotations

import logging
import io
import sqlite3
import time
from pathlib import Path
from typing import Any

import chess
import chess.pgn
import requests

from .config import AppConfig

log = logging.getLogger(__name__)

CARO_ROOT_SAN = ["e4", "c6"]
JOBAVA_SETUP_SAN = ["d4", "Nc3", "Bf4"]


class ExplorerClient:
    """Serial, rate-limited lichess opening-explorer client."""

    MAX_RETRIES = 3

    def __init__(self, endpoint: str, user_agent: str, delay_ms: int, timeout_s: float = 30.0):
        self._endpoint = endpoint.rstrip("/")
        self._delay = delay_ms / 1000.0
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._timeout = timeout_s
        self.request_count = 0

    def top_moves(self, fen: str, top_n: int) -> list[dict] | None:
        """Legal moves at `fen` ([] when unknown, None on persistent failure)."""
        for attempt in range(self.MAX_RETRIES):
            if self._delay and self.request_count:
                time.sleep(self._delay)
            self.request_count += 1
            try:
                resp = self._session.get(
                    self._endpoint,
                    params={"fen": fen, "moves": top_n},
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                log.warning("explorer request failed (%s)", exc)
                continue
            if resp.status_code == 404:
                return []
            if resp.status_code in (429, 500, 502, 503, 504):
                log.warning("explorer returned %d (attempt %d/%d); backing off",
                            resp.status_code, attempt + 1, self.MAX_RETRIES)
                time.sleep(max(self._delay, 2.0))
                continue
            if resp.status_code in (401, 403):
                # the explorer now requires authorization upstream: no point retrying
                log.warning("explorer returned %d (gated); aborting book build", resp.status_code)
                return None
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                log.warning("explorer error %s; aborting book build", exc)
                return None
            try:
                return resp.json().get("moves", [])
            except ValueError:
                log.warning("explorer returned invalid JSON; aborting book build")
                return None
        log.warning("explorer retries exhausted; stopping")
        return None


def _white_san_moves(board: chess.Board) -> list[str]:
    """SAN of White's moves so far (move_stack stores Move objects, not SAN)."""
    sans: list[str] = []
    replay = chess.Board()
    for move in board.move_stack:
        sans.append(replay.san(move))
        replay.push(move)
    return sans[0::2]


def _allowed_expansion(opening: str, board: chess.Board) -> list[str] | None:
    """SAN moves the book must follow at controlled plies (None = free choice).

    jobava_london: White must complete d4/Nc3/Bf4 within its first six plies
    (matching classify_opening); once complete, both sides expand freely.
    caro_kann: 1.e4 c6 forced, then both sides expand freely.
    """
    if opening == "caro_kann":
        if len(board.move_stack) == 0:
            return CARO_ROOT_SAN[:1]
        if len(board.move_stack) == 1:
            return CARO_ROOT_SAN[1:]
        return None
    if opening == "jobava_london":
        # classify_opening requires 1.d4: the book starts there too
        if not len(board.move_stack):
            return ["d4"]
        white_moves = _white_san_moves(board)
        missing = [m for m in JOBAVA_SETUP_SAN if m not in white_moves]
        if len(white_moves) >= 6 or not missing:
            return None
        # also stop if White already left the system
        if any(m not in JOBAVA_SETUP_SAN for m in white_moves):
            return []
        # the setup constrains White's moves only; Black replies freely
        return missing if board.turn == chess.WHITE else None
    raise ValueError(f"unknown opening: {opening}")


def _build_opening_book(conn: sqlite3.Connection, client: ExplorerClient, cfg: AppConfig, opening: str) -> int:
    ex = cfg.explorer
    board = chess.Board()
    queue: list[tuple[chess.Board, int]] = [(board, 0)]
    stored = 0
    while queue and client.request_count < ex.max_positions:
        current, ply = queue.pop(0)
        if ply >= ex.max_ply:
            continue
        forced = _allowed_expansion(opening, current)
        if forced is not None and len(forced) == 0:
            continue  # White left the system: dead branch
        moves = client.top_moves(current.fen(), ex.top_moves)
        if moves is None:
            queue.clear()
            break
        for mv in moves:
            try:
                move = chess.Move.from_uci(mv["uci"])
            except ValueError:
                continue
            if not current.is_legal(move):
                continue
            san = current.san(move)
            if forced is not None and san not in forced:
                continue
            total = int(mv.get("white", 0)) + int(mv.get("draws", 0)) + int(mv.get("black", 0))
            if total < ex.min_games:
                continue
            conn.execute(
                """
                INSERT INTO opening_book (opening, fen, move_uci, move_san, ply,
                                          white, draw, black, total, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'lichess')
                ON CONFLICT(opening, fen, move_uci) DO UPDATE SET
                    move_san = excluded.move_san,
                    ply = excluded.ply,
                    white = excluded.white,
                    draw = excluded.draw,
                    black = excluded.black,
                    total = excluded.total
                """,
                (
                    opening,
                    current.fen(),
                    mv["uci"],
                    san,
                    ply + 1,
                    int(mv.get("white", 0)),
                    int(mv.get("draws", 0)),
                    int(mv.get("black", 0)),
                    total,
                ),
            )
            stored += 1
            child = current.copy()  # keep the move stack: _allowed_expansion needs it
            child.push(move)
            queue.append((child, ply + 1))
    return stored


def build_book(conn: sqlite3.Connection, cfg: AppConfig) -> int:
    """Build/refresh the reference book for both openings (one-time, then offline)."""
    conn.execute("DELETE FROM opening_book")
    client = ExplorerClient(cfg.explorer.endpoint, cfg.user_agent, cfg.explorer.delay_ms)
    total = 0
    for opening in ("jobava_london", "caro_kann"):
        client.request_count = 0  # per-opening request budget
        n = _build_opening_book(conn, client, cfg, opening)
        log.info("book %s: %d moves stored (%d explorer requests)", opening, n, client.request_count)
        if n == 0 and client.request_count == 0:
            log.warning("book %s: no explorer requests made (API unreachable?)", opening)
        conn.commit()  # each opening's book is committed atomically
        total += n
    return total


# --------------------------------------------------------------- local fallback
# The lichess explorer API now requires authorization; building the book from
# the games already in the DB keeps the pipeline fully offline. Book quality
# is bounded by the local corpus (no true "reference" data) — reports note it.

LOCAL_MIN_GAMES = 5


def build_local_book(conn: sqlite3.Connection, cfg: AppConfig) -> int:
    """Build the book from local games (offline fallback)."""
    conn.execute("DELETE FROM opening_book")
    max_ply = cfg.explorer.max_ply
    total_stored = 0
    for opening in ("jobava_london", "caro_kann"):
        tally: dict[tuple[str, str], dict[str, int]] = {}
        rows = conn.execute(
            "SELECT pgn, result_parsed FROM games WHERE opening_class = ? AND parse_error IS NULL "
            "AND rules = 'chess'",
            (opening,),
        ).fetchall()
        for r in rows:
            game = chess.pgn.read_game(io.StringIO(r["pgn"]))
            if game is None:
                continue
            board = game.board()
            for ply, move in enumerate(game.mainline_moves(), start=1):
                if ply > max_ply:
                    break
                forced = _allowed_expansion(opening, board)
                if forced is not None:
                    san = board.san(move)
                    if san not in forced:
                        break  # game left the (constrained) system — stop walking
                key = (board.fen(), move.uci())
                slot = tally.setdefault(key, {"san": board.san(move), "ply": ply,
                                              "white": 0, "draw": 0, "black": 0})
                if r["result_parsed"] == "1-0":
                    slot["white"] += 1
                elif r["result_parsed"] == "1/2-1/2":
                    slot["draw"] += 1
                elif r["result_parsed"] == "0-1":
                    slot["black"] += 1
                # unknown/abandoned (*) results count towards total only
                board.push(move)
        stored = 0
        for (parent_fen, uci), slot in tally.items():
            t = slot["white"] + slot["draw"] + slot["black"]
            if t < LOCAL_MIN_GAMES:
                continue
            conn.execute(
                """
                INSERT INTO opening_book (opening, fen, move_uci, move_san, ply,
                                          white, draw, black, total, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'local')
                ON CONFLICT(opening, fen, move_uci) DO UPDATE SET
                    move_san = excluded.move_san,
                    ply = excluded.ply,
                    white = excluded.white,
                    draw = excluded.draw,
                    black = excluded.black,
                    total = excluded.total
                """,
                (opening, parent_fen, uci, slot["san"], slot["ply"],
                 slot["white"], slot["draw"], slot["black"], t),
            )
            stored += 1
        log.info("local book %s: %d moves stored from %d games", opening, stored, len(rows))
        total_stored += stored
    conn.commit()
    return total_stored


def load_book(conn: sqlite3.Connection, opening: str) -> dict[str, dict[str, dict[str, Any]]]:
    """book[parent_fen][move_uci] = {san, ply, white, draw, black, total}."""
    book: dict[str, dict[str, dict[str, Any]]] = {}
    for row in conn.execute("SELECT * FROM opening_book WHERE opening = ?", (opening,)):
        book.setdefault(row["fen"], {})[row["move_uci"]] = dict(row)
    return book


def walk_game_through_book(
    moves_uci: list[str], book: dict[str, dict[str, dict[str, Any]]], max_ply: int
) -> tuple[int | None, str | None, list[str]]:
    """Walk a game's UCI moves through the book.

    Returns (off_ply, off_side, visited_child_fens): the ply where the game
    first leaves the book, the side that left, and all in-book child positions.
    An empty book never matches, so no game is marked as leaving it.
    """
    if not book:
        return None, None, []
    board = chess.Board()
    visited: list[str] = []
    for ply, uci in enumerate(moves_uci, start=1):
        if ply > max_ply:
            break
        node = book.get(board.fen())
        if node is None or uci not in node:
            side = "white" if board.turn == chess.WHITE else "black"
            return ply, side, visited
        visited.append(_child_fen(board.fen(), uci))
        board.push(chess.Move.from_uci(uci))
    return None, None, visited


def _child_fen(parent_fen: str, uci: str) -> str:
    board = chess.Board(parent_fen)
    board.push(chess.Move.from_uci(uci))
    return board.fen()


def book_gap_min_total(source: str, explorer_min_games: int) -> int:
    """Popularity floor for repertoire-gap edges, scaled to the book's origin."""
    return LOCAL_MIN_GAMES * 2 if source == "local" else explorer_min_games * 5


def mainline_san(conn: sqlite3.Connection, opening: str, max_ply: int = 12) -> list[str]:
    """Reference mainline: always follow the most-played book move."""
    book = load_book(conn, opening)
    board = chess.Board()
    sans: list[str] = []
    while len(sans) < max_ply:
        node = book.get(board.fen())
        if not node:
            break
        best = max(node.values(), key=lambda e: e["total"])
        sans.append(best["move_san"])
        board.push(chess.Move.from_uci(best["move_uci"]))
    return sans
