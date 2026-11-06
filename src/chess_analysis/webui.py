"""Web UI: run the pipeline steps from the browser and view the reports.

Stdlib-only (http.server) to keep the runtime dependency-free: each pipeline
step is spawned as a CLI subprocess, logged to <db_dir>/logs/<step>.log, and
only one job may run at a time (SQLite is the single writer). The interactive
game viewer (/viewer) serves read-only API endpoints — WAL allows those to
run alongside a job. "Ask Stockfish" (/api/best) shares one engine process,
serialized by a lock; it never touches the DB.

The TOML config file is the single source of truth: the settings page
(/settings) and the first-run wizard (/setup when no config exists) write it,
and a ConfigWatcher picks up changes for the UI itself; running jobs load it
anew from disk when they start.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import chess
import chess.pgn

from .analysis import OPENING_PLIES, TIME_TROUBLE_S, win_percent
from .config import AppConfig, load_config, validate_since
from .db import connect, open_db
from .settings import schema_json, settings_document, update_settings

ASSETS_DIR = Path(__file__).parent / "assets"

UUID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

PV_SAN_LIMIT = 8  # best-line preview length in "Ask Stockfish" answers
LABEL_MAX_LEN = 300

STEPS = {
    "fetch": "Download game archives from chess.com (window limits in the form)",
    "sync-parsed": "Parse stored PGNs into derived fields (usually automatic)",
    "build-opening-book": "Build the reference opening book from your own games (offline, no analysis needed)",
    "analyze": "Engine review of every game (slow; resumable; use the viewer afterwards)",
    "report-mistakes": "Aggregate mistake statistics as an offline HTML report (per-game replay lives in the viewer)",
    "report-openings": "Opening-coach HTML report: Jobava London & Caro-Kann lines and deviations",
}

# Extra CLI arguments per step (default: none). The opening book is built
# offline from the games in the DB — the lichess explorer default needs
# network, is rate-limited, and may be authorization-gated.
STEP_ARGS: dict[str, list[str]] = {
    "build-opening-book": ["--source", "local"],
}

# Which steps accept per-run parameters from the UI (validated in _step_args).
STEP_PARAMS: dict[str, tuple[str, ...]] = {
    "fetch": ("months", "since", "max_games"),
    "analyze": ("limit", "workers"),
}

LOG_TAIL_CHARS = 8000


class ConfigWatcher:
    """Reloads the TOML config when the file changes (or appears/ disappears).

    `get()` returns None while the file is missing — the UI then serves the
    first-run setup wizard. A file that fails to validate keeps the last good
    config; `error` carries the reason for the settings page.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()  # serializes settings saves against each other
        self._config: AppConfig | None = None
        self._stamp: tuple | None = None
        self._error: str | None = None
        self.refresh()

    def refresh(self) -> None:
        try:
            st = self._path.stat()
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            with self._lock:
                if self._config is not None or self._stamp is not None:
                    self._config = None
                    self._stamp = None
                self._error = f"config file not found: {self._path}"
            return
        with self._lock:
            if stamp == self._stamp:
                return
        try:
            config = load_config(self._path)
        except Exception as exc:  # noqa: BLE001 — a broken file must not kill the UI
            with self._lock:
                self._error = f"invalid config: {exc}"
            return
        with self._lock:
            self._config = config
            self._stamp = stamp
            self._error = None

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Serialize a settings save: read-merge-validate-write + reload.

        The lock prevents two concurrent POSTs from discarding each other's
        changes (lost update); the watcher reload inside the lock makes the
        very next request see the new file.
        """
        with self._save_lock:
            doc = update_settings(self._path, payload)
            self.refresh()
            return doc

    def get(self) -> AppConfig | None:
        """The current config, or None while it is missing."""
        self.refresh()
        with self._lock:
            return self._config

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def path(self) -> Path:
        return self._path


def _int_param(params: dict[str, Any], key: str, option: str, minimum: int) -> list[str]:
    """One integer run-parameter -> CLI option, validated with a clear message."""
    value = params.get(key)
    if value in (None, ""):
        return []
    try:
        i = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer") from None
    if i < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return [option, str(i)]


def _step_args(step: str, params: dict[str, Any] | None) -> list[str]:
    """Validate per-run parameters and turn them into CLI arguments.

    Raises ValueError with a user-presentable message on bad input.
    """
    args: list[str] = []
    if not params:
        return args
    if step not in STEP_PARAMS:
        raise ValueError(f"step {step!r} takes no parameters")
    unknown = sorted(set(params) - set(STEP_PARAMS[step]))
    if unknown:
        raise ValueError(f"unknown parameter(s) for {step}: {', '.join(unknown)}")
    if step == "fetch":
        args += _int_param(params, "months", "--months", 0)
        since = params.get("since")
        if since:
            args += ["--since", validate_since(str(since))]
        args += _int_param(params, "max_games", "--max-games", 0)
    elif step == "analyze":
        args += _int_param(params, "limit", "--limit", 0)
        args += _int_param(params, "workers", "--workers", 0)
    return args


class JobManager:
    """Runs one pipeline step at a time as a subprocess; keeps the last job.

    The config is read fresh from disk (via the watcher) when a job starts, so
    settings saved in the web UI apply to the next run without a restart.
    """

    def __init__(self, watcher: ConfigWatcher):
        self._watcher = watcher
        self._lock = threading.Lock()
        self._job: dict[str, Any] | None = None
        self.logs_dir: Path | None = None

    @property
    def status(self) -> dict[str, Any] | None:
        """State of the most recent job (never cleared, replaced on next start)."""
        with self._lock:
            job = self._job
            if not job:
                return None
            returncode = job["proc"].poll()
            return {
                "step": job["step"],
                "started_at": job["started_at"],
                "done": returncode is not None,
                "returncode": returncode,
            }

    def start(self, step: str, params: dict[str, Any] | None = None) -> tuple[bool, str]:
        if step not in STEPS:
            return False, f"unknown step: {step!r}"
        try:
            extra = _step_args(step, params)
        except (ValueError, TypeError) as exc:
            return False, f"invalid parameters: {exc}"
        config = self._watcher.get()
        if config is None:
            return False, f"no valid config yet ({self._watcher.error or 'missing'}) — complete the setup first"
        with self._lock:
            current = self._job
            if current and current["proc"].poll() is None:
                return False, f"still running: {current['step']}"
            logs_dir = config.db_path.parent / "logs"
            if self.logs_dir is None or self.logs_dir != logs_dir:
                self.logs_dir = logs_dir
                logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.logs_dir / f"{step}.log"
            log_file = log_path.open("w", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-m", "chess_analysis", step, *STEP_ARGS.get(step, []), *extra],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env={
                    "CHESS_ANALYSIS_CONFIG": str(self._watcher.path),
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/tmp",
                    "PYTHONUNBUFFERED": "1",
                },
            )
            self._job = {
                "step": step,
                "proc": proc,
                "log_path": log_path,
                "log_file": log_file,
                "started_at": time.time(),
            }
            return True, f"started: {step}" + (f" ({' '.join(extra)})" if extra else "")

    def tail(self) -> str:
        with self._lock:
            job = self._job
        if not job:
            return ""
        try:
            text = Path(job["log_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-LOG_TAIL_CHARS:]


def _db_stats(config: AppConfig) -> dict[str, Any]:
    try:
        conn = open_db(config.db_path)
    except Exception:  # noqa: BLE001 — the UI must work before the first fetch
        return {"games": None, "analyzed": None, "book": None}
    try:
        return {
            "games": conn.execute("SELECT COUNT(*) FROM games").fetchone()[0],
            "analyzed": conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0],
            "book": conn.execute("SELECT COUNT(*) FROM opening_book").fetchone()[0],
        }
    finally:
        conn.close()


def _reports(config: AppConfig) -> list[dict[str, Any]]:
    out = []
    if config.reports_dir.is_dir():
        for p in sorted(config.reports_dir.glob("*.html")):
            out.append(
                {
                    "name": p.name,
                    "url": f"/reports/{p.name}",
                    "size_kb": round(p.stat().st_size / 1024, 1),
                    "mtime": int(p.stat().st_mtime),
                }
            )
    return out


def _viewer_games(config: AppConfig, time_class: str | None = None, color: str | None = None) -> list[dict[str, Any]]:
    """Analyzed games for the viewer list, newest first (read-only query).

    Optional server-side filters: time class (bullet/blitz/rapid/daily) and color.
    """
    conn = open_db(config.db_path)
    clauses = ["g.parse_error IS NULL"]
    params: list[Any] = []
    if time_class:
        clauses.append("g.time_class = ?")
        params.append(time_class)
    if color:
        clauses.append("g.my_color = ?")
        params.append(color)
    try:
        rows = conn.execute(
            f"""
            SELECT g.uuid, g.end_time, g.my_color, g.opponent, g.my_result,
                   g.time_class, g.month, g.white_rating, g.black_rating,
                   a.accuracy_white, a.accuracy_black, a.blunders
            FROM games g JOIN analysis a ON a.game_uuid = g.uuid
            WHERE {" AND ".join(clauses)}
            ORDER BY g.end_time DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        my_color = r["my_color"]
        mistakes = sum(
            1 for b in json.loads(r["blunders"]) if b.get("side") == my_color
        )
        out.append(
            {
                "uuid": r["uuid"],
                "end_time": r["end_time"],
                "month": r["month"],
                "color": my_color,
                "opponent": r["opponent"],
                "result": r["my_result"],
                "time_class": r["time_class"],
                "rating": r["white_rating"] if my_color == "white" else r["black_rating"],
                "accuracy": r["accuracy_white"] if my_color == "white" else r["accuracy_black"],
                "mistakes": mistakes,
            }
        )
    return out


def _viewer_game(config: AppConfig, uuid: str) -> dict[str, Any] | None:
    """One analyzed game with its per-move review data (read-only query)."""
    conn = open_db(config.db_path)
    try:
        r = conn.execute(
            """
            SELECT g.uuid, g.end_time, g.month, g.white, g.black, g.white_rating,
                   g.black_rating, g.result, g.time_class, g.pgn,
                   g.my_color, g.my_result, g.opponent,
                   a.accuracy_white, a.accuracy_black, a.moves, a.analyzed_at
            FROM games g JOIN analysis a ON a.game_uuid = g.uuid
            WHERE g.uuid = ? AND g.parse_error IS NULL
            """,
            (uuid,),
        ).fetchone()
        if r is None:
            return None
        link = ""
        for line in r["pgn"].splitlines():
            if line.startswith("[Link "):
                link = line.split('"')[1] if '"' in line else ""
                break
        return {
            "uuid": r["uuid"],
            "end_time": r["end_time"],
            "month": r["month"],
            "white": r["white"],
            "black": r["black"],
            "white_rating": r["white_rating"],
            "black_rating": r["black_rating"],
            "result": r["result"],
            "time_class": r["time_class"],
            "color": r["my_color"],
            "my_result": r["my_result"],
            "opponent": r["opponent"],
            "link": link if link.startswith("http") else "",
            "accuracy_white": r["accuracy_white"],
            "accuracy_black": r["accuracy_black"],
            "analyzed_at": r["analyzed_at"],
            "moves": json.loads(r["moves"]),
        }
    finally:
        conn.close()


def _collect_mistakes(config: AppConfig, *, opening_only: bool = False) -> dict[str, Any]:
    """All of the user's own mistakes with the position before each one
    (read-only). `fen_before` is what makes the insights page visual without
    chess notation: the client renders it as a clickable mini-board that jumps
    into the game viewer. Aggregations happen client-side (single source of
    truth, mirrors the mistake-pattern report).

    With `opening_only`, just the opening-phase mistakes (plies 1..20), each
    enriched with its game's opening context (name, ECO code, class, variation).
    """
    conn = open_db(config.db_path)
    try:
        rows = conn.execute(
            """
            SELECT g.uuid, g.month, g.my_color, g.time_class, g.end_time,
                   g.white_rating, g.black_rating, g.pgn,
                   g.opening_code, g.opening_name, g.opening_class, g.variation_key,
                   a.moves, a.blunders
            FROM games g JOIN analysis a ON a.game_uuid = g.uuid
            WHERE g.parse_error IS NULL
            """
        ).fetchall()
        labels = {
            row["theme_key"]: row["label"]
            for row in conn.execute("SELECT theme_key, label FROM pattern_labels WHERE label IS NOT NULL")
        }
    finally:
        conn.close()

    mistakes: list[dict[str, Any]] = []
    moves_by_color_tc: Counter = Counter()
    for r in rows:
        my_color = r["my_color"]
        moves = json.loads(r["moves"])
        moves_by_color_tc[(my_color, r["time_class"])] += sum(1 for m in moves if m["side"] == my_color)
        blunders = [b for b in json.loads(r["blunders"]) if b.get("side") == my_color]
        if opening_only:
            blunders = [b for b in blunders if b.get("ply", 99) <= OPENING_PLIES]
        if not blunders:
            continue
        rating = r["white_rating"] if my_color == "white" else r["black_rating"]
        opp_rating = r["black_rating"] if my_color == "white" else r["white_rating"]
        # one PGN replay per game: position (fen) before each mistake
        fens: dict[int, str] = {}
        game = chess.pgn.read_game(io.StringIO(r["pgn"]))
        if game is not None:
            board = game.board()
            wanted = {b["ply"] for b in blunders}
            for i, move in enumerate(game.mainline_moves(), start=1):
                if i in wanted:
                    fens[i] = board.fen()
                board.push(move)
        for b in blunders:
            meta = b.get("meta") or {}
            entry = {
                "game_uuid": r["uuid"],
                "ply": b["ply"],
                "side": my_color,
                "month": r["month"],
                "time_class": r["time_class"],
                "end_time": r["end_time"],
                "rating": rating,
                "opponent_rating": opp_rating,
                "classification": b.get("classification"),
                "san": b.get("san"),
                "uci": b.get("uci", ""),
                "drop": b.get("drop", 0.0),
                "clock_s": meta.get("clock_s", b.get("clock_s")),
                "fen_before": fens.get(b["ply"], ""),
                "motifs": meta.get("motifs", []),
                "position_type": meta.get("position_type"),
                "material_balance": meta.get("material_balance"),
            }
            if opening_only:
                entry.update(
                    {
                        "opening_code": r["opening_code"],
                        "opening_name": r["opening_name"],
                        "opening_class": r["opening_class"],
                        "variation_key": r["variation_key"],
                    }
                )
            mistakes.append(entry)
    return {
        "username": config.username,
        "time_trouble_s": TIME_TROUBLE_S,
        "mistakes": mistakes,
        "labels": labels,
        "moves_by_color_tc": {f"{c}|{tc}": n for (c, tc), n in moves_by_color_tc.items()},
    }


def _viewer_mistakes(config: AppConfig) -> dict[str, Any]:
    return _collect_mistakes(config)


def _opening_mistakes(config: AppConfig) -> dict[str, Any]:
    return _collect_mistakes(config, opening_only=True)


def _save_theme_label(config: AppConfig, key: str, label: str) -> bool:
    """Upsert (or clear, when label is empty) a recurring-pattern label.
    A single-row write — safe alongside running jobs (WAL + busy_timeout)."""
    if not key or len(key) > LABEL_MAX_LEN or len(label) > LABEL_MAX_LEN:
        return False
    conn = connect(config.db_path)
    try:
        if label:
            conn.execute(
                """
                INSERT INTO pattern_labels (theme_key, label) VALUES (?, ?)
                ON CONFLICT(theme_key) DO UPDATE SET label = excluded.label
                """,
                (key, label),
            )
        else:
            conn.execute("DELETE FROM pattern_labels WHERE theme_key = ?", (key,))
        conn.commit()
    finally:
        conn.close()
    return True


class EngineConsultant:
    """One shared Stockfish process for on-demand best-move questions.

    Spawned lazily on the first /api/best request and reused; a lock serializes
    access (the engine and its private asyncio loop are not thread-safe across
    HTTP handler threads). Answers are persisted in `engine_advice` — one
    consultation per position, invalidated on engine or budget change.

    The config comes from the ConfigWatcher on every call: when the settings
    page changes engine path, threads, hash, or budget, the shared engine is
    respawned with the new values (and the new budget invalidates the cache).
    """

    def __init__(self, watcher: ConfigWatcher, engine_factory: Any = None):
        self._watcher = watcher
        self._lock = threading.Lock()
        self._engine: Any = None
        self._engine_key: tuple | None = None
        self._factory = engine_factory

    def _spawn(self, config: AppConfig) -> Any:
        if self._factory is not None:
            return self._factory(config.engine_path, threads=config.threads, hash_mb=config.hash_mb)
        from .engine import Engine

        return Engine(config.engine_path, threads=config.threads, hash_mb=config.hash_mb)

    @staticmethod
    def _format_line(board: chess.Board, info: dict) -> dict:
        """One MultiPV line: move + eval + a short SAN preview of the line."""
        pv_uci = info.get("pv") or []
        b = board.copy(stack=False)
        pv_san: list[str] = []
        for u in pv_uci[:PV_SAN_LIMIT]:
            try:
                move = chess.Move.from_uci(u)
            except ValueError:
                break
            if move not in b.legal_moves:
                break
            pv_san.append(b.san(move))
            b.push(move)
        cp = info["cp"]
        return {
            "uci": pv_uci[0] if pv_uci else "",
            "san": pv_san[0] if pv_san else "",
            "cp": cp,
            "mate": info.get("mate"),
            "winp": round(win_percent(cp), 2),
            "pv_san": pv_san,
        }

    @staticmethod
    def _advice_signature(config: AppConfig, multipv: int) -> str:
        """Cache key part: budget + multipv (engine version compared separately,
        it is only known once the engine is spawned)."""
        return f"budget={config.eval_ms}ms-depth={config.eval_depth}-multipv={multipv}"

    def best_moves(self, fen: str, multipv: int) -> dict[str, Any] | None:
        """Best moves for a FEN, cached in `engine_advice` (invalidated by
        engine version or budget change). Returns None for an invalid FEN."""
        try:
            board = chess.Board(fen)
        except ValueError:
            return None
        if not board.legal_moves:
            return {"engine": "", "game_over": True, "lines": []}
        config = self._watcher.get()
        if config is None:
            return {"error": "engine unavailable: no valid config", "game_over": False, "lines": []}
        with self._lock:
            try:
                engine_key = (config.engine_path, config.threads, config.hash_mb)
                if self._engine_key != engine_key or self._engine is None:
                    if self._engine is not None:
                        try:
                            self._engine.close()
                        except Exception:  # noqa: BLE001 — engine may already be dead
                            pass
                    self._engine = self._spawn(config)
                    self._engine_key = engine_key
                engine_name = self._engine.name
                signature = self._advice_signature(config, multipv)
                cached = self._read_cache(config, fen, engine_name, signature)
                if cached is not None:
                    return cached
                infos = self._engine.evaluate(
                    board, eval_ms=config.eval_ms, eval_depth=config.eval_depth, multipv=multipv
                )
            except (OSError, RuntimeError) as exc:
                return {"error": f"engine unavailable: {exc}", "game_over": False, "lines": []}
            lines = [self._format_line(board, info) for info in infos if info.get("pv")]
            result = {"engine": engine_name, "game_over": False, "lines": lines}
            if lines:
                self._write_cache(config, fen, engine_name, signature, multipv, result)
            return result

    def _read_cache(self, config: AppConfig, fen: str, engine_name: str, signature: str) -> dict[str, Any] | None:
        try:
            conn = connect(config.db_path)
        except sqlite3.Error:
            return None
        try:
            row = conn.execute(
                "SELECT payload FROM engine_advice WHERE fen = ? AND engine_version = ? AND signature = ?",
                (fen, engine_name, signature),
            ).fetchone()
            return json.loads(row["payload"]) if row else None
        except (sqlite3.Error, json.JSONDecodeError):
            return None
        finally:
            conn.close()

    def _write_cache(self, config: AppConfig, fen: str, engine_name: str, signature: str, multipv: int, result: dict) -> None:
        try:
            conn = connect(config.db_path)
        except sqlite3.Error:
            return
        try:
            conn.execute(
                """
                INSERT INTO engine_advice (fen, engine_version, signature, multipv, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fen) DO UPDATE SET
                    engine_version = excluded.engine_version,
                    signature = excluded.signature,
                    multipv = excluded.multipv,
                    payload = excluded.payload,
                    created_at = excluded.created_at
                """,
                (
                    fen,
                    engine_name,
                    signature,
                    multipv,
                    json.dumps(result),
                    time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                ),
            )
            conn.commit()
        except sqlite3.Error:
            pass  # cache write failure must never break the answer
        finally:
            conn.close()

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                try:
                    self._engine.close()
                except Exception:  # noqa: BLE001 — engine may already be dead
                    pass
                self._engine = None
                self._engine_key = None


def make_handler(
    watcher: ConfigWatcher,
    manager: JobManager,
    consultant: EngineConsultant | None = None,
) -> type[BaseHTTPRequestHandler]:
    if consultant is None:
        consultant = EngineConsultant(watcher)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence per-request noise
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

        def _read_json(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ---------------------------------------------------------------- GET

        def do_GET(self):
            path, _, query = self.path.partition("?")
            config = watcher.get()
            if config is None:
                # first-run mode: every page is the setup wizard
                if path == "/api/status":
                    self._json({"setup_required": True, "config_error": watcher.error, "steps": STEPS})
                elif path == "/api/settings":
                    self._json({"setup_required": True, "config": settings_document(watcher.path),
                                "config_error": watcher.error, "schema": schema_json()})
                elif path == "/setup":
                    self._send(200, _setup_page_bytes(), "text/html; charset=utf-8")
                elif path.startswith("/api/"):
                    self._json({"error": "setup required"}, code=409)
                else:
                    self._redirect("/setup")
                return
            if path in ("/", "/index.html"):
                self._send(200, _page_bytes(), "text/html; charset=utf-8")
            elif path == "/setup":
                self._send(200, _setup_page_bytes(), "text/html; charset=utf-8")
            elif path == "/settings":
                self._send(200, _settings_page_bytes(), "text/html; charset=utf-8")
            elif path == "/viewer":
                self._send(200, _viewer_page_bytes(), "text/html; charset=utf-8")
            elif path == "/insights":
                self._send(200, _insights_page_bytes(), "text/html; charset=utf-8")
            elif path == "/openings":
                self._send(200, _openings_page_bytes(), "text/html; charset=utf-8")
            elif path == "/api/games":
                qs = parse_qs(query)
                time_class = (qs.get("time_class") or [""])[0].strip()
                color = (qs.get("color") or [""])[0].strip()
                try:
                    self._json(_viewer_games(config, time_class or None, color or None))
                except Exception:  # noqa: BLE001 — UI must work before the first fetch
                    self._json([])
            elif path == "/api/mistakes":
                try:
                    self._json(_viewer_mistakes(config))
                except Exception:  # noqa: BLE001 — UI must work before the first fetch
                    self._json({"username": config.username, "mistakes": [], "labels": {},
                                "moves_by_color_tc": {}})
            elif path == "/api/opening-mistakes":
                try:
                    self._json(_opening_mistakes(config))
                except Exception:  # noqa: BLE001 — UI must work before the first fetch
                    self._json({"username": config.username, "time_trouble_s": TIME_TROUBLE_S,
                                "mistakes": [], "labels": {}, "moves_by_color_tc": {}})
            elif path.startswith("/api/game/"):
                uuid = path[len("/api/game/"):]
                if not UUID_RE.match(uuid):
                    self._send(404, b"not found", "text/plain")
                    return
                game = _viewer_game(config, uuid)
                if game is None:
                    self._send(404, b"not found", "text/plain")
                else:
                    self._json(game)
            elif path == "/api/best":
                qs = parse_qs(query)
                fen = (qs.get("fen") or [""])[0]
                try:
                    multipv = int((qs.get("multipv") or ["3"])[0])
                except ValueError:
                    multipv = 3
                multipv = max(1, min(5, multipv))
                if not fen:
                    self._json({"error": "missing fen"}, code=400)
                    return
                result = consultant.best_moves(fen, multipv)
                if result is None:
                    self._json({"error": "invalid fen"}, code=400)
                elif result.get("error"):
                    self._json(result, code=503)
                else:
                    self._json(result)
            elif path == "/api/status":
                status = manager.status
                self._json(
                    {
                        "setup_required": False,
                        "job": status,
                        "log": manager.tail(),
                        "stats": _db_stats(config),
                        "reports": _reports(config),
                        "steps": STEPS,
                        "step_params": STEP_PARAMS,
                        "config": {
                            "username": config.username,
                            "default_time_class": config.ui.default_time_class,
                        },
                    }
                )
            elif path == "/api/settings":
                self._json(
                    {
                        "setup_required": False,
                        "config": settings_document(watcher.path),
                        "config_error": watcher.error,
                        "schema": schema_json(),
                    }
                )
            elif path.startswith("/reports/"):
                name = Path(path[len("/reports/"):]).name  # basename only
                report = config.reports_dir / name
                if report.is_file() and report.suffix == ".html":
                    self._send(200, report.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        # --------------------------------------------------------------- POST

        def do_POST(self):
            path, _, _ = self.path.partition("?")
            if path == "/api/settings":
                payload = self._read_json()
                if payload is None:
                    self._json({"ok": False, "error": "invalid JSON body"}, code=400)
                    return
                try:
                    watcher.save_settings(payload)
                except (ValueError, OSError) as exc:
                    self._json({"ok": False, "error": str(exc)}, code=400)
                    return
                config = watcher.get()
                self._json(
                    {
                        "ok": True,
                        "config": settings_document(watcher.path),
                        "setup_complete": config is not None,
                    }
                )
            elif path.startswith("/api/run/"):
                step = path[len("/api/run/"):].strip("/")
                payload = self._read_json()
                params = payload.get("params") if payload else None
                if params is not None and not isinstance(params, dict):
                    self._json({"ok": False, "error": "params must be an object"}, code=400)
                    return
                ok, message = manager.start(step, params)
                self._json({"ok": ok, "message": message}, code=200 if ok else 409)
            elif path == "/api/theme-label":
                payload = self._read_json()
                if payload is None:
                    self._json({"ok": False, "error": "invalid JSON body"}, code=400)
                    return
                key = str(payload.get("key") or "").strip()
                label = str(payload.get("label") or "").strip()
                config = watcher.get()
                if config is None:
                    self._json({"ok": False, "error": "no valid config"}, code=409)
                elif not _save_theme_label(config, key, label):
                    self._json({"ok": False, "error": "invalid key or label"}, code=400)
                else:
                    self._json({"ok": True, "key": key, "label": label})
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def _page_bytes() -> bytes:
    return (ASSETS_DIR / "webui_template.html").read_bytes()


def _setup_page_bytes() -> bytes:
    return (ASSETS_DIR / "setup_template.html").read_bytes()


def _settings_page_bytes() -> bytes:
    return (ASSETS_DIR / "settings_template.html").read_bytes()


def _viewer_page_bytes() -> bytes:
    """Game viewer page with the vendored chess.js inlined (cached)."""
    page = getattr(_viewer_page_bytes, "_cache", None)
    if page is None:
        template = (ASSETS_DIR / "game_viewer_template.html").read_text(encoding="utf-8")
        chessjs = (ASSETS_DIR / "chess.min.js").read_text(encoding="utf-8")
        page = template.replace("{{chessjs}}", chessjs).encode("utf-8")
        _viewer_page_bytes._cache = page  # type: ignore[attr-defined]
    return page


def _insights_page_bytes() -> bytes:
    """Mistake-insights page with the vendored Chart.js inlined (cached)."""
    page = getattr(_insights_page_bytes, "_cache", None)
    if page is None:
        template = (ASSETS_DIR / "insights_template.html").read_text(encoding="utf-8")
        chartjs = (ASSETS_DIR / "chart.umd.js").read_text(encoding="utf-8")
        page = template.replace("{{chartjs}}", chartjs).encode("utf-8")
        _insights_page_bytes._cache = page  # type: ignore[attr-defined]
    return page


def _openings_page_bytes() -> bytes:
    """Opening-blunders page with the vendored chess.js inlined (cached)."""
    page = getattr(_openings_page_bytes, "_cache", None)
    if page is None:
        template = (ASSETS_DIR / "opening_blunders_template.html").read_text(encoding="utf-8")
        chessjs = (ASSETS_DIR / "chess.min.js").read_text(encoding="utf-8")
        page = template.replace("{{chessjs}}", chessjs).encode("utf-8")
        _openings_page_bytes._cache = page  # type: ignore[attr-defined]
    return page


def serve(host: str, port: int, config_path: Path) -> None:
    """Start the web UI HTTP server (blocks).

    Works without a config file: the UI then serves the first-run setup wizard
    (Story 09) instead of crashing; once the wizard writes the config, the
    normal pages come up without a restart.
    """
    watcher = ConfigWatcher(Path(config_path))
    manager = JobManager(watcher)
    consultant = EngineConsultant(watcher)
    httpd = ThreadingHTTPServer((host, port), make_handler(watcher, manager, consultant))
    if watcher.get() is None:
        print(f"chess-analysis web UI: http://{host}:{port} — no config found, open /setup to get started", flush=True)
    else:
        print(f"chess-analysis web UI: http://{host}:{port} (config: {config_path})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    finally:
        consultant.close()
