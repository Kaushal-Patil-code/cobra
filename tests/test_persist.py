"""v3 — verdict persistence + unified tick (§1, §6, §11). All I/O mocked."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from compute import persist
from market import tick
from schemas.verdict import (
    SideVerdict,
    StrikeSignal,
    VerdictRecord,
    VerdictState,
    WallSignal,
)

IST = timezone(timedelta(hours=5, minutes=30))
TS = datetime(2026, 6, 19, 10, 21, tzinfo=IST)
EXP = date(2026, 6, 23)

# Writable columns persist_state must produce (matches verdict_store insert order).
WRITABLE = {"ts", "trading_date", "weekday", "window_minutes", "side", "option_type",
            "wall_strike", "verdict", "conviction", "meaning", "tag", "nifty_sig",
            "sensex_sig", "dte_n", "dte_s", "suppressed", "expiry_label"}


def _wall(index, ot="CE"):
    sig = StrikeSignal(index_name=index, option_type=ot, strike=24400, expiry=EXP,
                       is_wall=True, direction="up", magnitude="signal", streak=3, trend=True)
    return WallSignal(index_name=index, state="building", wall=sig, summary="building/signal/streak3")


def _side(side, ot, verdict="CAP HOLDING", conv="HIGH", suppressed=False, sensex=True, tag=None):
    return SideVerdict(
        side=side, option_type=ot, wall_strike=24400, verdict=verdict, conviction=conv,
        meaning="m", tag=tag, nifty_sig="building/signal/streak3",
        sensex_sig="building/mild/streak2" if sensex else None,
        dte_n=4, dte_s=6 if sensex else None, suppressed=suppressed,
        expiry_label="cross-check active", nifty=_wall("NIFTY", ot),
        sensex=_wall("SENSEX", ot) if sensex else None,
    )


def _state(sides):
    return VerdictState(ts=TS, trading_date=date(2026, 6, 19), weekday="Fri",
                        window_minutes=15, sides=sides)


# --- persist_state ---------------------------------------------------------

def test_persist_state_writes_one_row_per_side(monkeypatch):
    captured = {}
    monkeypatch.setattr(persist, "insert_verdicts",
                        lambda rows: captured.update(rows=rows) or len(rows))
    n = persist.persist_state(_state([_side("CAP", "CE"),
                                      _side("FLOOR", "PE", "NO SIGNAL", "NONE")]))
    assert n == 2
    rows = captured["rows"]
    assert {r["side"] for r in rows} == {"CAP", "FLOOR"}
    for r in rows:
        assert set(r) == WRITABLE
        assert r["ts"] == TS and r["weekday"] == "Fri" and r["window_minutes"] == 15
    cap = next(r for r in rows if r["side"] == "CAP")
    assert cap["option_type"] == "CE" and cap["verdict"] == "CAP HOLDING"
    assert cap["dte_s"] == 6 and cap["wall_strike"] == 24400


def test_persist_state_records_pin_tag_and_nifty_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(persist, "insert_verdicts",
                        lambda rows: captured.update(rows=rows) or len(rows))
    persist.persist_state(_state([_side("CAP", "CE", "CAP HOLDING (NIFTY-ONLY)", "MODERATE",
                                        suppressed=True, sensex=False, tag="EXPIRY/PIN")]))
    row = captured["rows"][0]
    assert row["suppressed"] is True and row["sensex_sig"] is None and row["dte_s"] is None
    assert row["tag"] == "EXPIRY/PIN"


def test_persist_state_empty_sides_writes_nothing(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(persist, "insert_verdicts",
                        lambda rows: called.__setitem__("n", called["n"] + 1))
    assert persist.persist_state(_state([])) == 0
    assert called["n"] == 0


# --- build_history ---------------------------------------------------------

def test_build_history_assembles_records_and_buckets(monkeypatch):
    rec = VerdictRecord(ts=TS, trading_date=date(2026, 6, 19), weekday="Fri",
                        window_minutes=15, side="CAP", option_type="CE",
                        verdict="CAP HOLDING", conviction="HIGH")
    monkeypatch.setattr(persist, "read_verdicts_range", lambda s, e, side=None: [rec])
    monkeypatch.setattr(persist, "bucket_counts",
                        lambda s, e, side=None: [{"bucket": "Fri", "count": 1, "by_verdict": {"CAP HOLDING": 1}}])
    hist = persist.build_history(date(2026, 6, 15), date(2026, 6, 19))
    assert len(hist.records) == 1 and hist.records[0].verdict == "CAP HOLDING"
    assert hist.weekday_buckets[0].bucket == "Fri" and hist.weekday_buckets[0].count == 1


# --- run_tick (unified cycle) ----------------------------------------------

def _chains(*names):
    return {n: SimpleNamespace(fetched_at=TS) for n in names}


def _mock_metrics(monkeypatch):
    monkeypatch.setattr(tick, "all_instruments",
                        lambda: {"NIFTY": SimpleNamespace(strike_interval=50),
                                 "SENSEX": SimpleNamespace(strike_interval=100)})
    monkeypatch.setattr(tick, "index_metrics_from_chain", lambda ch, interval: object())
    monkeypatch.setattr(tick, "insert_metrics", lambda td, ts, metrics: len(metrics))


def test_run_tick_full_cycle(monkeypatch):
    calls = []
    state = _state([_side("CAP", "CE", tag="EXPIRY/PIN")])
    state.range_broken = ["NIFTY"]

    monkeypatch.setattr("auth.get_fyers_client", lambda: "client")
    monkeypatch.setattr(tick, "fetch_chains", lambda c, strikecount=10: _chains("NIFTY", "SENSEX"))
    monkeypatch.setattr(tick, "store_chains", lambda chains: {n: 3 for n in chains})
    _mock_metrics(monkeypatch)
    monkeypatch.setattr(tick, "lock_walls", lambda td, chains: calls.append("lock") or [{"side": "CAP"}])
    monkeypatch.setattr(tick, "build_state", lambda trading_date, window_minutes: state)
    monkeypatch.setattr(tick, "persist_state", lambda s: calls.append("persist") or len(s.sides))

    out = tick.run_tick(trading_date=date(2026, 6, 19), strikecount=10, window_minutes=15)

    assert out["snapshots"] == {"NIFTY": 3, "SENSEX": 3}
    assert out["metrics_rows"] == 2 and "lock" in calls and "persist" in calls
    assert out["verdicts_written"] == 1 and out["locked"] == [{"side": "CAP"}]
    assert out["sides"][0]["verdict"] == "CAP HOLDING" and out["sides"][0]["tag"] == "EXPIRY/PIN"
    assert out["range_broken"] == ["NIFTY"]


def test_run_tick_locks_every_tick_idempotently(monkeypatch):
    calls = []
    monkeypatch.setattr("auth.get_fyers_client", lambda: "client")
    monkeypatch.setattr(tick, "fetch_chains", lambda c, strikecount=10: _chains("NIFTY"))
    monkeypatch.setattr(tick, "store_chains", lambda chains: {n: 3 for n in chains})
    _mock_metrics(monkeypatch)
    monkeypatch.setattr(tick, "lock_walls", lambda td, chains: calls.append("lock") or [])
    monkeypatch.setattr(tick, "build_state", lambda trading_date, window_minutes: _state([]))
    monkeypatch.setattr(tick, "persist_state", lambda s: 0)
    out = tick.run_tick(trading_date=date(2026, 6, 19))
    assert calls == ["lock"] and out["locked"] == [] and out["verdicts_written"] == 0


def test_run_tick_survives_compute_failure(monkeypatch):
    # A DB error AFTER snapshots are stored → partial summary, never raises.
    monkeypatch.setattr("auth.get_fyers_client", lambda: "client")
    monkeypatch.setattr(tick, "fetch_chains", lambda c, strikecount=10: _chains("NIFTY"))
    monkeypatch.setattr(tick, "store_chains", lambda chains: {"NIFTY": 3})
    _mock_metrics(monkeypatch)

    def boom(td, chains):
        raise RuntimeError("db down")

    monkeypatch.setattr(tick, "lock_walls", boom)
    out = tick.run_tick(trading_date=date(2026, 6, 19))
    assert out["snapshots"] == {"NIFTY": 3}            # storage still reported
    assert "compute/persist" in out["error"]


def test_run_tick_graceful_when_no_chains(monkeypatch):
    monkeypatch.setattr("auth.get_fyers_client", lambda: "client")
    monkeypatch.setattr(tick, "fetch_chains", lambda c, strikecount=10: {})
    out = tick.run_tick(trading_date=date(2026, 6, 19))
    assert out["error"] == "no chains fetched"
