"""Application configuration loaded from a TOML file with sane defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    "workers": 1,
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
    workers: int = 1
    thresholds: Thresholds = field(default_factory=lambda: Thresholds(**DEFAULTS["thresholds"]))
    explorer: ExplorerConfig = field(default_factory=lambda: ExplorerConfig(**DEFAULTS["explorer"]))
    opening: OpeningConfig = field(default_factory=lambda: OpeningConfig(**DEFAULTS["opening"]))

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


def load_config(path: Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"config file not found: {path} (see config/chess-analysis.toml.example)"
        )
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    cfg = _merge(DEFAULTS, raw)
    username = str(cfg["username"]).strip()
    if not username:
        raise ValueError("config key 'username' must be set")
    thresholds = Thresholds(**cfg["thresholds"])
    explorer = ExplorerConfig(**cfg["explorer"])
    opening = OpeningConfig(**cfg["opening"])
    workers = int(cfg["workers"])
    if workers < 1:
        raise ValueError("config key 'workers' must be >= 1")
    return AppConfig(
        username=username,
        db_path=Path(cfg["db_path"]),
        user_agent=str(cfg["user_agent"]),
        reports_dir=Path(cfg["reports_dir"]),
        engine_path=str(cfg["engine_path"]),
        eval_ms=int(cfg["eval_ms"]),
        eval_depth=int(cfg["eval_depth"]),
        multipv=int(cfg["multipv"]),
        threads=int(cfg["threads"]),
        hash_mb=int(cfg["hash_mb"]),
        workers=workers,
        thresholds=thresholds,
        explorer=explorer,
        opening=opening,
    )
