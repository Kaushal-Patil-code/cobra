"""v3 — internal scheduler tick gating (market hours · single-flight · minute dedup).

All I/O mocked. Verifies scheduled_tick only runs run_tick when the market is open,
this worker wins the advisory lock, and no tick already ran this minute.
"""
from datetime import date

from market import tick


class _FakeCur:
    def __init__(self, got):
        self._got = got
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, p=None):
        self.queries.append(q)

    def fetchone(self):
        return {"got": self._got}


class _FakeConn:
    def __init__(self, got):
        self._got = got

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCur(self._got)


def _patch(monkeypatch, *, market=True, got=True, ticked=False):
    monkeypatch.setattr(tick, "is_market_hours", lambda now=None: market)
    monkeypatch.setattr(tick, "already_ticked_this_minute", lambda now=None: ticked)
    monkeypatch.setattr(tick, "get_conn", lambda: _FakeConn(got))
    calls = []
    monkeypatch.setattr(tick, "run_tick",
                        lambda **kw: calls.append(kw) or {"ran": True, **kw})
    return calls


def test_scheduled_tick_skips_outside_market_hours(monkeypatch):
    calls = _patch(monkeypatch, market=False)
    out = tick.scheduled_tick()
    assert out["skipped"] is True and "market hours" in out["reason"]
    assert calls == []


def test_scheduled_tick_skips_when_lock_held(monkeypatch):
    calls = _patch(monkeypatch, got=False)
    out = tick.scheduled_tick()
    assert out["skipped"] is True and "lock" in out["reason"]
    assert calls == []


def test_scheduled_tick_skips_when_already_ticked(monkeypatch):
    calls = _patch(monkeypatch, got=True, ticked=True)
    out = tick.scheduled_tick()
    assert out["skipped"] is True and "minute" in out["reason"]
    assert calls == []


def test_scheduled_tick_runs_when_clear(monkeypatch):
    calls = _patch(monkeypatch, got=True, ticked=False)
    out = tick.scheduled_tick(strikecount=10, window_minutes=15)
    assert out.get("ran") is True
    assert len(calls) == 1 and calls[0]["strikecount"] == 10


def test_scheduled_tick_never_raises(monkeypatch):
    monkeypatch.setattr(tick, "is_market_hours", lambda now=None: True)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(tick, "get_conn", boom)
    out = tick.scheduled_tick()
    assert "scheduled" in out["error"]


# --- Render keep-alive (app/scheduler.py) ---------------------------------

def test_keepalive_pings_health(monkeypatch):
    from types import SimpleNamespace

    from app import scheduler

    seen = {}
    monkeypatch.setattr(scheduler.settings, "keepalive_url", "https://svc.onrender.com/")
    monkeypatch.setattr(scheduler.requests, "get",
                        lambda url, timeout=None: seen.update(url=url, timeout=timeout)
                        or SimpleNamespace(status_code=200))
    scheduler._keepalive_job()
    assert seen["url"] == "https://svc.onrender.com/health"  # trailing slash trimmed


def test_keepalive_url_overrides_render(monkeypatch):
    from types import SimpleNamespace

    from app import scheduler

    seen = {}
    # Manual KEEPALIVE_URL wins over Render's auto-injected one.
    monkeypatch.setattr(scheduler.settings, "keepalive_url", "https://manual.example.com")
    monkeypatch.setattr(scheduler.settings, "render_external_url", "https://auto.onrender.com")
    monkeypatch.setattr(scheduler.requests, "get",
                        lambda url, timeout=None: seen.update(url=url) or SimpleNamespace(status_code=200))
    scheduler._keepalive_job()
    assert seen["url"] == "https://manual.example.com/health"


def test_keepalive_noop_without_url(monkeypatch):
    from app import scheduler

    monkeypatch.setattr(scheduler.settings, "keepalive_url", None)
    monkeypatch.setattr(scheduler.settings, "render_external_url", None)
    called = {"n": 0}
    monkeypatch.setattr(scheduler.requests, "get", lambda *a, **k: called.__setitem__("n", 1))
    scheduler._keepalive_job()
    assert called["n"] == 0


def test_keepalive_never_raises(monkeypatch):
    from app import scheduler

    monkeypatch.setattr(scheduler.settings, "keepalive_url", "https://svc.onrender.com")

    def boom(*a, **k):
        raise RuntimeError("net down")

    monkeypatch.setattr(scheduler.requests, "get", boom)
    scheduler._keepalive_job()  # must swallow the error
