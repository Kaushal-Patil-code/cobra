"""v3 — history/all-tables read API (app/api/tables.py). Readers mocked."""
import psycopg
import pytest

from app.api import tables


@pytest.fixture(autouse=True)
def _no_db_gate(monkeypatch):
    monkeypatch.setattr(tables, "_require_db", lambda: None)


def _env(items=None):
    return {"items": items or [],
            "page": {"limit": 100, "count": len(items or []), "has_more": False, "next_cursor": None}}


def test_snapshots_passes_filters_through(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(tables, "read_snapshots", lambda **kw: seen.update(kw) or _env())
    r = client.get("/snapshots?index=NIFTY&option_type=CE&strike=23850&limit=50&order=asc")
    assert r.status_code == 200
    assert seen["index"] == "NIFTY" and seen["option_type"] == "CE"
    assert seen["strike"] == 23850 and seen["limit"] == 50 and seen["order"] == "asc"


def test_envelope_passthrough(client, monkeypatch):
    monkeypatch.setattr(tables, "read_snapshots", lambda **kw: _env([{"id": "x", "ts": "t"}]))
    body = client.get("/snapshots").get_json()
    assert "items" in body and "page" in body and body["page"]["has_more"] is False


def test_limit_clamped_to_max(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(tables, "read_snapshots", lambda **kw: seen.update(kw) or _env())
    client.get("/snapshots?limit=999999")
    assert seen["limit"] == 1000


def test_bad_order_400(client):
    assert client.get("/snapshots?order=sideways").status_code == 400


def test_bad_index_400(client):
    assert client.get("/metrics?index=DOW").status_code == 400


def test_bad_side_400(client):
    assert client.get("/walls?side=MIDDLE").status_code == 400


def test_bad_date_400(client):
    assert client.get("/snapshots?start=not-a-date").status_code == 400


def test_bad_cursor_maps_to_400(client, monkeypatch):
    def boom(**kw):
        raise ValueError("invalid cursor")
    monkeypatch.setattr(tables, "read_snapshots", boom)
    assert client.get("/snapshots?cursor=@@@").status_code == 400


def test_db_error_maps_to_503(client, monkeypatch):
    def boom(**kw):
        raise psycopg.OperationalError("down")
    monkeypatch.setattr(tables, "read_metrics", boom)
    assert client.get("/metrics").status_code == 503


def test_walls_and_ladders_routes(client, monkeypatch):
    monkeypatch.setattr(tables, "read_walls", lambda **kw: _env([{"id": "w"}]))
    monkeypatch.setattr(tables, "read_ladders", lambda **kw: _env([{"id": "l"}]))
    assert client.get("/walls?side=CAP&index=NIFTY").status_code == 200
    assert client.get("/ladders?index=SENSEX").status_code == 200


def test_instruments_ok(client, monkeypatch):
    monkeypatch.setattr(tables, "read_instruments", lambda: _env([{"name": "NIFTY"}]))
    body = client.get("/instruments").get_json()
    assert body["items"][0]["name"] == "NIFTY" and body["page"]["has_more"] is False
