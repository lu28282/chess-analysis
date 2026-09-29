"""Stockfish game review: per-move classification, accuracy, blunder metadata, caching."""

from __future__ import annotations

import io
import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

import chess
import chess.pgn

from .config import AppConfig
from .db import connect
from .engine import Engine
from .ingestion import extract_clocks, pgn_checksum

log = logging.getLogger(__name__)

WINP_K = 0.00368208  # chess.com logistic constant
E = 2.718281828459045
PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
KING_VALUE = 100

OPENING_PLIES = 20
MIDDLEGAME_PLIES = 60
MIN_PHASE_MOVES = 4
TIME_TROUBLE_S = 30.0
ENDGAME_MATERIAL = 13  # combined non-pawn material points below which it's an endgame
KING_PRESSURE = 2  # enemy pieces near king to tag king_safety
OPEN_FILES_OPEN = 4  # open files for 'open position'


def win_percent(cp: float) -> float:
    """White-POV centipawns -> white win% (chess.com logistic)."""
    return 50.0 + 50.0 * (2.0 / (1.0 + pow(E, -WINP_K * cp)) - 1.0)


def drop_for_mover(winp_before: float, winp_after: float, side: str) -> float:
    """Win% drop from the mover's point of view (never negative)."""
    before = winp_before if side == "white" else 100.0 - winp_before
    after = winp_after if side == "white" else 100.0 - winp_after
    return max(0.0, before - after)


def classify(drop: float, played_best: bool, sacrifice: bool, after_cp_mover: float, t: Any) -> str:
    """chess.com-style classification from the mover's win% drop.

    Bands are bounded below by the configured thresholds (win% points):
    blunder >= t.blunder, mistake >= t.mistake, inaccuracy >= t.inaccuracy,
    good >= t.good, best otherwise (near-perfect or engine-best).
    Brilliancy: an engine-best *sacrifice* that still leaves the mover
    at least t.brilliant pawns ahead.
    """
    if played_best and sacrifice and after_cp_mover >= t.brilliant * 100:
        return "brilliant"
    if drop >= t.blunder:
        return "blunder"
    if drop >= t.mistake:
        return "mistake"
    if drop >= t.inaccuracy:
        return "inaccuracy"
    if drop >= t.good:
        return "good"
    if played_best or drop <= t.best:
        return "best"
    return "good"


def move_accuracy(drop: float) -> float:
    """Lichess-style per-move accuracy from win% drop, clamped to [0, 100]."""
    acc = 103.1668 * pow(E, -0.04375 * drop) - 3.1669
    return max(0.0, min(100.0, acc))


def phase_of(ply: int) -> str:
    if ply <= OPENING_PLIES:
        return "opening"
    if ply <= MIDDLEGAME_PLIES:
        return "middlegame"
    return "endgame"


# ---------------------------------------------------------------- motifs


def material_balance(board: chess.Board) -> int:
    """Material in pawns, white POV."""
    total = 0
    for piece_type, value in PIECE_VALUES.items():
        total += value * len(board.pieces(piece_type, chess.WHITE))
        total -= value * len(board.pieces(piece_type, chess.BLACK))
    return total


def non_pawn_material(board: chess.Board) -> int:
    total = 0
    for piece_type, value in PIECE_VALUES.items():
        if piece_type == chess.PAWN:
            continue
        total += value * len(board.pieces(piece_type, chess.WHITE))
        total += value * len(board.pieces(piece_type, chess.BLACK))
    return total


def is_sacrifice(board_before: chess.Board, move: chess.Move) -> bool:
    """The played move gives up material (1-ply SEE approximation): the moved
    piece lands on a square where a strictly cheaper enemy piece can take it,
    and the captured piece (if any) does not compensate for the mover's value."""
    mover = board_before.piece_at(move.from_square)
    if mover is None:
        return False
    mover_val = PIECE_VALUES.get(mover.piece_type, 0)
    victim = board_before.piece_at(move.to_square)
    victim_val = PIECE_VALUES.get(victim.piece_type, 0) if victim else 0
    if victim_val >= mover_val:
        return False  # a fair-or-winning capture is never a sacrifice
    board_after = board_before.copy(stack=False)
    board_after.push(move)
    attackers = board_after.attackers(not mover.color, move.to_square)
    if not attackers:
        return False
    cheapest_attacker = min(
        PIECE_VALUES.get(board_after.piece_at(sq).piece_type, 0) for sq in attackers
    )
    # A cheaper enemy piece can win the mover: net loss = mover_val - victim_val.
    return cheapest_attacker < mover_val


def hanging_pieces(board: chess.Board, side: bool) -> list[str]:
    """Squares of side's pieces attacked more often than defended."""
    out = []
    for piece_type in PIECE_VALUES:
        for sq in board.pieces(piece_type, side):
            if len(board.attackers(not side, sq)) > len(board.attackers(side, sq)):
                out.append(chess.square_name(sq))
    return out


def _moved_piece_fork_count(board: chess.Board, to_square: int, mover: bool) -> int:
    """How many valuable enemy pieces the piece on `to_square` attacks."""
    count = 0
    for sq in board.attacks(to_square):
        piece = board.piece_at(sq)
        if piece and piece.color != mover:
            value = KING_VALUE if piece.piece_type == chess.KING else PIECE_VALUES.get(piece.piece_type, 0)
            if value >= 3:
                count += 1
    return count


def move_is_fork(board_before: chess.Board, move: chess.Move) -> bool:
    """Does `move` attack 2+ valuable enemy pieces with the moved piece?"""
    mover = board_before.turn
    board = board_before.copy(stack=False)
    if not board.is_legal(move):
        return False
    board.push(move)
    return _moved_piece_fork_count(board, move.to_square, mover) >= 2


def side_occupied(board: chess.Board, side: bool) -> chess.SquareSet:
    """Squares occupied by `side` (occupied_co is an int bitmask in python-chess)."""
    return chess.SquareSet(board.occupied_co[side])


def pinned_pieces(board: chess.Board, side: bool) -> int:
    """Count of `side`'s pieces currently pinned by the opponent."""
    return sum(1 for sq in side_occupied(board, side) if board.is_pinned(side, sq))


def king_castled(board: chess.Board, side: bool) -> bool:
    """King has left its home square (castled or otherwise developed)."""
    king = board.king(side)
    home = chess.E1 if side == chess.WHITE else chess.E8
    return king is not None and king != home


def king_on_back_rank(board: chess.Board, side: bool) -> bool:
    """King still on rank 1/8 (never castled)."""
    king = board.king(side)
    if king is None:
        return False
    return chess.square_rank(king) == (0 if side == chess.WHITE else 7)


def king_pressure(board: chess.Board, side: bool) -> int:
    """Enemy non-pawn pieces within 2 squares of `side`'s king."""
    king = board.king(side)
    if king is None:
        return 0
    return sum(
        1
        for sq in side_occupied(board, not side)
        if board.piece_type_at(sq) != chess.PAWN and chess.square_distance(sq, king) <= 2
    )


def count_open_files(board: chess.Board) -> int:
    return sum(
        1
        for file in range(8)
        if not any(
            (p := board.piece_at(chess.square(file, rank))) and p.piece_type == chess.PAWN
            for rank in range(8)
        )
    )


def _pawn_weakness_present(board: chess.Board, side: bool) -> bool:
    """Does `side` currently have any doubled or isolated pawn?"""
    pawns_per_file = [0] * 8
    for sq in board.pieces(chess.PAWN, side):
        pawns_per_file[chess.square_file(sq)] += 1
    if any(n >= 2 for n in pawns_per_file):
        return True
    for file in range(8):
        if pawns_per_file[file] == 1:
            left = pawns_per_file[file - 1] if file > 0 else 0
            right = pawns_per_file[file + 1] if file < 7 else 0
            if left == 0 and right == 0:
                return True
    return False


def pawn_concession(board_before: chess.Board, move: chess.Move, board_after: chess.Board) -> bool:
    """The played pawn move *created* a doubled or isolated pawn that was not
    there before (weaknesses that predate the move are not attributed to it)."""
    if board_before.piece_type_at(move.from_square) != chess.PAWN:
        return False
    side = board_before.turn
    return not _pawn_weakness_present(board_before, side) and _pawn_weakness_present(board_after, side)


def skewer_available(board: chess.Board, victim_side: bool) -> bool:
    """Can the opponent x-ray `victim_side`: a slider aims through exactly one
    cheap piece of `victim_side` at a more valuable piece behind it?"""
    attacker_side = not victim_side
    for slider_sq in side_occupied(board, attacker_side):
        piece = board.piece_at(slider_sq)
        if not piece or piece.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
            continue
        for back_sq in side_occupied(board, victim_side):
            dist = chess.square_distance(slider_sq, back_sq)
            if dist < 2:
                continue
            ray = chess.BB_RAYS[slider_sq][back_sq]
            if not ray & chess.BB_SQUARES[back_sq]:
                continue  # not on one slider line
            back_piece = board.piece_at(back_sq)
            back_val = (
                KING_VALUE if back_piece.piece_type == chess.KING else PIECE_VALUES.get(back_piece.piece_type, 0)
            )
            blockers = [
                sq
                for sq in chess.SquareSet(ray)
                if sq != slider_sq
                and chess.square_distance(slider_sq, sq) < dist
                and board.piece_at(sq) is not None
            ]
            if len(blockers) != 1:
                continue
            blocker_piece = board.piece_at(blockers[0])
            if (
                blocker_piece.color == victim_side
                and PIECE_VALUES.get(blocker_piece.piece_type, 0) < back_val
            ):
                return True
    return False


def motifs_for(
    board_before: chess.Board,
    move: chess.Move,
    board_after: chess.Board,
    engine_best_uci: str | None,
    clock_s: float | None,
) -> dict:
    """Motif + position metadata for one classified mistake/blunder (mover POV)."""
    side = board_before.turn
    motifs: list[str] = []
    if hanging_pieces(board_after, side):
        motifs.append("hanging_piece")
    if engine_best_uci:
        try:
            best = chess.Move.from_uci(engine_best_uci)
            if move_is_fork(board_before, best):
                motifs.append("missed_fork")
        except ValueError:
            pass
    if pinned_pieces(board_after, side) > pinned_pieces(board_before, side):
        motifs.append("pin_created")
    if skewer_available(board_after, side):
        motifs.append("skewer_available")
    if any(move_is_fork(board_after, m) for m in board_after.legal_moves):
        motifs.append("fork_available_opponent")
    if king_pressure(board_after, side) >= KING_PRESSURE:
        motifs.append("king_safety")
    if king_on_back_rank(board_after, side) and king_pressure(board_after, side) >= 1:
        motifs.append("back_rank")
    if pawn_concession(board_before, move, board_after):
        motifs.append("pawn_concession")
    if non_pawn_material(board_after) <= ENDGAME_MATERIAL:
        motifs.append("endgame_technique")
    if clock_s is not None and clock_s <= TIME_TROUBLE_S:
        motifs.append("timeout_adjacent")
    if not motifs:
        motifs.append("uncategorized")
    open_files = count_open_files(board_after)
    return {
        "motifs": motifs,
        "material_balance": material_balance(board_after),
        "clock_s": clock_s,
        "open_files": open_files,
        "position_type": "open" if open_files >= OPEN_FILES_OPEN else "closed",
        "king_castled": king_castled(board_after, side),
        "time_trouble": clock_s is not None and clock_s <= TIME_TROUBLE_S,
    }


def review_game(pgn_text: str, engine: Engine, cfg: AppConfig) -> dict:
    """Full chess.com-style review of one game. Returns a serializable result dict."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("unreadable PGN")
    if game.errors:
        raise ValueError(f"PGN parse errors: {game.errors[0]}")
    clocks = extract_clocks(game)
    board = game.board()
    info = engine.evaluate(board, eval_ms=cfg.eval_ms, eval_depth=cfg.eval_depth, multipv=cfg.multipv)[0]
    evals: list[int] = [info["cp"]]
    best_pv: list[str | None] = [info["pv"][0] if info["pv"] else None]
    moves_meta: list[dict] = []
    ply = 0
    for move in game.mainline_moves():
        ply += 1
        side = "white" if board.turn == chess.WHITE else "black"
        san = board.san(move)
        board_before = board.copy(stack=False)
        played_best = best_pv[-1] == move.uci()
        sacrifice = is_sacrifice(board_before, move)
        board.push(move)
        info = engine.evaluate(board, eval_ms=cfg.eval_ms, eval_depth=cfg.eval_depth, multipv=cfg.multipv)[0]
        cp_after = info["cp"]
        evals.append(cp_after)
        best_pv.append(info["pv"][0] if info["pv"] else None)
        winp_before = win_percent(evals[-2])
        winp_after = win_percent(evals[-1])
        drop = drop_for_mover(winp_before, winp_after, side)
        cp_mover = cp_after if side == "white" else -cp_after
        classification = classify(drop, played_best, sacrifice, cp_mover, cfg.thresholds)
        clock_s = clocks[ply - 1] if ply - 1 < len(clocks) else None
        entry = {
            "ply": ply,
            "side": side,
            "san": san,
            "uci": move.uci(),
            "cp_before": evals[-2],
            "cp_after": cp_after,
            "winp_before": round(winp_before, 2),
            "winp_after": round(winp_after, 2),
            "drop": round(drop, 2),
            "classification": classification,
            "accuracy": round(move_accuracy(drop), 2),
            "clock_s": clock_s,
        }
        if classification in ("mistake", "blunder"):
            board_after = board.copy(stack=False)
            entry["meta"] = motifs_for(board_before, move, board_after, best_pv[-2], clock_s)
        moves_meta.append(entry)

    def harmonic_mean(vals: list[float]) -> float:
        """Chess.com-style game accuracy: harmonic mean of per-move accuracies,
        so a few bad moves dominate (closer to chess.com Game Review than the
        arithmetic mean, which flatters games with many tiny inaccuracies)."""
        return len(vals) / sum(1.0 / max(v, 0.01) for v in vals)

    def side_accuracy(side: str) -> float | None:
        vals = [m["accuracy"] for m in moves_meta if m["side"] == side]
        return round(harmonic_mean(vals), 1) if vals else None

    phases: dict[str, dict[str, float | None]] = {}
    for phase in ("opening", "middlegame", "endgame"):
        phases[phase] = {"white": None, "black": None}
        for side in ("white", "black"):
            vals = [
                m["accuracy"]
                for m in moves_meta
                if m["side"] == side and phase_of(m["ply"]) == phase
            ]
            if len(vals) >= MIN_PHASE_MOVES:
                phases[phase][side] = round(harmonic_mean(vals), 1)

    blunders = [m for m in moves_meta if m["classification"] in ("mistake", "blunder")]
    return {
        "accuracy_white": side_accuracy("white"),
        "accuracy_black": side_accuracy("black"),
        "phases": phases,
        "moves": moves_meta,
        "blunders": blunders,
    }


def _is_fresh(
    row: sqlite3.Row | None, signature: str, checksum: str, engine_version: str | None
) -> bool:
    """A stored analysis is fresh when engine version, budget/thresholds signature,
    and the input PGN checksum all match the current configuration."""
    return bool(
        row
        and row["signature"] == signature
        and row["input_checksum"] == checksum
        and engine_version is not None
        and row["engine_version"] == engine_version
    )


def _store_analysis(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    result: dict,
    engine_name: str,
    signature: str,
    checksum: str,
) -> None:
    """Upsert the review of one game (caller commits)."""
    conn.execute(
        """
        INSERT INTO analysis (game_uuid, engine_version, signature, input_checksum,
                              analyzed_at, accuracy_white, accuracy_black, phases, moves, blunders)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_uuid) DO UPDATE SET
            engine_version = excluded.engine_version,
            signature = excluded.signature,
            input_checksum = excluded.input_checksum,
            analyzed_at = excluded.analyzed_at,
            accuracy_white = excluded.accuracy_white,
            accuracy_black = excluded.accuracy_black,
            phases = excluded.phases,
            moves = excluded.moves,
            blunders = excluded.blunders
        """,
        (
            row["uuid"],
            engine_name,
            signature,
            checksum,
            datetime.now(timezone.utc).isoformat(),
            result["accuracy_white"],
            result["accuracy_black"],
            json.dumps(result["phases"]),
            json.dumps(result["moves"]),
            json.dumps(result["blunders"]),
        ),
    )


def run_analyze(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    *,
    limit: int | None = None,
    game_uuid: str | None = None,
    force: bool = False,
    workers: int | None = None,
) -> int:
    """Analyze unanalyzed or stale games, oldest first; commits per game (resumable).

    A game is skipped when a stored analysis matches the current engine version,
    engine budget, thresholds, and PGN checksum. `--game UUID` (or force)
    re-analyzes explicitly. A single bad game is logged and skipped, never fatal.

    With `workers` > 1 (config key `workers` or the CLI option) the games run in
    parallel: each worker thread owns its own Stockfish process and SQLite
    connection and commits its results as they finish (WAL, short transactions).
    """
    n_workers = max(1, cfg.workers if workers is None else workers)
    signature = cfg.analysis_signature
    if game_uuid:
        rows = conn.execute(
            "SELECT * FROM games WHERE uuid = ? AND parse_error IS NULL AND pgn != ''",
            (game_uuid,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM games WHERE parse_error IS NULL AND pgn != '' ORDER BY end_time"
        ).fetchall()
    if game_uuid or n_workers == 1:
        return _run_sequential(conn, cfg, rows, signature, limit=limit, game_uuid=game_uuid, force=force)
    return _run_parallel(conn, cfg, rows, signature, limit=limit, force=force, workers=n_workers)


def _run_sequential(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    rows: list[sqlite3.Row],
    signature: str,
    *,
    limit: int | None,
    game_uuid: str | None,
    force: bool,
) -> int:
    done = skipped_errors = 0
    engine: Engine | None = None
    try:
        for row in rows:
            if limit is not None and done >= limit:
                break
            checksum = pgn_checksum(row["pgn"])
            existing = conn.execute(
                "SELECT engine_version, signature, input_checksum FROM analysis WHERE game_uuid = ?",
                (row["uuid"],),
            ).fetchone()
            if engine is None:
                # Spawn eagerly to log the engine version at run start and to
                # include it in every freshness check (spec: version, budget,
                # thresholds can all invalidate results).
                engine = Engine(cfg.engine_path, threads=cfg.threads, hash_mb=cfg.hash_mb)
                log.info("engine: %s", engine.name)
            if not force and not game_uuid and _is_fresh(existing, signature, checksum, engine.name):
                continue
            log.info("analyzing %s (end_time %s)", row["uuid"], row["end_time"])
            try:
                result = review_game(row["pgn"], engine, cfg)
            except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
                log.warning("skipping %s: %s", row["uuid"], exc)
                skipped_errors += 1
                continue
            _store_analysis(conn, row, result, engine.name, signature, checksum)
            conn.commit()
            done += 1
    except KeyboardInterrupt:
        log.info("interrupted — %d games analyzed so far are committed", done)
        raise
    finally:
        if engine is not None:
            engine.close()
    if skipped_errors:
        log.warning("%d games were skipped due to analysis errors", skipped_errors)
    return done


def _select_stale(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    signature: str,
    engine_name: str,
    *,
    limit: int | None,
    force: bool,
) -> list[tuple[sqlite3.Row, str]]:
    """Rows needing analysis plus their PGN checksums, oldest first, honoring `limit`."""
    todo: list[tuple[sqlite3.Row, str]] = []
    for row in rows:
        if limit is not None and len(todo) >= limit:
            break
        checksum = pgn_checksum(row["pgn"])
        existing = conn.execute(
            "SELECT engine_version, signature, input_checksum FROM analysis WHERE game_uuid = ?",
            (row["uuid"],),
        ).fetchone()
        if not force and _is_fresh(existing, signature, checksum, engine_name):
            continue
        todo.append((row, checksum))
    return todo


def _run_parallel(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    rows: list[sqlite3.Row],
    signature: str,
    *,
    limit: int | None,
    force: bool,
    workers: int,
) -> int:
    """Parallel game review: N threads, each with its own engine process and
    SQLite connection, pulling from a shared queue and committing per game.

    Ctrl+C stops dispatching new games and waits for the in-flight ones to
    finish (Ctrl+C again abandons them; Stockfish exits on stdin EOF).
    """
    if not rows or (limit is not None and limit <= 0):
        return 0
    # One probe engine supplies the version for all freshness checks (all
    # workers run the same binary, so their versions match).
    probe = Engine(cfg.engine_path, threads=cfg.threads, hash_mb=cfg.hash_mb)
    log.info("engine: %s", probe.name)
    try:
        todo = _select_stale(conn, rows, signature, probe.name, limit=limit, force=force)
    finally:
        probe.close()
    if not todo:
        return 0
    workers = min(workers, len(todo))
    log.info("analyzing %d games with %d parallel workers", len(todo), workers)

    task_queue: queue.Queue[tuple[sqlite3.Row, str]] = queue.Queue()
    for item in todo:
        task_queue.put(item)
    stop = threading.Event()
    stats = {"done": 0, "skipped": 0}
    stats_lock = threading.Lock()

    def worker() -> None:
        wconn = connect(cfg.db_path)
        engine: Engine | None = None
        try:
            engine = Engine(cfg.engine_path, threads=cfg.threads, hash_mb=cfg.hash_mb)
            while not stop.is_set():
                try:
                    row, checksum = task_queue.get_nowait()
                except queue.Empty:
                    return
                log.info("analyzing %s (end_time %s)", row["uuid"], row["end_time"])
                try:
                    result = review_game(row["pgn"], engine, cfg)
                    _store_analysis(wconn, row, result, engine.name, signature, checksum)
                    wconn.commit()
                    with stats_lock:
                        stats["done"] += 1
                except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
                    log.warning("skipping %s: %s", row["uuid"], exc)
                    with stats_lock:
                        stats["skipped"] += 1
        finally:
            if engine is not None:
                engine.close()
            wconn.close()

    threads = [
        threading.Thread(target=worker, daemon=True, name=f"analyze-{i}") for i in range(workers)
    ]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.1)
    except KeyboardInterrupt:
        stop.set()
        log.info("interrupted — finishing in-flight games (Ctrl+C again to abandon)")
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            raise
        log.info("interrupted — %d games analyzed so far are committed", stats["done"])
        raise KeyboardInterrupt
    if stats["skipped"]:
        log.warning("%d games were skipped due to analysis errors", stats["skipped"])
    return stats["done"]
