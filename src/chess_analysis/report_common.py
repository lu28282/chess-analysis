"""Shared report helpers: HTML assembly and offline chess-board diagrams."""

from __future__ import annotations

import io

import chess
import chess.pgn

UNICODE_PIECES = {
    "R": "♖", "N": "♘", "B": "♗", "Q": "♕", "K": "♔", "P": "♙",
    "r": "♜", "n": "♞", "b": "♝", "q": "♛", "k": "♚", "p": "♟",
}


def board_html(fen: str, size: int = 28) -> str:
    """Small static board diagram (HTML table, unicode pieces, no images)."""
    try:
        board = chess.Board(fen)
    except ValueError:
        return ""
    rows = []
    for rank in range(7, -1, -1):
        cells = []
        for file in range(8):
            sq = chess.square(file, rank)
            piece = board.piece_at(sq)
            glyph = UNICODE_PIECES[piece.symbol()] if piece else ""
            light = (file + rank) % 2 == 1
            bg = "#f0d9b5" if light else "#b58863"
            cells.append(
                f'<td style="width:{size}px;height:{size}px;background:{bg};'
                f'text-align:center;font-size:{size - 6}px;line-height:{size}px;'
                f'color:#{"fff" if piece and piece.color == chess.BLACK else "#333"};'
                f'padding:0">{glyph}</td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return '<table style="border-collapse:collapse;border:2px solid #333">' + "".join(rows) + "</table>"


def replay_game(pgn_text: str, ply: int) -> tuple[str, str] | None:
    """Replay a stored PGN to `ply`; returns (fen_before_move, chess.com link)."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    board = game.board()
    for i, move in enumerate(game.mainline_moves(), start=1):
        if i == ply:
            link = game.headers.get("Link", "")
            return board.fen(), link
        board.push(move)
    return None


def render_html(template: str, **replacements: str) -> str:
    """Insert chart.js and the data payload into the template."""
    for key, value in replacements.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template
