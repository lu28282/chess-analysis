"""Game ingestion: PGN parsing, derived fields, ECO lookup, opening classification."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import chess.pgn

from .config import AppConfig

ASSETS_DIR = Path(__file__).parent / "assets"

CLOCK_RE = re.compile(r"%clk\s+(\d+):(\d+):(\d+(?:\.\d+)?)")

GAME_RESULTS = {"1-0", "0-1", "1/2-1/2", "*"}

JOBAVA_SETUP_SAN = ["d4", "Nc3", "Bf4"]


_ECO_TABLE_CACHE: list[tuple[str, str, list[str]]] | None = None


def load_eco_table() -> list[tuple[str, str, list[str]]]:
    """Load the bundled ECO table (cached after the first call)."""
    global _ECO_TABLE_CACHE
    if _ECO_TABLE_CACHE is None:
        entries: list[tuple[str, str, list[str]]] = []
        with open(ASSETS_DIR / "eco.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                entries.append((row["eco"], row["name"], row["moves"].split()))
        _ECO_TABLE_CACHE = entries
    return _ECO_TABLE_CACHE


def lookup_opening(moves_san: list[str], eco_table: list[tuple[str, str, list[str]]]) -> tuple[str | None, str | None]:
    """Longest-prefix ECO match. Returns (eco_code, opening_name).

    A match must cover the whole ECO line (single-move entries like `b4` are valid
    matches for a first move). Longer matches win; ties prefer the later entry,
    which for equally-long lines keeps the more specific named variation.
    """
    best: tuple[str, str] | None = None
    best_len = 0
    for eco, name, line in eco_table:
        n = min(len(line), len(moves_san))
        if n < len(line):
            continue  # game is shorter than this ECO line; not a full match
        if line[0] != moves_san[0]:
            continue  # cheap first-move filter before comparing in depth
        match_len = 0
        for i in range(n):
            if line[i] != moves_san[i]:
                break
            match_len += 1
        if match_len == len(line) and match_len >= best_len:
            best = (eco, name)
            best_len = match_len
    if best is None:
        return None, None
    return best


def extract_clocks(game: chess.pgn.Game) -> list[float | None]:
    """Remaining clock in seconds per ply, from %clk annotations; None when absent."""
    clocks: list[float | None] = []
    for node in game.mainline():
        clk = node.clock()
        if clk is None:
            m = CLOCK_RE.search(node.comment or "")
            if m:
                h, mi, s = m.groups()
                clk = int(h) * 3600 + int(mi) * 60 + float(s)
        clocks.append(clk)
    return clocks


def pgn_checksum(pgn_text: str) -> str:
    return hashlib.sha256(pgn_text.strip().encode("utf-8")).hexdigest()


def _result_for(color: str, result: str) -> str:
    if result == "*":
        return "unknown"
    if result == "1/2-1/2":
        return "draw"
    won = result == "1-0"
    if color == "white":
        return "win" if won else "loss"
    return "win" if not won else "loss"


def parse_game(row: sqlite3.Row, config: AppConfig, eco_table: list[tuple[str, str, list[str]]]) -> dict:
    """Parse one stored game row into derived fields; never raises."""
    out: dict = {
        "parse_error": None,
        "my_color": None,
        "my_result": None,
        "opponent": None,
        "rating_delta": None,
        "opening_code": None,
        "opening_name": None,
        "move_count": None,
        "result_parsed": None,
    }
    try:
        username = config.username.lower()
        white = (row["white"] or "").lower()
        black = (row["black"] or "").lower()
        if white == username:
            color = "white"
        elif black == username:
            color = "black"
        else:
            out["parse_error"] = f"username {config.username!r} not among players"
            out["my_color"] = None
            return out
        out["my_color"] = color
        out["opponent"] = row["black"] if color == "white" else row["white"]
        my_rating = row["white_rating"] if color == "white" else row["black_rating"]
        opp_rating = row["black_rating"] if color == "white" else row["white_rating"]
        if my_rating is not None and opp_rating is not None:
            out["rating_delta"] = opp_rating - my_rating

        game = chess.pgn.read_game(io.StringIO(row["pgn"]))
        if game is None:
            out["parse_error"] = "unreadable PGN"
            return out
        if game.errors:
            out["parse_error"] = f"PGN parse errors: {game.errors[0]}"
            return out

        moves_san: list[str] = []
        board = game.board()
        for move in game.mainline_moves():
            moves_san.append(board.san(move))
            board.push(move)
        result = game.headers.get("Result", "*")
        if result not in GAME_RESULTS:
            result = "*"
        out["result_parsed"] = result
        out["my_result"] = _result_for(color, result)
        out["move_count"] = len(moves_san)

        if (row["rules"] or "chess") != "chess":
            # variants (crazyhouse, king-of-the-hill, ...) have different move
            # semantics: no opening classification, no ECO lookup
            return out

        eco, name = lookup_opening(moves_san, eco_table)
        out["opening_code"] = eco
        out["opening_name"] = name

        opening_class, variation_key = classify_opening(color, moves_san, config)
        out["opening_class"] = opening_class
        out["variation_key"] = variation_key
    except Exception as exc:  # noqa: BLE001 — malformed data must never break ingestion
        out["parse_error"] = f"parse failure: {exc}"
    return out


def classify_opening(color: str, moves_san: list[str], config: AppConfig) -> tuple[str, str | None]:
    """Classify into jobava_london / caro_kann / avoided variants / other.

    Returns (opening_class, variation_key) where variation_key is the first
    `max_ply` SAN moves joined by spaces (None for 'other'). Only the player's
    own moves count towards the Jobava setup; games shorter than 2 plies are
    'other' (nothing was played to classify).
    """
    max_ply = max(config.opening.jobava_max_setup_ply, config.opening.caro_kann_max_ply)
    if len(moves_san) < 2:
        return "other", None
    if color == "white":
        if moves_san[0] != "d4":
            return "other", None
        used = [False] * len(JOBAVA_SETUP_SAN)
        for ply, san in enumerate(moves_san[: config.opening.jobava_max_setup_ply]):
            if ply % 2 != 0:
                continue  # only White's own moves (even, 0-based plies)
            for i, target in enumerate(JOBAVA_SETUP_SAN):
                if not used[i] and san == target:
                    used[i] = True
                    break
        if all(used):
            return "jobava_london", " ".join(moves_san[:max_ply])
        return "jobava_avoided", " ".join(moves_san[:max_ply])
    # black
    if moves_san[0] != "e4":
        return "other", None
    if moves_san[1] != "c6":
        return "caro_avoided", " ".join(moves_san[:max_ply])
    return "caro_kann", " ".join(moves_san[:max_ply])


def sync_parsed(conn: sqlite3.Connection, config: AppConfig, only_uuids: list[str] | None = None) -> tuple[int, int]:
    """(Re)parse the given games (all games when None). Returns (parsed_count, error_count)."""
    eco_table = load_eco_table()
    clauses, params = [], []
    if only_uuids is not None:
        if not only_uuids:
            return 0, 0
        placeholders = ", ".join("?" for _ in only_uuids)
        clauses.append(f"uuid IN ({placeholders})")
        params.extend(only_uuids)
    sql = "SELECT * FROM games"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = conn.execute(sql, params).fetchall()
    parsed = errors = 0
    for row in rows:
        derived = parse_game(row, config, eco_table)
        conn.execute(
            """
            UPDATE games SET
                parse_error = ?, my_color = ?, my_result = ?, opponent = ?,
                rating_delta = ?, opening_code = ?, opening_name = ?,
                move_count = ?, result_parsed = ?,
                opening_class = ?, variation_key = ?
            WHERE uuid = ?
            """,
            (
                derived["parse_error"],
                derived["my_color"],
                derived["my_result"],
                derived["opponent"],
                derived["rating_delta"],
                derived["opening_code"],
                derived["opening_name"],
                derived["move_count"],
                derived["result_parsed"],
                derived.get("opening_class"),
                derived.get("variation_key"),
                row["uuid"],
            ),
        )
        if derived["parse_error"]:
            errors += 1
        else:
            parsed += 1
    conn.commit()
    return parsed, errors


def upsert_games(conn: sqlite3.Connection, month_url: str, games: list[dict]) -> int:
    """Insert or update games from one month archive. Returns number of rows written."""
    written = 0
    for game in games:
        uuid = game.get("uuid")
        if not uuid:
            continue
        row = _game_fields(month_url, game)
        conn.execute(
            """
            INSERT INTO games (uuid, month, end_time, pgn, white, black,
                               white_rating, black_rating, result, time_control,
                               time_class, rules, eco, accuracies)
            VALUES (:uuid, :month, :end_time, :pgn, :white, :black,
                    :white_rating, :black_rating, :result, :time_control,
                    :time_class, :rules, :eco, :accuracies)
            ON CONFLICT(uuid) DO UPDATE SET
                month = excluded.month,
                end_time = excluded.end_time,
                pgn = excluded.pgn,
                white = excluded.white,
                black = excluded.black,
                white_rating = excluded.white_rating,
                black_rating = excluded.black_rating,
                result = excluded.result,
                time_control = excluded.time_control,
                time_class = excluded.time_class,
                rules = excluded.rules,
                eco = excluded.eco,
                accuracies = excluded.accuracies,
                parse_error = NULL
            """,
            row,
        )
        written += 1
    return written


def _game_fields(month_url: str, game: dict) -> dict:
    month = "/".join(month_url.rstrip("/").split("/")[-2:])
    accuracies = game.get("accuracies")
    return {
        "uuid": game["uuid"],
        "month": month,
        "end_time": game.get("end_time"),
        "pgn": game.get("pgn") or "",
        "white": (game.get("white") or {}).get("username"),
        "black": (game.get("black") or {}).get("username"),
        "white_rating": (game.get("white") or {}).get("rating"),
        "black_rating": (game.get("black") or {}).get("rating"),
        "result": game.get("result"),
        "time_control": game.get("time_control"),
        "time_class": game.get("time_class"),
        "rules": game.get("rules"),
        "eco": game.get("eco"),
        "accuracies": json.dumps(accuracies) if accuracies else None,
    }


def store_sync_state(conn: sqlite3.Connection, month_url: str, etag: str | None, last_modified: str | None) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (month_url, etag, last_modified, last_synced_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(month_url) DO UPDATE SET
            etag = excluded.etag,
            last_modified = excluded.last_modified,
            last_synced_at = excluded.last_synced_at
        """,
        (month_url, etag, last_modified, datetime.now(timezone.utc).isoformat()),
    )


def load_sync_state(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["month_url"]: row for row in conn.execute("SELECT * FROM sync_state")}
