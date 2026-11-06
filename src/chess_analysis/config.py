"""Application configuration loaded from a TOML file with sane defaults."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SINCE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?$")

DEFAULTS: dict[str, Any] = {
    "username": "",
    "db_path": "data/chess-analysis.db",
    "user_agent": "chess-analysis (github.com/chess-analysis)",
    "reports_dir": "reports",
    "engine_path": "/usr/games/stockfish",
    "eval_ms": 1000,
    "eval_depth": 0,
    "multipv": 1,
    "threads": 1,
    "hash_mb": 256,
    # 0 = auto: one analyze worker per CPU core
    "workers": 0,
    "thresholds": {
        "brilliant": 1.0,
        "best": 0.6,
        "good": 5.0,
        "inaccuracy": 10.0,
        "mistake": 20.0,
        "blunder": 40.0,
    },
    "explorer": {
        "endpoint": "https://explorer.lichess.ovh/lichess",
        "max_ply": 12,
        "min_games": 20,
        "top_moves": 12,
        "delay_ms": 700,
        "max_positions": 600,
    },
    "opening": {
        "jobava_max_setup_ply": 12,
        "caro_kann_max_ply": 12,
    },
    # Fetch window: 0 / "" = no limit (all monthly archives)
    "fetch": {
        "months": 0,
        "since": "",
        "max_games": 0,
    },
    # Web UI: "" = all time classes; one of bullet/blitz/rapid/daily
    "ui": {
        "default_time_class": "",
    },
}


def _merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass
class Thresholds:
    brilliant: float
    best: float
    good: float
    inaccuracy: float
    mistake: float
    blunder: float


@dataclass
class ExplorerConfig:
    endpoint: str
    max_ply: int
    min_games: int
    top_moves: int
    delay_ms: int
    max_positions: int


@dataclass
class OpeningConfig:
    jobava_max_setup_ply: int
    caro_kann_max_ply: int


@dataclass
class FetchConfig:
    """Window limiting how much `fetch` downloads (0 / "" = no limit)."""

    months: int
    since: str
    max_games: int


@dataclass
class UIConfig:
    """Web UI preferences."""

    default_time_class: str


TIME_CLASSES = ("bullet", "blitz", "rapid", "daily")


@dataclass
class AppConfig:
    username: str
    db_path: Path
    user_agent: str
    reports_dir: Path
    engine_path: str
    eval_ms: int
    eval_depth: int
    multipv: int
    threads: int
    hash_mb: int
    workers: int = 0
    thresholds: Thresholds = field(default_factory=lambda: Thresholds(**DEFAULTS["thresholds"]))
    explorer: ExplorerConfig = field(default_factory=lambda: ExplorerConfig(**DEFAULTS["explorer"]))
    opening: OpeningConfig = field(default_factory=lambda: OpeningConfig(**DEFAULTS["opening"]))
    fetch: FetchConfig = field(default_factory=lambda: FetchConfig(**DEFAULTS["fetch"]))
    ui: UIConfig = field(default_factory=lambda: UIConfig(**DEFAULTS["ui"]))

    @property
    def resolved_workers(self) -> int:
        """Effective parallel analyze workers: `workers` 0 means auto (all cores)."""
        return self.workers if self.workers > 0 else max(1, os.cpu_count() or 1)

    @property
    def analysis_signature(self) -> str:
        """Identifies the analysis configuration; a change invalidates cached results.

        Covers the engine budget (time, depth, threads, hash) and the
        classification thresholds. The engine binary version is compared
        separately (it is only known once the engine is spawned). The
        `brilliant` threshold is in pawns (required winning margin for a
        brilliancy); all others are win% drop points.
        """
        t = self.thresholds
        return (
            f"budget={self.eval_ms}ms-depth={self.eval_depth}-"
            f"threads={self.threads}-hash={self.hash_mb}-multipv={self.multipv}-"
            f"thr={t.brilliant},{t.best},{t.good},{t.inaccuracy},{t.mistake},{t.blunder}"
        )


def validate_since(since: str) -> str:
    """Normalize a `since` fetch bound: YYYY-MM (preferred) or YYYY-MM-DD."""
    since = since.strip()
    if not since:
        return ""
    if not SINCE_RE.match(since):
        raise ValueError(
            f"fetch bound 'since' must be YYYY-MM (or YYYY-MM-DD), got {since!r}"
        )
    return since


def config_from_dict(cfg: dict[str, Any]) -> AppConfig:
    """Validate a defaults-merged config dict into an AppConfig."""
    username = str(cfg["username"]).strip()
    if not username:
        raise ValueError("config key 'username' must be set")
    engine_path = str(cfg["engine_path"])
    if not engine_path.strip():
        raise ValueError("config key 'engine_path' must not be empty")
    eval_ms = int(cfg["eval_ms"])
    if eval_ms <= 0:
        raise ValueError("config key 'eval_ms' must be > 0")
    eval_depth = int(cfg["eval_depth"])
    if eval_depth < 0:
        raise ValueError("config key 'eval_depth' must be >= 0")
    multipv = int(cfg["multipv"])
    if not 1 <= multipv <= 5:
        raise ValueError("config key 'multipv' must be between 1 and 5")
    threads = int(cfg["threads"])
    if threads < 1:
        raise ValueError("config key 'threads' must be >= 1")
    hash_mb = int(cfg["hash_mb"])
    if hash_mb < 1:
        raise ValueError("config key 'hash_mb' must be >= 1")
    thresholds = Thresholds(**cfg["thresholds"])
    explorer = ExplorerConfig(**cfg["explorer"])
    opening = OpeningConfig(**cfg["opening"])
    fetch = FetchConfig(
        months=int(cfg["fetch"]["months"]),
        since=validate_since(str(cfg["fetch"]["since"])),
        max_games=int(cfg["fetch"]["max_games"]),
    )
    ui = UIConfig(default_time_class=str(cfg["ui"]["default_time_class"]).strip())
    if ui.default_time_class and ui.default_time_class not in TIME_CLASSES:
        raise ValueError(
            f"config key 'ui.default_time_class' must be one of {', '.join(TIME_CLASSES)} or empty"
        )
    workers = int(cfg["workers"])
    if workers < 0:
        raise ValueError("config key 'workers' must be >= 0 (0 = auto)")
    if fetch.months < 0 or fetch.max_games < 0:
        raise ValueError("config keys 'fetch.months' and 'fetch.max_games' must be >= 0")
    return AppConfig(
        username=username,
        db_path=Path(cfg["db_path"]),
        user_agent=str(cfg["user_agent"]),
        reports_dir=Path(cfg["reports_dir"]),
        engine_path=engine_path,
        eval_ms=eval_ms,
        eval_depth=eval_depth,
        multipv=multipv,
        threads=threads,
        hash_mb=hash_mb,
        workers=workers,
        thresholds=thresholds,
        explorer=explorer,
        opening=opening,
        fetch=fetch,
        ui=ui,
    )


def read_config_dict(path: Path) -> dict[str, Any]:
    """The raw user TOML (no defaults merged); {} when the file is missing.

    A file that fails to parse also returns {} — callers (the settings page,
    the repair flow) treat it as "no config yet" and the ConfigWatcher
    surfaces the parse error to the user.
    """
    try:
        with open(Path(path), "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError:
        return {}


def load_config(path: Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"config file not found: {path} (see config/chess-analysis.toml.example)"
        )
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return config_from_dict(_merge(DEFAULTS, raw))
