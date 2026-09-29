"""Thin synchronous UCI wrapper around a Stockfish binary."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import chess
import chess.engine

log = logging.getLogger(__name__)


class Engine:
    """Owns one UCI engine process on a private asyncio loop; supports
    positional evaluation with a time or depth budget."""

    def __init__(self, path: str, *, threads: int = 1, hash_mb: int = 256):
        self._loop = asyncio.new_event_loop()
        try:
            self._transport, self._engine = self._loop.run_until_complete(
                chess.engine.popen_uci(path)
            )
            self._loop.run_until_complete(
                self._engine.configure({"Threads": threads, "Hash": hash_mb})
            )
        except Exception:
            self.close()
            raise
        self.name = self._engine.id.get("name", "unknown")
        log.info("engine ready: %s (%s)", self.name, path)

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._loop.run_until_complete(self._engine.quit())
        except Exception:  # noqa: BLE001 — engine may already be dead
            pass
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    def evaluate(
        self,
        board: chess.Board,
        *,
        eval_ms: int = 1000,
        eval_depth: int = 0,
        multipv: int = 1,
    ) -> list[dict]:
        """Evaluate a position. Returns a list of MultiPV infos (best first):

        each item: {'cp': int (white-POV centipawns; mate as +/-10000),
                    'mate': int | None, 'pv': [uci moves]}
        """
        limit: chess.engine.Limit
        if eval_depth and eval_depth > 0:
            limit = chess.engine.Limit(depth=eval_depth)
        else:
            limit = chess.engine.Limit(time=eval_ms / 1000.0)
        infos = self._loop.run_until_complete(
            self._engine.analyse(board, limit=limit, multipv=multipv)
        )
        if isinstance(infos, dict):
            infos = [infos]
        out = []
        for info in infos:
            score = info["score"].white()
            cp = score.score(mate_score=10000)
            mate = score.mate()
            pv = [m.uci() for m in info.get("pv", [])]
            out.append({"cp": cp, "mate": mate, "pv": pv})
        return out