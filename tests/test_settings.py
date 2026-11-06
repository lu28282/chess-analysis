"""Story 08/09 tests: settings schema, TOML writing, config validation."""

from __future__ import annotations

import tomllib

import pytest

from chess_analysis.config import validate_since
from chess_analysis.settings import SCHEMA, schema_json, settings_document, update_settings


def test_settings_document_merges_defaults(tmp_path):
    path = tmp_path / "config.toml"
    doc = settings_document(path)  # missing file -> pure defaults
    assert doc["username"] == ""
    assert doc["workers"] == 0
    assert doc["fetch"]["months"] == 0
    assert doc["thresholds"]["blunder"] == 40.0

    path.write_text('username = "alice"\n[fetch]\nmonths = 3\n', encoding="utf-8")
    doc = settings_document(path)
    assert doc["username"] == "alice"
    assert doc["fetch"]["months"] == 3
    assert doc["fetch"]["max_games"] == 0  # default fills the gap


def test_update_settings_creates_full_config(tmp_path):
    """The setup wizard writes a username-only payload; defaults fill the rest."""
    path = tmp_path / "config.toml"
    doc = update_settings(path, {"username": "alice"})
    assert doc["username"] == "alice"
    assert doc["db_path"]  # a default db path was persisted
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["username"] == "alice"
    assert raw["eval_ms"] == 1000
    # and the written file loads as a valid AppConfig
    from chess_analysis.config import load_config

    cfg = load_config(path)
    assert cfg.username == "alice"
    assert cfg.ui.default_time_class == ""


def test_update_settings_partial_overlay_keeps_other_values(tmp_path):
    path = tmp_path / "config.toml"
    update_settings(path, {"username": "alice", "eval_ms": 500, "threads": 2})
    # a later partial save must not clobber unrelated keys
    update_settings(path, {"thresholds": {"blunder": 30.0}})
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["eval_ms"] == 500
    assert raw["threads"] == 2
    assert raw["thresholds"]["blunder"] == 30.0
    assert raw["thresholds"]["mistake"] == 20.0  # untouched sibling kept


def test_update_settings_rejects_bad_input_without_writing(tmp_path):
    path = tmp_path / "config.toml"
    update_settings(path, {"username": "alice"})
    before = path.read_text(encoding="utf-8")

    for bad in (
        {"username": "alice", "nope": 1},               # unknown key
        {"username": "alice", "eval_ms": "fast"},      # not an integer
        {"username": "alice", "workers": -1},          # invalid range
        {"username": "alice", "fetch": {"since": "2024-13"}},  # bad month
        {"username": "alice", "ui": {"default_time_class": "hyperbullet"}},
        {"username": ""},                               # empty username
    ):
        with pytest.raises(ValueError):
            update_settings(path, bad)
    assert path.read_text(encoding="utf-8") == before  # never touched on failure


def test_update_settings_repair_partial_config(tmp_path):
    """A username-only file (previously invalid) is completed by a settings save."""
    path = tmp_path / "config.toml"
    path.write_text('username = "bob"\n', encoding="utf-8")
    update_settings(path, {"eval_ms": 200})
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["username"] == "bob"
    assert raw["eval_ms"] == 200


def test_schema_json_is_serializable():
    import json

    doc = schema_json()
    json.dumps(doc)  # must not raise
    assert doc["username"] == "string"
    assert doc["workers"] == "integer"
    assert doc["thresholds"]["brilliant"] == "number"
    assert doc["fetch"]["since"] == "string"
    assert set(SCHEMA) == set(doc)


def test_validate_since():
    assert validate_since("") == ""
    assert validate_since("2024-01") == "2024-01"
    assert validate_since(" 2024-01-15 ") == "2024-01-15"
    for bad in ("2024-13", "24-01", "2024-1", "january", "2024-01-32"):
        with pytest.raises(ValueError):
            validate_since(bad)


def test_workers_auto_resolution(tmp_path):
    import os

    from chess_analysis.config import load_config

    path = tmp_path / "config.toml"
    update_settings(path, {"username": "alice"})  # workers default 0 = auto
    cfg = load_config(path)
    assert cfg.workers == 0
    assert cfg.resolved_workers == max(1, os.cpu_count() or 1)


# --------------------------------------------- review findings: hardening

def test_corrupt_toml_is_repairable(tmp_path):
    """An unparseable file must not crash the settings page; saving repairs it."""
    path = tmp_path / "config.toml"
    path.write_text('username = "alice"\nthis is [not toml', encoding="utf-8")
    doc = settings_document(path)  # GET /api/settings must not raise
    assert doc["username"] == ""  # unreadable file -> pure defaults for the form
    update_settings(path, {"username": "alice"})  # POST repairs the file
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["username"] == "alice"


def test_non_finite_and_typed_values_rejected(tmp_path):
    path = tmp_path / "config.toml"
    update_settings(path, {"username": "alice"})
    before = path.read_text(encoding="utf-8")
    for bad in (
        {"workers": 1e999},                       # JSON inf -> OverflowError path
        {"eval_ms": "nan"},
        {"username": {"nested": "object"}},       # non-scalar into a string field
        {"workers": True},                        # boolean
        {"thresholds": {"best": float("inf")}},
    ):
        with pytest.raises(ValueError):
            update_settings(path, bad)
    assert path.read_text(encoding="utf-8") == before


def test_write_config_preserves_file_mode(tmp_path):
    import os
    import stat

    path = tmp_path / "config.toml"
    update_settings(path, {"username": "alice"})
    os.chmod(path, 0o644)
    update_settings(path, {"eval_ms": 300})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644


def test_engine_budget_ranges_validated(tmp_path):
    path = tmp_path / "config.toml"
    update_settings(path, {"username": "alice"})
    before = path.read_text(encoding="utf-8")
    for bad in (
        {"eval_ms": 0},
        {"eval_ms": -100},
        {"multipv": 0},
        {"multipv": 6},
        {"threads": 0},
        {"hash_mb": 0},
        {"engine_path": "  "},
    ):
        with pytest.raises(ValueError):
            update_settings(path, bad)
    assert path.read_text(encoding="utf-8") == before
