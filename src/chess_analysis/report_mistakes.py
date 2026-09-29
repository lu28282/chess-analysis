"""Story 5: self-contained mistake-pattern HTML report (offline, Chart.js vendored)."""

from __future__ import annotations

import html
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis import TIME_TROUBLE_S, phase_of
from .config import AppConfig
from .report_common import board_html, render_html, replay_game

ASSETS_DIR = Path(__file__).parent / "assets"


def _rating_bracket(rating: int | None) -> str:
    if rating is None:
        return "unknown"
    base = (rating // 200) * 200
    return f"{base:04d}-{base + 199:04d}"


def _example_ref(conn: sqlite3.Connection, pgn_cache: dict, game_uuid: str, ply: int) -> dict:
    """Board diagram + game link for one example position (no engine needed)."""
    if game_uuid not in pgn_cache:
        row = conn.execute("SELECT pgn FROM games WHERE uuid = ?", (game_uuid,)).fetchone()
        pgn_cache[game_uuid] = row["pgn"] if row else ""
    info = replay_game(pgn_cache[game_uuid], ply) or ("", None)
    fen, link = info
    return {"diagram": board_html(fen) if fen else "", "fen": fen, "link": link or ""}


def collect_data(conn: sqlite3.Connection, config: AppConfig) -> dict[str, Any]:
    """Aggregate analysis results into report-ready data structures.

    Frequency statistics are computed client-side from `blunders` (single source
    of truth, keeps filters consistent); this function gathers, enriches, and
    precomputes only what needs the DB: per-move counts for normalization,
    accuracy trends, motif/theme examples with diagrams, and human labels.
    """
    rows = conn.execute(
        """
        SELECT g.uuid, g.my_color, g.my_result, g.time_class, g.end_time,
               g.white_rating, g.black_rating, g.month,
               a.accuracy_white, a.accuracy_black, a.moves, a.blunders
        FROM games g JOIN analysis a ON a.game_uuid = g.uuid
        WHERE g.parse_error IS NULL
        """
    ).fetchall()

    pgn_cache: dict[str, str] = {}
    games: list[dict] = []
    blunders: list[dict] = []
    moves_by_color_tc: Counter = Counter()

    for r in rows:
        my_color = r["my_color"]
        my_rating = r["white_rating"] if my_color == "white" else r["black_rating"]
        my_acc = r["accuracy_white"] if my_color == "white" else r["accuracy_black"]
        moves = json.loads(r["moves"])
        games.append(
            {
                "uuid": r["uuid"],
                "color": my_color,
                "result": r["my_result"],
                "time_class": r["time_class"],
                "end_time": r["end_time"],
                "month": r["month"],
                "rating": my_rating,
                "accuracy": my_acc,
            }
        )
        moves_by_color_tc[(my_color, r["time_class"])] += sum(
            1 for m in moves if m["side"] == my_color
        )
        for b in json.loads(r["blunders"]):
            if b["side"] != my_color:
                continue  # my mistakes only
            entry = dict(b)
            entry.update(
                {
                    "game_uuid": r["uuid"],
                    "time_class": r["time_class"],
                    "color": my_color,
                    "rating": my_rating,
                    "rating_bracket": _rating_bracket(my_rating),
                    "opponent_rating": (
                        r["black_rating"] if my_color == "white" else r["white_rating"]
                    ),
                    "month": r["month"],
                }
            )
            blunders.append(entry)

    # motif examples: top N example positions per motif (with board diagrams)
    motif_examples: dict[str, list] = defaultdict(list)
    for b in sorted(blunders, key=lambda x: -x["drop"]):
        for motif in b.get("meta", {}).get("motifs", []):
            if len(motif_examples[motif]) < 8:
                ref = _example_ref(conn, pgn_cache, b["game_uuid"], b["ply"])
                motif_examples[motif].append(
                    {
                        "game_uuid": b["game_uuid"],
                        "ply": b["ply"],
                        "san": b["san"],
                        "color": b["color"],
                        "time_class": b["time_class"],
                        "drop": b["drop"],
                        "classification": b["classification"],
                        "month": b["month"],
                        **ref,
                    }
                )

    # recurring themes: aggregate motif x position-type x phase x material clusters
    theme_counter: Counter = Counter()
    theme_examples: dict[tuple, list] = defaultdict(list)
    for b in blunders:
        meta = b.get("meta", {})
        if not meta:
            continue
        for motif in meta.get("motifs", []):
            key = (
                motif,
                meta.get("position_type", "?"),
                phase_of(b["ply"]),
                "ahead" if meta.get("material_balance", 0) > 0 else "behind/equal",
            )
            theme_counter[key] += 1
            if len(theme_examples[key]) < 8:
                ref = _example_ref(conn, pgn_cache, b["game_uuid"], b["ply"])
                theme_examples[key].append(
                    {
                        "game_uuid": b["game_uuid"],
                        "ply": b["ply"],
                        "san": b["san"],
                        "color": b["color"],
                        "time_class": b["time_class"],
                        "drop": b["drop"],
                        "classification": b["classification"],
                        "month": b["month"],
                        "material_balance": meta.get("material_balance"),
                        "clock_s": meta.get("clock_s"),
                        **ref,
                    }
                )

    # merge human labels (labels live in the pattern_labels table)
    labels = {
        row["theme_key"]: row["label"]
        for row in conn.execute("SELECT theme_key, label FROM pattern_labels WHERE label IS NOT NULL")
    }
    themes = []
    for key, count in theme_counter.most_common(30):
        stored_key = json.dumps(key)
        themes.append(
            {
                "key": stored_key,
                "motif": key[0],
                "position": key[1],
                "phase": key[2],
                "material": key[3],
                "count": count,
                "label": labels.get(stored_key, ""),
                "examples": theme_examples[key],
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "username": config.username,
        "games": games,
        "blunders": blunders,
        "time_trouble_s": TIME_TROUBLE_S,
        "moves_by_color_tc": {f"{c}|{tc}": n for (c, tc), n in moves_by_color_tc.items()},
        "motif_examples": dict(motif_examples),
        "themes": themes,
    }


def generate_report(conn: sqlite3.Connection, config: AppConfig, output: Path | None = None) -> Path:
    data = collect_data(conn, config)
    out = Path(output) if output else config.reports_dir / "mistake-patterns.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    template = (ASSETS_DIR / "mistake_report_template.html").read_text(encoding="utf-8")
    chartjs = (ASSETS_DIR / "chart.umd.js").read_text(encoding="utf-8")
    payload = json.dumps(data).replace("</", "<\\/")  # keep inline <script> safe
    page = render_html(
        template,
        chartjs=chartjs,
        data_json=payload,
        username=html.escape(data["username"]),
        generated_at=data["generated_at"][:19],
        n_games=str(len(data["games"])),
        n_blunders=str(len(data["blunders"])),
    )
    out.write_text(page, encoding="utf-8")
    return out
