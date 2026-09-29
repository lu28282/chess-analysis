"""SQLite access layer: schema versioning, migrations, and query helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- Story 2: fetcher schema v1
        CREATE TABLE IF NOT EXISTS games (
            uuid TEXT PRIMARY KEY,
            month TEXT NOT NULL,
            end_time INTEGER,
            pgn TEXT NOT NULL,
            white TEXT,
            black TEXT,
            white_rating INTEGER,
            black_rating INTEGER,
            result TEXT,
            time_control TEXT,
            time_class TEXT,
            rules TEXT,
            eco TEXT,
            accuracies TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_games_end_time ON games(end_time);
        CREATE TABLE IF NOT EXISTS sync_state (
            month_url TEXT PRIMARY KEY,
            etag TEXT,
            last_modified TEXT,
            last_synced_at TEXT
        );
        """,
    ),
    (
        2,
        """
        -- Story 3: ingestion / derived columns
        ALTER TABLE games ADD COLUMN parse_error TEXT;
        ALTER TABLE games ADD COLUMN my_color TEXT;
        ALTER TABLE games ADD COLUMN my_result TEXT;
        ALTER TABLE games ADD COLUMN opponent TEXT;
        ALTER TABLE games ADD COLUMN rating_delta INTEGER;
        ALTER TABLE games ADD COLUMN opening_code TEXT;
        ALTER TABLE games ADD COLUMN opening_name TEXT;
        ALTER TABLE games ADD COLUMN move_count INTEGER;
        ALTER TABLE games ADD COLUMN result_parsed TEXT;
        CREATE INDEX IF NOT EXISTS idx_games_query
            ON games(time_class, my_color, my_result, opponent);
        """,
    ),
    (
        3,
        """
        -- Story 4: analysis results, cached per game
        CREATE TABLE IF NOT EXISTS analysis (
            game_uuid TEXT PRIMARY KEY REFERENCES games(uuid) ON DELETE CASCADE,
            engine_version TEXT NOT NULL,
            signature TEXT NOT NULL,
            input_checksum TEXT NOT NULL,
            analyzed_at TEXT NOT NULL,
            accuracy_white REAL,
            accuracy_black REAL,
            phases TEXT NOT NULL,
            moves TEXT NOT NULL,
            blunders TEXT NOT NULL
        );
        """,
    ),
    (
        4,
        """
        -- Story 6: opening classification + reference book
        ALTER TABLE games ADD COLUMN opening_class TEXT;
        ALTER TABLE games ADD COLUMN variation_key TEXT;
        ALTER TABLE games ADD COLUMN book_off_ply INTEGER;
        ALTER TABLE games ADD COLUMN book_off_side TEXT;
        CREATE INDEX IF NOT EXISTS idx_games_opening_class ON games(opening_class);
        CREATE TABLE IF NOT EXISTS opening_book (
            opening TEXT NOT NULL,
            fen TEXT NOT NULL,
            move_uci TEXT NOT NULL,
            move_san TEXT NOT NULL,
            ply INTEGER NOT NULL,
            white INTEGER NOT NULL,
            draw INTEGER NOT NULL,
            black INTEGER NOT NULL,
            total INTEGER NOT NULL,
            PRIMARY KEY (opening, fen, move_uci)
        );
        """,
    ),
    (
        5,
        """
        -- Story 5: human labels for recurring mistake themes
        CREATE TABLE IF NOT EXISTS pattern_labels (
            theme_key TEXT PRIMARY KEY,
            label TEXT
        );
        """,
    ),
    (
        6,
        """
        -- Story 3: phase_marks placeholder (opening/middlegame/endgame split happens in Story 4)
        ALTER TABLE games ADD COLUMN phase_marks TEXT;
        """,
    ),
    (
        7,
        """
        -- Story 6: provenance of each opening-book edge
        ALTER TABLE opening_book ADD COLUMN source TEXT NOT NULL DEFAULT 'lichess';
        """,
    ),
    (
        8,
        """
        -- "Ask Stockfish" answer cache: one stored engine consultation per
        -- position; invalidated by engine version or budget signature change.
        CREATE TABLE IF NOT EXISTS engine_advice (
            fen TEXT PRIMARY KEY,
            engine_version TEXT NOT NULL,
            signature TEXT NOT NULL,
            multipv INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """,
    ),
]

SCHEMA_VERSION_MAX = MIGRATIONS[-1][0]


def _split_statements(script: str) -> list[str]:
    """Split a migration script into complete SQL statements (semicolons inside
    string literals or triggers are respected via sqlite3.complete_statement)."""
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Parallel analyze workers write from separate connections; wait instead
    # of failing with SQLITE_BUSY on short write contention.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply all pending numbered migrations; safe to call on every startup.

    Each migration runs in one transaction (statements executed individually,
    committed together with its schema_version row), so a crash mid-migration
    leaves the DB at the previous version and it is re-run cleanly.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] or 0
    for version, script in MIGRATIONS:
        if version <= current:
            continue
        statements = _split_statements(script)
        conn.execute("BEGIN")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.commit()


def open_db(db_path: Path) -> sqlite3.Connection:
    """Connect and migrate in one step."""
    conn = connect(db_path)
    migrate(conn)
    return conn


def query_games(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    color: str | None = None,
    result: str | None = None,
    opponent: str | None = None,
    time_class: str | None = None,
    opening: str | None = None,
    opening_class: str | None = None,
    parse_ok_only: bool = True,
) -> list[sqlite3.Row]:
    """Filter games by date range (YYYY-MM-DD), color, result, opponent, time class, opening."""
    clauses, params = [], []
    if date_from:
        clauses.append("end_time >= unixepoch(?)")
        params.append(f"{date_from} 00:00:00")
    if date_to:
        clauses.append("end_time < unixepoch(?)")
        params.append(f"{date_to} 24:00:00")
    if color:
        clauses.append("my_color = ?")
        params.append(color)
    if result:
        clauses.append("my_result = ?")
        params.append(result)
    if opponent:
        clauses.append("lower(opponent) = lower(?)")
        params.append(opponent)
    if time_class:
        clauses.append("time_class = ?")
        params.append(time_class)
    if opening:
        clauses.append("opening_name LIKE ?")
        params.append(f"%{opening}%")
    if opening_class:
        clauses.append("opening_class = ?")
        params.append(opening_class)
    if parse_ok_only:
        clauses.append("parse_error IS NULL")
    sql = "SELECT * FROM games"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY end_time"
    return conn.execute(sql, params).fetchall()
