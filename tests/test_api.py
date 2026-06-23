"""v3 — dashboard HTTP API (§6/§7). Compute/DB layers mocked."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.api import dashboard
from schemas.verdict import VerdictHistory, VerdictState

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _no_db_gate(monkeypatch):
    # Make the endpoints hermetic — don't depend on a configured DATABASE_URL.
    monkeypatch.setattr(dashboard, "_require_db", lambda: None)


def _state(window=15):
    return VerdictState(ts=datetime(2026, 6, 19, 10, 0, tzinfo=IST),
                        trading_date=date(2026, 6, 19), weekday="Fri",
                        window_minutes=window, sides=[], note="x")


# --- /state ----------------------------------------------------------------

def test_state_passes_window_through(client, monkeypatch):
    captured = {}

    def fake(trading_date=None, window_minutes=15):
        captured["window"] = window_minutes
        return _state(window_minutes)

    monkeypatch.setattr(dashboard, "build_state", fake)
    resp = client.get("/state?window=30")
    assert resp.status_code == 200
    assert resp.get_json()["weekday"] == "Fri"
    assert captured["window"] == 30


def test_state_invalid_window_falls_back_to_default(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard, "build_state",
                        lambda trading_date=None, window_minutes=15: captured.update(w=window_minutes) or _state())
    client.get("/state?window=99")
    assert captured["w"] == 15


# --- /history --------------------------------------------------------------

def test_history_returns_payload(client, monkeypatch):
    monkeypatch.setattr(dashboard, "build_history",
                        lambda s, e, side=None: VerdictHistory(start=s, end=e, records=[], weekday_buckets=[]))
    resp = client.get("/history")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "records" in body and "weekday_buckets" in body


def test_history_rejects_bad_side(client):
    assert client.get("/history?side=BOGUS").status_code == 400


def test_history_accepts_cap_side(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(dashboard, "build_history",
                        lambda s, e, side=None: seen.update(side=side) or VerdictHistory(records=[], weekday_buckets=[]))
    assert client.get("/history?side=CAP").status_code == 200
    assert seen["side"] == "CAP"


def test_history_defaults_to_two_week_range(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(dashboard, "build_history",
                        lambda s, e, side=None: seen.update(start=s, end=e) or VerdictHistory(records=[], weekday_buckets=[]))
    client.get("/history?end=2026-06-19")
    assert seen["end"] == date(2026, 6, 19) and seen["start"] == date(2026, 6, 5)


# --- no /zones or /set-zones in v3 (spot-anchored, no manual input) ---------

def test_zones_routes_are_gone(client):
    assert client.get("/zones").status_code == 404
    assert client.post("/set-zones", json={"zones": []}).status_code == 404


# --- /tick -----------------------------------------------------------------

def test_tick_skips_outside_market_hours(client, monkeypatch):
    import market.tick as mt
    monkeypatch.setattr(mt, "is_market_hours", lambda now=None: False)
    body = client.get("/tick").get_json()
    assert body["skipped"] is True and "market hours" in body["reason"]


def test_tick_skips_on_minute_dedup(client, monkeypatch):
    import market.tick as mt
    monkeypatch.setattr(mt, "is_market_hours", lambda now=None: True)
    monkeypatch.setattr(mt, "already_ticked_this_minute", lambda now=None: True)
    body = client.get("/tick").get_json()
    assert body["skipped"] is True and "minute" in body["reason"]


def test_tick_runs_pipeline_when_forced(client, monkeypatch):
    import market.tick as mt
    monkeypatch.setattr(mt, "run_tick",
                        lambda strikecount=10, window_minutes=15: {"trading_date": "2026-06-19", "verdicts_written": 2})
    resp = client.get("/tick?force=true")
    assert resp.status_code == 200 and resp.get_json()["verdicts_written"] == 2


def test_tick_returns_503_on_pipeline_error(client, monkeypatch):
    import market.tick as mt
    monkeypatch.setattr(mt, "run_tick", lambda strikecount=10, window_minutes=15: {"error": "auth: nope"})
    resp = client.get("/tick?force=true")
    assert resp.status_code == 503 and "error" in resp.get_json()
