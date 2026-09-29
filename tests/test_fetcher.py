"""Story 2 tests: fetcher — serial PubAPI client, ETag caching, backoff, upserts."""

from __future__ import annotations

from chess_analysis.fetcher import PubApiClient
from chess_analysis.pipeline import run_fetch
from tests.conftest import make_archive_game

ARCHIVES_URL = "https://api.chess.com/pub/player/testuser/games/archives"
MONTH_URL = "https://api.chess.com/pub/player/testuser/games/2024/04"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_session(monkeypatch, responses, calls=None):
    def fake_get(self, url, headers=None, timeout=None):
        if calls is not None:
            calls.append((url, headers))
        return responses.pop(0)

    monkeypatch.setattr("requests.Session.get", fake_get)


def test_fetch_all_months_and_games(db, config, monkeypatch):
    games = [make_archive_game("uuid-1"), make_archive_game("uuid-2", result="0-1")]
    responses = [
        FakeResponse(200, {"archives": [MONTH_URL]}),
        FakeResponse(200, {"games": games}),
    ]
    _patch_session(monkeypatch, responses)
    stats = run_fetch(db, config)
    assert stats.months_fetched == 1
    assert stats.games_stored == 2
    assert db.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 2
    # derived fields were populated by the pipeline
    row = db.execute("SELECT * FROM games WHERE uuid='uuid-1'").fetchone()
    assert row["my_color"] == "white"
    assert row["my_result"] == "win"
    assert row["opponent"] == "opponent"
    assert row["rating_delta"] == -100


def test_refetch_with_304_stores_nothing(db, config, monkeypatch):
    responses = [
        FakeResponse(200, {"archives": [MONTH_URL]}),
        FakeResponse(200, {"games": [make_archive_game("uuid-1")]},
                     headers={"ETag": '"abc"', "Last-Modified": "Wed, 01 May 2024 00:00:00 GMT"}),
        FakeResponse(200, {"archives": [MONTH_URL]}),
        FakeResponse(304),
    ]
    _patch_session(monkeypatch, responses)
    run_fetch(db, config)
    before = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    stats2 = run_fetch(db, config)
    after = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    assert before == 1 == after
    assert stats2.months_304 == 1
    assert stats2.games_stored == 0


def test_conditional_headers_sent(db, config, monkeypatch):
    responses = [
        FakeResponse(200, {"archives": [MONTH_URL]}),
        FakeResponse(200, {"games": [make_archive_game("uuid-1")]},
                     headers={"ETag": '"abc"', "Last-Modified": "Wed, 01 May 2024 00:00:00 GMT"}),
        FakeResponse(200, {"archives": [MONTH_URL]}),
        FakeResponse(304),
    ]
    calls = []
    _patch_session(monkeypatch, responses, calls=calls)
    run_fetch(db, config)
    run_fetch(db, config)
    month_calls = [h for url, h in calls if url == MONTH_URL]
    assert len(month_calls) == 2
    assert month_calls[0] == {}  # first fetch has no validators
    assert month_calls[1].get("If-None-Match") == '"abc"'
    assert "If-Modified-Since" in month_calls[1]


def test_changed_month_detects_new_games(db, config, monkeypatch):
    responses = [
        FakeResponse(200, {"archives": [MONTH_URL]}),
        FakeResponse(200, {"games": [make_archive_game("uuid-1")]}, headers={"ETag": '"v1"'}),
        FakeResponse(200, {"archives": [MONTH_URL]}),
        FakeResponse(200, {"games": [make_archive_game("uuid-1"), make_archive_game("uuid-2")]},
                     headers={"ETag": '"v2"'}),
    ]
    _patch_session(monkeypatch, responses)
    run_fetch(db, config)
    stats = run_fetch(db, config)
    assert stats.games_stored == 2  # changed month: both games upserted
    assert db.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 2


def test_404_month_is_noop_not_error(db, config, monkeypatch):
    responses = [
        FakeResponse(200, {"archives": [MONTH_URL]}),
        FakeResponse(404),
    ]
    _patch_session(monkeypatch, responses)
    stats = run_fetch(db, config)
    assert stats.months_missing == 1
    assert stats.games_stored == 0


def test_429_backoff_then_success(monkeypatch):
    client = PubApiClient("test-agent")
    delays = []
    monkeypatch.setattr("time.sleep", lambda s: delays.append(s))
    responses = [FakeResponse(429), FakeResponse(429), FakeResponse(200, {"games": []})]
    _patch_session(monkeypatch, responses)
    status, games, _, _ = client.get_month(MONTH_URL)
    assert status == "ok"
    assert games == []
    assert len(delays) == 2
    assert delays[0] < delays[1]  # exponential


def test_503_backoff_then_success(monkeypatch):
    client = PubApiClient("test-agent")
    monkeypatch.setattr("time.sleep", lambda s: None)
    responses = [FakeResponse(503), FakeResponse(200, {"games": []})]
    _patch_session(monkeypatch, responses)
    status, games, _, _ = client.get_month(MONTH_URL)
    assert status == "ok"
    assert games == []


def test_requests_are_serial():
    """The client is synchronous: each request completes before the next starts."""
    client = PubApiClient("test-agent")
    responses = [FakeResponse(200, {"archives": []})]
    seen = []

    def fake_get(self, url, headers=None, timeout=None):
        seen.append(("start", url))
        assert not any(s == "start" for s, _ in seen[:-1]), "request started while another in flight"
        resp = responses.pop(0)
        seen.append(("end", url))
        return resp

    import requests

    orig = requests.Session.get
    requests.Session.get = fake_get
    try:
        client.get_json("/player/testuser/games/archives")
    finally:
        requests.Session.get = orig
    assert seen == [("start", ARCHIVES_URL), ("end", ARCHIVES_URL)]


def test_user_agent_sent(monkeypatch):
    client = PubApiClient("my-agent")
    captured = {}

    def fake_get(self, url, headers=None, timeout=None):
        captured.update(self.headers)
        return FakeResponse(200, {"archives": []})

    monkeypatch.setattr("requests.Session.get", fake_get)
    client.get_json("/player/testuser/games/archives")
    assert captured.get("User-Agent") == "my-agent"


def test_archives_404_raises_for_bad_username(monkeypatch):
    client = PubApiClient("test-agent")
    responses = [FakeResponse(404)]

    def fake_get(self, url, headers=None, timeout=None):
        return responses.pop(0)

    monkeypatch.setattr("requests.Session.get", fake_get)
    from chess_analysis.fetcher import list_archives

    import pytest

    with pytest.raises(ValueError, match="username"):
        list_archives(client, "nobody-here")


def test_upsert_is_idempotent(db):
    game = make_archive_game("uuid-1")
    from chess_analysis.ingestion import upsert_games

    upsert_games(db, MONTH_URL, [game])
    upsert_games(db, MONTH_URL, [game])
    assert db.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1


def test_sync_state_roundtrip(db):
    from chess_analysis.ingestion import load_sync_state, store_sync_state

    store_sync_state(db, "url", '"e1"', "date")
    state = load_sync_state(db)
    assert state["url"]["etag"] == '"e1"'
