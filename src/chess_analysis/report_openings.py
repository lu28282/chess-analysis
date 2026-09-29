"""Story 6: opening coach report — Jobava London & Caro-Kann drill-down."""

from __future__ import annotations

import html
import io
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from .config import AppConfig
from .opening_book import (
    _child_fen,
    book_gap_min_total,
    load_book,
    mainline_san,
    walk_game_through_book,
)
from .report_common import render_html

ASSETS_DIR = Path(__file__).parent / "assets"

PUNISH_WINDOW_PLIES = 6
SOFT = ("inaccuracy", "mistake", "blunder")
BAD = ("mistake", "blunder")


def _parse_game(pgn_text: str) -> tuple[list[str], list[str]] | None:
    """(uci moves, san moves) for a game's mainline, or None if unreadable."""
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
    except ValueError:
        return None
    if game is None:
        return None
    ucis: list[str] = []
    sans: list[str] = []
    try:
        board = game.board()
        for move in game.mainline_moves():
            ucis.append(move.uci())
            sans.append(board.san(move))
            board.push(move)
    except ValueError:
        return None
    return ucis, sans


def _first_soft_move(moves: list[dict], side: str) -> int | None:
    for m in moves:
        if m["side"] == side and m["classification"] in SOFT:
            return m["ply"]
    return None


def _punished_after(moves: list[dict], off_ply: int, off_side: str) -> bool | None:
    """Did the side that left the book blunder within the punish window?"""
    for m in moves:
        if off_ply < m["ply"] <= off_ply + PUNISH_WINDOW_PLIES:
            if m["side"] == off_side and m["classification"] in BAD:
                return True
    return False


def _opponent_line_key(sans: list[str], my_color: str, plies: int = 6) -> str:
    """SAN sequence of the opponent's first moves (the line they chose)."""
    opp_is_white = my_color == "black"
    out = []
    for i, san in enumerate(sans):
        white_to_move = i % 2 == 0
        if white_to_move == opp_is_white and i < plies:
            out.append(san)
    return " ".join(out)


def _avg(vals: list) -> float | None:
    return round(sum(vals) / len(vals), 1) if vals else None


def _tally(table: dict, result: str | None) -> None:
    if result == "win":
        table["win"] += 1
    elif result == "loss":
        table["loss"] += 1
    elif result == "draw":
        table["draw"] += 1
    # unknown (abandoned etc.) counts towards games only


def _summarize_variations(variations: dict[str, dict]) -> list[dict]:
    out = []
    for key, v in sorted(variations.items(), key=lambda kv: -kv[1]["games"]):
        out.append(
            {
                "variation": key,
                "games": v["games"],
                "win": v["win"],
                "loss": v["loss"],
                "draw": v["draw"],
                "my_accuracy": _avg(v["my_acc"]),
                "opp_accuracy": _avg(v["opp_acc"]),
                "first_soft_mine": _avg(v["first_soft_mine"]),
                "first_soft_opp": _avg(v["first_soft_opp"]),
                "book_off_ply": _avg(v["off_ply"]),
                "off_by_me": v["off_by_me"],
            }
        )
    return out[:40]


def _summarize_opponent_lines(opponent_lines: dict[str, dict]) -> list[dict]:
    out = []
    for key, o in sorted(opponent_lines.items(), key=lambda kv: -kv[1]["games"]):
        out.append(
            {
                "line": key,
                "games": o["games"],
                "score_pct": round(100.0 * (o["win"] + 0.5 * o["draw"]) / o["games"], 1),
                "win": o["win"],
                "loss": o["loss"],
                "draw": o["draw"],
                "my_accuracy": _avg(o["my_acc"]),
                "avg_off_ply": _avg(o["off_ply"]),
                "punished": o["punished"],
                "unpunished": o["unpunished"],
            }
        )
    return out[:40]


def _repertoire_gaps(
    book: dict[str, dict],
    seen_child_fens: set[str],
    min_total: int,
    max_ply: int = 8,
) -> list[dict]:
    """Popular book branches that none of my games ever reached.

    Paths are reconstructed by BFS from the root through the book, so every
    reachable node gets a complete SAN line (transposed parents converge
    deterministically on the first-visited route).
    """
    san_paths: dict[str, list[str]] = {chess.Board().fen(): []}
    queue = [chess.Board().fen()]
    while queue:
        parent = queue.pop(0)
        node = book.get(parent)
        if node is None:
            continue
        for uci, edge in node.items():
            child = _child_fen(parent, uci)
            if child not in san_paths:
                san_paths[child] = san_paths[parent] + [edge["move_san"]]
                queue.append(child)

    gaps = []
    for parent_fen, node in book.items():
        for edge in node.values():
            if edge["ply"] > max_ply or edge["total"] < min_total:
                continue
            if _child_fen(parent_fen, edge["move_uci"]) in seen_child_fens:
                continue
            path = san_paths.get(parent_fen, [])
            gaps.append(
                {
                    "move_san": edge["move_san"],
                    "ply": edge["ply"],
                    "total_games": edge["total"],
                    "path": " ".join(path + [edge["move_san"]]),
                }
            )
    gaps.sort(key=lambda g: (-g["total_games"], g["ply"]))
    return gaps[:20]


def _section(opening: str, label: str, description: str, per_game: list[dict], book: dict,
             seen_child_fens: set[str], conn: sqlite3.Connection,
             gap_min_total: int, include_gaps: bool, max_ply: int) -> dict:
    games = [g for g in per_game if g["opening"] == opening]
    variations: dict[str, dict] = defaultdict(
        lambda: {"games": 0, "win": 0, "loss": 0, "draw": 0,
                  "my_acc": [], "opp_acc": [], "first_soft_mine": [], "first_soft_opp": [],
                  "off_ply": [], "off_by_me": 0}
    )
    opponent_lines: dict[str, dict] = defaultdict(
        lambda: {"games": 0, "win": 0, "loss": 0, "draw": 0, "my_acc": [],
                 "off_ply": [], "punished": 0, "unpunished": 0}
    )
    leaver_stats = Counter()
    failed_punish: list[dict] = []

    for g in games:
        v = variations[g["variation_key"]]
        v["games"] += 1
        _tally(v, g["result"])
        if g["my_accuracy"] is not None:
            v["my_acc"].append(g["my_accuracy"])
        if g["opp_accuracy"] is not None:
            v["opp_acc"].append(g["opp_accuracy"])
        if g["first_soft_mine"] is not None:
            v["first_soft_mine"].append(g["first_soft_mine"])
        if g["first_soft_opp"] is not None:
            v["first_soft_opp"].append(g["first_soft_opp"])
        if g["off_ply"] is not None:
            v["off_ply"].append(g["off_ply"])
            if g["off_by_me"]:
                v["off_by_me"] += 1

        o = opponent_lines[g["opponent_line"]]
        o["games"] += 1
        _tally(o, g["result"])
        if g["my_accuracy"] is not None:
            o["my_acc"].append(g["my_accuracy"])
        if g["off_ply"] is not None:
            o["off_ply"].append(g["off_ply"])
            if g["off_by_me"]:
                leaver_stats[("me", g["punished"])] += 1
            else:
                # the opponent left the book: did I punish it?
                leaver_stats[("opponent", g["punished"])] += 1
                if g["punished"] is True:
                    o["punished"] += 1
                elif g["punished"] is False:
                    o["unpunished"] += 1
                    failed_punish.append(
                        {
                            "uuid": g["uuid"],
                            "off_ply": g["off_ply"],
                            "san": g["sans"][g["off_ply"] - 1] if g["off_ply"] <= len(g["sans"]) else "",
                            "my_accuracy": g["my_accuracy"],
                        }
                    )

    return {
        "label": label,
        "description": description,
        "games_count": len(games),
        "variation_count": len(variations),
        "variations": _summarize_variations(variations),
        "opponent_lines": _summarize_opponent_lines(opponent_lines),
        "off_by_me": leaver_stats[("me", True)] + leaver_stats[("me", False)] + leaver_stats[("me", None)],
        "off_by_opponent": leaver_stats[("opponent", True)] + leaver_stats[("opponent", False)] + leaver_stats[("opponent", None)],
        "failed_punish": failed_punish[:20],
        # a book built from the player's own games cannot reveal unfaced lines:
        # gap detection only makes sense against a reference (lichess) book
        "gaps": _repertoire_gaps(book, seen_child_fens, min_total=gap_min_total)
        if include_gaps else [],
        "mainline": mainline_san(conn, opening, max_ply=max_ply),
    }


def collect_data(conn: sqlite3.Connection, config: AppConfig) -> dict[str, Any]:
    """Aggregate opening classification + book-leaving + analysis into report data."""
    books = {
        "jobava_london": load_book(conn, "jobava_london"),
        "caro_kann": load_book(conn, "caro_kann"),
    }
    max_ply = config.explorer.max_ply
    class_counts: Counter = Counter()
    per_game: list[dict] = []
    seen_child_fens: dict[str, set[str]] = {"jobava_london": set(), "caro_kann": set()}
    book_sources = {row[0] for row in conn.execute("SELECT DISTINCT source FROM opening_book")}
    book_source = (
        "none" if not book_sources
        else "mixed" if len(book_sources) > 1
        else next(iter(book_sources))
    )
    gap_min_total = book_gap_min_total(book_source, config.explorer.min_games)
    include_gaps = book_source != "local"
    book_max_ply = config.explorer.max_ply

    rows = conn.execute(
        """
        SELECT g.uuid, g.pgn, g.my_color, g.my_result, g.opening_class, g.variation_key,
               g.time_class,
               a.accuracy_white, a.accuracy_black, a.moves
        FROM games g LEFT JOIN analysis a ON a.game_uuid = g.uuid
        WHERE g.parse_error IS NULL AND g.opening_class IS NOT NULL AND g.rules = 'chess'
        """
    ).fetchall()
    # clear stale book-off marks: recomputed below for every walkable game
    conn.execute("UPDATE games SET book_off_ply = NULL, book_off_side = NULL WHERE book_off_ply IS NOT NULL")

    for r in rows:
        opening = r["opening_class"]
        class_counts[opening] += 1
        if opening not in books:
            continue
        parsed = _parse_game(r["pgn"])
        if not parsed:
            continue
        ucis, sans = parsed
        moves = json.loads(r["moves"]) if r["moves"] else None
        my_acc = opp_acc = None
        if moves:
            my_acc = r["accuracy_white"] if r["my_color"] == "white" else r["accuracy_black"]
            opp_acc = r["accuracy_black"] if r["my_color"] == "white" else r["accuracy_white"]
        off_ply, off_side, visited = walk_game_through_book(ucis, books[opening], max_ply)
        seen_child_fens[opening].update(visited)
        entry = {
            "uuid": r["uuid"],
            "opening": opening,
            "color": r["my_color"],
            "result": r["my_result"],
            "variation_key": r["variation_key"],
            "opponent_line": _opponent_line_key(sans, r["my_color"]),
            "time_class": r["time_class"],
            "my_accuracy": my_acc,
            "opp_accuracy": opp_acc,
            "off_ply": off_ply,
            "off_side": off_side,
            "off_by_me": off_side == r["my_color"],
            "punished": _punished_after(moves, off_ply, off_side) if moves and off_ply is not None else None,
            "first_soft_mine": _first_soft_move(moves, r["my_color"]) if moves else None,
            "first_soft_opp": _first_soft_move(moves, "black" if r["my_color"] == "white" else "white") if moves else None,
            "sans": sans,
        }
        per_game.append(entry)
        if off_ply is not None:
            conn.execute(
                "UPDATE games SET book_off_ply = ?, book_off_side = ? WHERE uuid = ?",
                (off_ply, off_side, r["uuid"]),
            )
        else:
            conn.execute(
                "UPDATE games SET book_off_ply = NULL, book_off_side = NULL WHERE uuid = ?",
                (r["uuid"],),
            )
    conn.commit()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "username": config.username,
        "book_source": book_source,
        "class_counts": dict(class_counts),
        "jobava": _section(
            "jobava_london", "Jobava London (White)",
            "System opening: setup consistency matters; opponents choose how to disrupt it.",
            per_game, books["jobava_london"], seen_child_fens["jobava_london"], conn,
            gap_min_total, include_gaps, book_max_ply,
        ),
        "caro_kann": _section(
            "caro_kann", "Caro-Kann (Black vs 1.e4)",
            "White chooses the variation — coverage across all anti-Caro lines matters.",
            per_game, books["caro_kann"], seen_child_fens["caro_kann"], conn,
            gap_min_total, include_gaps, book_max_ply,
        ),
        "avoided": {
            "jobava_avoided": class_counts.get("jobava_avoided", 0),
            "caro_avoided": class_counts.get("caro_avoided", 0),
        },
    }


def generate_report(conn: sqlite3.Connection, config: AppConfig, output: Path | None = None) -> Path:
    data = collect_data(conn, config)
    out = Path(output) if output else config.reports_dir / "openings.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    template = (ASSETS_DIR / "openings_report_template.html").read_text(encoding="utf-8")
    chartjs = (ASSETS_DIR / "chart.umd.js").read_text(encoding="utf-8")
    payload = json.dumps(data).replace("</", "<\\/")
    page = render_html(
        template,
        chartjs=chartjs,
        data_json=payload,
        username=html.escape(data["username"]),
        generated_at=data["generated_at"][:19],
    )
    out.write_text(page, encoding="utf-8")
    return out
