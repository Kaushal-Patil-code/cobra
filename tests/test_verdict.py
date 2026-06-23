"""v3 — verdict engine (§5) + EXPIRY/PIN guard (§4).

Per-strike primitives, every row of the §5.2 dual-index table per SIDE, the
"single unwind = FAKE" streak gate, the pin guard (0-DTE unwind ≠ breakout),
NIFTY-ONLY on missing Sensex, and end-to-end build_state with DB reads mocked.
"""
from datetime import date, datetime, timedelta, timezone

from compute import engine
from compute.verdict import (
    build_wall_signal,
    classify_magnitude,
    compute_reads_streak,
    direction_of,
    index_state,
    side_verdict,
    strike_signal,
    window_change,
)
from schemas.market import ExpiryAssessment, IndexExpiry, MonitoredStrike, SnapshotPoint
from schemas.verdict import StrikeSignal, WallSignal

IST = timezone(timedelta(hours=5, minutes=30))
EXP = date(2026, 6, 23)
NOW = datetime(2026, 6, 19, 10, 0, tzinfo=IST)


def _pt(minute, oi):
    return SnapshotPoint(ts=datetime(2026, 6, 19, 9, minute, tzinfo=IST), oi=oi)


# --- window_change (§5.1) --------------------------------------------------

def test_window_change_basic():
    series = [_pt(30, 100), _pt(48, 110), _pt(57, 120)]  # cutoff 9:45 → baseline 9:30=100
    chg, base, latest = window_change(series, NOW, 15)
    assert base == 100 and latest == 120 and round(chg, 2) == 20.0


def test_window_change_insufficient_when_no_point_before_cutoff():
    series = [_pt(50, 100), _pt(57, 120)]                # all after 9:45 cutoff
    chg, base, latest = window_change(series, NOW, 15)
    assert chg is None and base is None and latest == 120


def test_window_change_zero_baseline_is_insufficient():
    chg, _, _ = window_change([_pt(30, 0), _pt(57, 100)], NOW, 15)
    assert chg is None


def test_window_change_30min_picks_earlier_baseline():
    series = [_pt(25, 200), _pt(40, 220), _pt(58, 260)]  # cutoff 9:30 → baseline 9:25=200
    chg, base, _ = window_change(series, NOW, 30)
    assert base == 200 and round(chg, 2) == 30.0


# --- magnitude / direction (§5.1) ------------------------------------------

def test_classify_magnitude_buckets():
    assert classify_magnitude(3) == "noise"
    assert classify_magnitude(7) == "mild"
    assert classify_magnitude(-12) == "signal"
    assert classify_magnitude(25) == "strong"
    assert classify_magnitude(None) == "noise"


def test_direction_flat_below_five_percent():
    assert direction_of(4.9) == "flat"
    assert direction_of(6) == "up"
    assert direction_of(-6) == "down"
    assert direction_of(None) == "flat"


# --- reads / streak (§5.1) -------------------------------------------------

def test_streak_counts_consecutive_same_direction_reads():
    reads, streak, d = compute_reads_streak([_pt(30, 100), _pt(33, 104), _pt(36, 109), _pt(39, 115)])
    assert reads == 3 and streak == 3 and d == "up"


def test_streak_breaks_on_direction_flip():
    reads, streak, d = compute_reads_streak([_pt(30, 100), _pt(33, 110), _pt(36, 100)])
    assert reads == 2 and streak == 1 and d == "down"


def test_streak_zero_when_latest_move_is_noise():
    reads, streak, d = compute_reads_streak([_pt(30, 100), _pt(33, 110), _pt(36, 111)])
    assert reads == 1 and streak == 0 and d == "flat"


# --- strike_signal + index_state -------------------------------------------

def test_strike_signal_building_with_trend():
    series = [_pt(30, 100), _pt(40, 105), _pt(50, 111), _pt(57, 118)]
    sig = strike_signal("NIFTY", "CE", 24400, EXP, series, NOW, 15, is_wall=True)
    assert sig.direction == "up" and sig.magnitude == "signal"
    assert sig.streak == 3 and sig.trend is True and index_state(sig) == "building"


def test_index_state_nodata_when_insufficient():
    series = [_pt(50, 100), _pt(57, 120)]
    sig = strike_signal("NIFTY", "CE", 24400, EXP, series, NOW, 15, is_wall=True)
    assert sig.insufficient is True and index_state(sig) == "nodata"


# --- WallSignal + migration (§3) -------------------------------------------

def _ssig(strike, oi, is_wall=False):
    return StrikeSignal(index_name="NIFTY", option_type="CE", strike=strike, expiry=EXP,
                        is_wall=is_wall, oi_latest=oi, direction="up", magnitude="signal",
                        streak=3, trend=True)


def test_build_wall_signal_flags_neighbor_migration():
    ms = MonitoredStrike(trading_date=date(2026, 6, 19), side="CAP", index_name="NIFTY",
                         option_type="CE", expiry=EXP, wall_strike=24400,
                         monitored=[24350, 24400, 24450])
    signals = {24350: _ssig(24350, 100), 24400: _ssig(24400, 500, is_wall=True),
               24450: _ssig(24450, 900)}
    ws = build_wall_signal(ms, signals)
    assert ws.wall.strike == 24400 and len(ws.neighbors) == 2
    assert ws.migration and "24400 → 24450" in ws.migration and "up" in ws.migration


# --- dual-index verdict per side (§5.2) ------------------------------------

def _assess(dte_n=4, dte_s=6, nifty_pin=False, sensex_pin=False, sensex_missing=False,
            low_weight=False, sensex=True, label="cross-check active"):
    nx = IndexExpiry(index_name="NIFTY", expiry=date(2026, 6, 23), dte=dte_n,
                     is_expiry_day=(dte_n == 0), near_expiry=(dte_n == 1))
    sx = (IndexExpiry(index_name="SENSEX", expiry=date(2026, 6, 25), dte=dte_s,
                      is_expiry_day=(dte_s == 0), near_expiry=(dte_s == 1)) if sensex else None)
    return ExpiryAssessment(nifty=nx, sensex=sx, sensex_missing=sensex_missing,
                            nifty_pin=nifty_pin, sensex_pin=sensex_pin,
                            low_weight=low_weight, label=label)


def _wall(index, state, magnitude="signal", streak=3, trend=None, oi=1000, strike=24400, ot="CE"):
    trend = (streak >= 3) if trend is None else trend
    direction = {"building": "up", "unwinding": "down", "flat": "flat", "nodata": "flat"}[state]
    sig = StrikeSignal(index_name=index, option_type=ot, strike=strike, expiry=EXP, is_wall=True,
                       oi_latest=oi, direction=direction, magnitude=magnitude,
                       reads=streak, streak=streak, trend=trend, insufficient=(state == "nodata"))
    return WallSignal(index_name=index, state=state, wall=sig, neighbors=[],
                      summary=f"{state}/{magnitude}/streak{streak}")


def test_verdict_both_building_high():
    v = side_verdict("CAP", _wall("NIFTY", "building", "signal"),
                     _wall("SENSEX", "building", "strong"), _assess())
    assert v.verdict == "CAP HOLDING" and v.conviction == "HIGH"
    assert v.option_type == "CE" and v.suppressed is False and v.wall_strike == 24400


def test_verdict_both_building_moderate_when_mild_no_trend():
    v = side_verdict("CAP", _wall("NIFTY", "building", "mild", streak=1, trend=False),
                     _wall("SENSEX", "building", "mild", streak=1, trend=False), _assess())
    assert v.verdict == "CAP HOLDING" and v.conviction == "MODERATE"


def test_verdict_both_unwinding_confirmed():
    v = side_verdict("FLOOR", _wall("NIFTY", "unwinding", "signal", streak=2, trend=False, ot="PE"),
                     _wall("SENSEX", "unwinding", "signal", streak=2, trend=False, ot="PE"), _assess())
    assert v.verdict == "BREAKDOWN" and v.conviction == "MODERATE" and v.option_type == "PE"


def test_verdict_unwinding_unconfirmed_single_read():
    v = side_verdict("CAP", _wall("NIFTY", "unwinding", "signal", streak=1, trend=False),
                     _wall("SENSEX", "unwinding", "signal", streak=2, trend=False), _assess())
    assert v.verdict == "BREAKOUT" and v.conviction == "UNCONFIRMED"


def test_verdict_divergence():
    v = side_verdict("CAP", _wall("NIFTY", "building"), _wall("SENSEX", "unwinding"), _assess())
    assert v.verdict == "DIVERGENCE" and v.conviction == "LOW"


def test_verdict_partial_one_index_only():
    v = side_verdict("CAP", _wall("NIFTY", "building"), _wall("SENSEX", "flat"), _assess())
    assert v.verdict.startswith("PARTIAL")


def test_verdict_no_signal_both_flat():
    v = side_verdict("CAP", _wall("NIFTY", "flat"), _wall("SENSEX", "flat"), _assess())
    assert v.verdict == "NO SIGNAL" and v.conviction == "NONE"


def test_verdict_nifty_only_when_sensex_missing():
    v = side_verdict("CAP", _wall("NIFTY", "unwinding", "signal", streak=1, trend=False),
                     None, _assess(sensex=False, dte_s=None, sensex_missing=True,
                                   label="NIFTY-ONLY — Sensex unavailable"))
    assert "NIFTY-ONLY" in v.verdict and "UNCONFIRMED" in v.verdict
    assert v.suppressed is True and v.sensex is None and v.dte_s is None


# --- EXPIRY/PIN guard (§4) -------------------------------------------------

def test_pin_unwind_reads_as_hold_not_breakout():
    # Nifty 0-DTE wall unwinding (settlement) + Sensex building → must NOT break;
    # reads as HOLDING, tagged EXPIRY/PIN, conviction capped at MODERATE.
    a = _assess(dte_n=0, nifty_pin=True, label="EXPIRY/PIN — Nifty 0-DTE")
    v = side_verdict("FLOOR", _wall("NIFTY", "unwinding", "strong", streak=3, ot="PE"),
                     _wall("SENSEX", "building", "signal", streak=2, ot="PE"), a)
    assert v.verdict == "FLOOR HOLDING" and v.tag == "EXPIRY/PIN"
    assert v.conviction == "MODERATE" and v.nifty.pin is True


def test_pin_unwind_vs_real_unwind_is_divergence_not_breakdown():
    a = _assess(dte_n=0, nifty_pin=True, label="EXPIRY/PIN — Nifty 0-DTE")
    v = side_verdict("FLOOR", _wall("NIFTY", "unwinding", "strong", streak=3, ot="PE"),
                     _wall("SENSEX", "unwinding", "signal", streak=3, ot="PE"), a)
    assert v.verdict == "DIVERGENCE"    # pin Nifty reads as building → opposite to Sensex


# --- end-to-end build_state with reads mocked ------------------------------

def _ms(side, index, ot, wall, interval, expiry):
    return MonitoredStrike(trading_date=date(2026, 6, 19), side=side, index_name=index,
                           option_type=ot, expiry=expiry, wall_strike=wall,
                           monitored=[wall - interval, wall, wall + interval])


def _building_series(base):
    return [_pt(30, base), _pt(40, int(base * 1.05)), _pt(50, int(base * 1.11)),
            _pt(57, int(base * 1.18))]


def _mock_reads(monkeypatch, monitored):
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    monkeypatch.setattr(engine, "read_oi_series",
                        lambda i, o, s, e, since: _building_series(1000 + s))
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {})
    monkeypatch.setattr(engine, "get_ladders", lambda d: {})


def test_build_state_end_to_end(monkeypatch):
    td = date(2026, 6, 19)
    nexp, sexp = date(2026, 6, 23), date(2026, 6, 25)        # DTE 4 / 6 → clean
    monitored = [
        _ms("CAP", "NIFTY", "CE", 24400, 50, nexp),
        _ms("CAP", "SENSEX", "CE", 78000, 100, sexp),
        _ms("FLOOR", "NIFTY", "PE", 24000, 50, nexp),        # no Sensex FLOOR → NIFTY-ONLY
    ]
    _mock_reads(monkeypatch, monitored)
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    assert [v.side for v in state.sides] == ["CAP", "FLOOR"]
    cap = state.sides[0]
    assert cap.verdict == "CAP HOLDING" and cap.dte_n == 4 and cap.dte_s == 6
    floor = state.sides[1]
    assert floor.sensex is None and "NIFTY-ONLY" in floor.verdict
    assert state.expiry.nifty_pin is False


def test_build_state_tags_pin_on_nifty_expiry_day(monkeypatch):
    td = date(2026, 6, 19)
    monitored = [
        _ms("CAP", "NIFTY", "CE", 24400, 50, td),            # 0-DTE → pin, still computes
        _ms("CAP", "SENSEX", "CE", 78000, 100, date(2026, 6, 25)),
    ]
    _mock_reads(monkeypatch, monitored)
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    assert state.expiry.nifty_pin is True and not state.expiry.sensex_missing
    cap = state.sides[0]
    assert cap.tag == "EXPIRY/PIN" and cap.suppressed is False  # not NIFTY-ONLY (data present)


def test_build_state_no_walls(monkeypatch):
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: [])
    state = engine.build_state(trading_date=date(2026, 6, 19), now=NOW)
    assert state.sides == [] and state.note and "no walls" in state.note
