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
    compose_action_line,
    compose_wait_reason,
    compute_reads_streak,
    direction_of,
    index_state,
    side_verdict,
    strike_signal,
    window_change,
)
from schemas.market import ExpiryAssessment, IndexExpiry, MonitoredStrike, SnapshotPoint
from schemas.verdict import SideVerdict, StrikeSignal, WallSignal

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
            sensex=True, label="cross-check active"):
    nx = IndexExpiry(index_name="NIFTY", expiry=date(2026, 6, 23), dte=dte_n,
                     is_expiry_day=(dte_n == 0), near_expiry=(dte_n == 1))
    sx = (IndexExpiry(index_name="SENSEX", expiry=date(2026, 6, 25), dte=dte_s,
                      is_expiry_day=(dte_s == 0), near_expiry=(dte_s == 1)) if sensex else None)
    return ExpiryAssessment(nifty=nx, sensex=sx, sensex_missing=sensex_missing,
                            nifty_pin=nifty_pin, sensex_pin=sensex_pin, label=label)


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
    # reads as HOLDING, tagged EXPIRY/PIN. §1: the matured pin is trusted MORE, so a
    # HOLDING read is no longer capped — HIGH here (both ≥signal + at-expiry).
    a = _assess(dte_n=0, nifty_pin=True, label="EXPIRY/PIN — Nifty 0-DTE")
    v = side_verdict("FLOOR", _wall("NIFTY", "unwinding", "strong", streak=3, ot="PE"),
                     _wall("SENSEX", "building", "signal", streak=2, ot="PE"), a)
    assert v.verdict == "FLOOR HOLDING" and v.tag == "EXPIRY/PIN"
    assert v.conviction == "HIGH" and v.nifty.pin is True


def test_near_expiry_lifts_holding_to_high():
    # §1: 1-DTE Nifty. Mild/no-trend building on both indices would be MODERATE, but
    # the matured wall reaches HIGH on its own and is tagged NEAR-EXPIRY (not a pin).
    a = _assess(dte_n=1)
    v = side_verdict("CAP", _wall("NIFTY", "building", "mild", streak=1, trend=False),
                     _wall("SENSEX", "building", "mild", streak=1, trend=False), a)
    assert v.verdict == "CAP HOLDING" and v.conviction == "HIGH"
    assert v.tag == "NEAR-EXPIRY" and "matured" in v.meaning


def test_expiry_day_holding_is_high_not_capped():
    # §1 regression: a 0-DTE HOLDING is no longer downgraded to MODERATE. Even a
    # mild/no-trend build reaches HIGH because the pin has matured the wall.
    a = _assess(dte_n=0, nifty_pin=True, label="EXPIRY/PIN — Nifty 0-DTE")
    v = side_verdict("CAP", _wall("NIFTY", "building", "mild", streak=1, trend=False),
                     _wall("SENSEX", "building", "mild", streak=1, trend=False), a)
    assert v.verdict == "CAP HOLDING" and v.conviction == "HIGH" and v.tag == "EXPIRY/PIN"


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


def test_build_state_warmup_notice_early_session(monkeypatch):
    # §5.5: no snapshot yet spans the window → Δ% not computable → "Collecting data".
    td = date(2026, 6, 19)
    nexp = date(2026, 6, 23)
    monitored = [_ms("CAP", "NIFTY", "CE", 24400, 50, nexp)]
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    # Only recent points (all AFTER the 15-min cutoff 9:45) → window_change None → insufficient.
    monkeypatch.setattr(engine, "read_oi_series",
                        lambda i, o, s, e, since: [_pt(52, 1000), _pt(57, 1050)])
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {})
    monkeypatch.setattr(engine, "get_ladders", lambda d: {})
    # Session's first snapshot at 9:52 → 8 min old at NOW (10:00) → ~7 min to first read.
    monkeypatch.setattr(engine, "read_earliest_snapshot_ts",
                        lambda since: datetime(2026, 6, 19, 9, 52, tzinfo=IST))
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    assert state.sides[0].nifty.wall.insufficient is True
    assert state.warmup == "Collecting data — first read in ~7 min"


def test_build_state_no_warmup_once_data_spans_window(monkeypatch):
    # §5.5: once a strike's series spans the window (Δ% computable), no warm-up notice.
    td = date(2026, 6, 19)
    nexp = date(2026, 6, 23)
    monitored = [_ms("CAP", "NIFTY", "CE", 24400, 50, nexp)]
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    monkeypatch.setattr(engine, "read_oi_series", lambda i, o, s, e, since: _building_series(1000))
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {})
    monkeypatch.setattr(engine, "get_ladders", lambda d: {})
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    assert state.sides[0].nifty.wall.insufficient is False
    assert state.warmup is None


def test_build_state_expiry_pin_note(monkeypatch):
    # §5.6: on a 0-DTE Nifty, name the max-pain pin target from the pinning index.
    from schemas.market import IndexMetrics
    td = date(2026, 6, 19)
    monitored = [_ms("CAP", "NIFTY", "CE", 24400, 50, td),   # 0-DTE Nifty → pin
                 _ms("CAP", "SENSEX", "CE", 78000, 100, date(2026, 6, 25))]
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    monkeypatch.setattr(engine, "read_oi_series", lambda i, o, s, e, since: _building_series(1000))
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {
        "NIFTY": IndexMetrics(index_name="NIFTY", expiry=td, spot=24400.0, max_pain=24350),
        "SENSEX": IndexMetrics(index_name="SENSEX", expiry=date(2026, 6, 25), spot=78000.0, max_pain=77900)})
    monkeypatch.setattr(engine, "get_ladders", lambda d: {})
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    assert state.expiry.nifty_pin is True
    # uses the PINNING index (Nifty) max-pain 24350, not Sensex's.
    assert state.expiry_pin_note == "Pin target 24350 (max-pain) — price likely drawn here today."


def test_build_state_no_expiry_pin_note_off_expiry(monkeypatch):
    # §5.6: no pin note on a non-expiry day even with max-pain present.
    from schemas.market import IndexMetrics
    td = date(2026, 6, 19)
    nexp = date(2026, 6, 23)                                 # DTE 4 → not a pin
    monitored = [_ms("CAP", "NIFTY", "CE", 24400, 50, nexp)]
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    monkeypatch.setattr(engine, "read_oi_series", lambda i, o, s, e, since: _building_series(1000))
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {
        "NIFTY": IndexMetrics(index_name="NIFTY", expiry=nexp, spot=24400.0, max_pain=24350)})
    monkeypatch.setattr(engine, "get_ladders", lambda d: {})
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    assert state.expiry_pin_note is None


def test_build_state_attaches_vix_regime(monkeypatch):
    # §5.3: VIX flows metrics → state as a regime + a ready-made one-glance line.
    from schemas.market import IndexMetrics
    td = date(2026, 6, 19)
    nexp = date(2026, 6, 23)
    monitored = [_ms("CAP", "NIFTY", "CE", 24400, 50, nexp)]
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    monkeypatch.setattr(engine, "read_oi_series", lambda i, o, s, e, since: _building_series(1000))
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {
        "NIFTY": IndexMetrics(index_name="NIFTY", expiry=nexp, spot=24400.0, vix=22.5)})
    # baseline read is only reached because a current VIX exists (guards no-VIX days).
    monkeypatch.setattr(engine, "read_day_open_vix", lambda d: 21.0)
    monkeypatch.setattr(engine, "get_ladders", lambda d: {})
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    assert state.vix == 22.5 and state.vix_regime == "spiking"
    assert state.vix_line == "VIX 22.5 — spiking, trend risk, don't fade"


# --- action verb (the one-line decision) -----------------------------------

def _wall_str(index, state, strength, **kw):
    w = _wall(index, state, **kw)
    w.strength = strength
    return w


def test_action_fade_ok_when_holding_and_strong_wall():
    v = side_verdict("CAP", _wall_str("NIFTY", "building", 4, magnitude="signal"),
                     _wall("SENSEX", "building", "strong"), _assess())
    assert v.verdict == "CAP HOLDING" and v.action == "FADE OK"


def test_action_wait_when_holding_but_thin_wall():
    v = side_verdict("CAP", _wall_str("NIFTY", "building", 2, magnitude="signal"),
                     _wall("SENSEX", "building", "strong"), _assess())
    assert v.verdict == "CAP HOLDING" and v.action == "WAIT"   # strength < 3 → don't fade


def test_action_dont_fade_on_confirmed_break():
    v = side_verdict("FLOOR", _wall_str("NIFTY", "unwinding", 4, magnitude="signal", streak=2, trend=False, ot="PE"),
                     _wall("SENSEX", "unwinding", "signal", streak=2, trend=False, ot="PE"), _assess())
    assert v.verdict == "BREAKDOWN" and v.action == "DON'T FADE"


def test_action_wait_on_unconfirmed_break_even_if_strong():
    # never fade a strength-5 unwind, but an UNCONFIRMED break → WAIT (not DON'T FADE yet)
    v = side_verdict("CAP", _wall_str("NIFTY", "unwinding", 5, magnitude="signal", streak=1, trend=False),
                     _wall("SENSEX", "unwinding", "signal", streak=2, trend=False), _assess())
    assert v.conviction == "UNCONFIRMED" and v.action == "WAIT"


def test_action_wait_on_divergence_and_partial():
    div = side_verdict("CAP", _wall("NIFTY", "building"), _wall("SENSEX", "unwinding"), _assess())
    assert div.verdict == "DIVERGENCE" and div.action == "WAIT"
    par = side_verdict("CAP", _wall("NIFTY", "building"), _wall("SENSEX", "flat"), _assess())
    assert par.verdict.startswith("PARTIAL") and par.action == "WAIT"


def test_action_never_fade_str5_unwinding():
    # §2 invariant: a dominant (str-5) wall that is UNWINDING is a real break — never
    # a fade. Confirmed on both indices → DON'T FADE (go with the move), not FADE OK.
    v = side_verdict("FLOOR", _wall_str("NIFTY", "unwinding", 5, magnitude="strong", streak=3, ot="PE"),
                     _wall("SENSEX", "unwinding", "strong", streak=3, ot="PE"), _assess())
    assert v.verdict == "BREAKDOWN" and v.action == "DON'T FADE"


def test_action_fade_ok_when_flat_but_dominant_wall():
    # "flat but huge" — quiet, but a strength-5 wall is still very fade-able.
    v = side_verdict("CAP", _wall_str("NIFTY", "flat", 5), _wall("SENSEX", "flat"), _assess())
    assert v.verdict == "NO SIGNAL" and v.action == "FADE OK"


# --- §5.1 ACTION line composer ---------------------------------------------

def _sv(side="CAP", verdict="CAP HOLDING", conviction="HIGH", prox="AT", dist_pts=10,
        suppressed=False, nifty_state="building", sensex_state="building", action="WAIT"):
    n = _wall("NIFTY", nifty_state)
    s = None if suppressed else _wall("SENSEX", sensex_state)
    return SideVerdict(
        side=side, option_type=("CE" if side == "CAP" else "PE"), wall_strike=24400,
        verdict=verdict, conviction=conviction, action=action, meaning="", prox=prox,
        dist_pts=dist_pts, suppressed=suppressed, nifty_sig="", dte_n=4, nifty=n, sensex=s,
    )


def test_action_line_holding_at_wall_cap():
    line = compose_action_line(_sv(side="CAP", verdict="CAP HOLDING", prox="AT", dist_pts=12,
                                   action="FADE OK"))
    assert line == ("Top holding, both agree, price 12pts away → "
                    "FADE-NOW zone (buy PE). Wait for your 5-min candle.")


def test_action_line_holding_approaching_floor():
    line = compose_action_line(_sv(side="FLOOR", verdict="FLOOR HOLDING", prox="APPROACHING",
                                    dist_pts=-40, action="FADE OK",
                                    nifty_state="building", sensex_state="building"))
    assert line == "Bottom holding, both agree, price 40pts away → get ready to fade (buy CE)."


def test_action_line_holding_far_just_watch():
    # FAR wall → engine has already downgraded to WAIT; the line just watches.
    line = compose_action_line(_sv(prox="FAR", dist_pts=120, action="WAIT"))
    assert line == "Top holding but price 120pts away → too far, just watch."


def test_action_line_holding_thin_wall_does_not_invite_fade():
    # §5.1/§5.2 consistency: a thin HOLDING wall (action WAIT) must NOT say "FADE-NOW".
    line = compose_action_line(_sv(verdict="CAP HOLDING", prox="AT", dist_pts=10, action="WAIT"))
    assert "FADE-NOW" not in line and "too thin to fade" in line


def test_action_line_holding_nifty_only_says_nifty_alone():
    line = compose_action_line(_sv(verdict="CAP HOLDING (NIFTY-ONLY)", prox="AT",
                                   dist_pts=10, suppressed=True, action="FADE OK"))
    assert "Nifty alone" in line and "FADE-NOW zone (buy PE)" in line


def test_action_line_breakout_confirmed():
    line = compose_action_line(_sv(side="CAP", verdict="BREAKOUT", conviction="HIGH",
                                   prox="AT", nifty_state="unwinding", sensex_state="unwinding"))
    assert line == "Top breaking up — DON'T FADE, stand aside."


def test_action_line_breakdown_unconfirmed_appends_caveat():
    line = compose_action_line(_sv(side="FLOOR", verdict="BREAKDOWN", conviction="UNCONFIRMED",
                                   nifty_state="unwinding", sensex_state="unwinding"))
    assert line == "Bottom breaking down — DON'T FADE, stand aside. (1 read only — wait to confirm.)"


def test_action_line_divergence_partial_nosignal_niftyonly():
    div = compose_action_line(_sv(verdict="DIVERGENCE", conviction="LOW",
                                  nifty_state="building", sensex_state="unwinding"))
    assert div == "Nifty & Sensex disagree — skip or tiny only."
    par = compose_action_line(_sv(verdict="PARTIAL — one index only", conviction="LOW",
                                  nifty_state="building", sensex_state="flat"))
    assert par == "Only Nifty moving, other quiet — wait for both."
    quiet = compose_action_line(_sv(verdict="NO SIGNAL", conviction="NONE",
                                    nifty_state="flat", sensex_state="flat", prox="FAR"))
    assert quiet == "Both quiet — nothing to do."
    nifty_only = compose_action_line(_sv(verdict="NO SIGNAL (NIFTY-ONLY)", conviction="NONE",
                                         nifty_state="flat", suppressed=True, prox="FAR"))
    assert nifty_only == "No Sensex check — Nifty alone, be careful."


def test_action_line_overlays_vix_and_expiry_pin():
    line = compose_action_line(
        _sv(side="CAP", verdict="CAP HOLDING", prox="AT", dist_pts=5, action="FADE OK"),
        vix_regime="spiking", expiry_max_pain=24350,
    )
    assert line.endswith("· VIX spiking, trend risk. · Expiry pin 24350.")
    assert "FADE-NOW zone (buy PE)" in line


# --- §5.2 WAIT reason ------------------------------------------------------

def test_wait_reason_none_when_not_waiting():
    assert compose_wait_reason(_sv(action="FADE OK")) is None
    assert compose_wait_reason(_sv(action="DON'T FADE")) is None


def test_wait_reason_unconfirmed_break():
    r = compose_wait_reason(_sv(verdict="BREAKOUT", conviction="UNCONFIRMED",
                                nifty_state="unwinding", sensex_state="unwinding"))
    assert r == "(1 read only)"


def test_wait_reason_divergence():
    r = compose_wait_reason(_sv(verdict="DIVERGENCE", conviction="LOW",
                                nifty_state="building", sensex_state="unwinding"))
    assert r == "(they disagree)"


def test_wait_reason_partial_names_the_quiet_index():
    # Nifty moving, Sensex flat → Sensex is quiet.
    r = compose_wait_reason(_sv(verdict="PARTIAL — one index only", conviction="LOW",
                                nifty_state="building", sensex_state="flat"))
    assert r == "(Sensex quiet)"
    # Sensex moving, Nifty flat → Nifty is quiet.
    r2 = compose_wait_reason(_sv(verdict="PARTIAL — one index only", conviction="LOW",
                                 nifty_state="flat", sensex_state="building"))
    assert r2 == "(Nifty quiet)"


def test_wait_reason_too_far():
    r = compose_wait_reason(_sv(verdict="CAP HOLDING", conviction="HIGH", prox="FAR",
                                dist_pts=120))
    assert r == "(too far — 120pts)"


def test_wait_reason_thin_wall():
    r = compose_wait_reason(_sv(verdict="CAP HOLDING", conviction="MODERATE", prox="AT"))
    assert r == "(thin wall — hold off)"


def test_build_state_far_wall_downgrades_fade_to_wait(monkeypatch):
    # §5.2/§5.4: a strong HOLDING wall that price is FAR from → WAIT (too far), not FADE.
    from schemas.market import IndexMetrics, Ladder
    td = date(2026, 6, 19)
    nexp, sexp = date(2026, 6, 23), date(2026, 6, 25)
    monitored = [_ms("CAP", "NIFTY", "CE", 24700, 50, nexp),   # wall well above spot
                 _ms("CAP", "SENSEX", "CE", 78100, 100, sexp)]
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    # Nifty wall OI dominates → strength high; all building → CAP HOLDING.
    def _series(i, o, s, e, since):
        return _building_series(6000 if s == (24700 if i == "NIFTY" else 78100) else 800)
    monkeypatch.setattr(engine, "read_oi_series", _series)
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {
        "NIFTY": IndexMetrics(index_name="NIFTY", expiry=nexp, spot=24400.0),   # 300 pts away → FAR
        "SENSEX": IndexMetrics(index_name="SENSEX", expiry=sexp, spot=78100.0)})
    monkeypatch.setattr(engine, "get_ladders", lambda d: {
        "NIFTY": Ladder(index_name="NIFTY", expiry=nexp, spot_at_lock=24400.0, atm=24400,
                        interval=50, strikes=[24550, 24500, 24450, 24400, 24350, 24300, 24250, 24200]),
        "SENSEX": Ladder(index_name="SENSEX", expiry=sexp, spot_at_lock=78100.0, atm=78100,
                         interval=100, strikes=[78400, 78300, 78200, 78100, 78000, 77900, 77800, 77700])})
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    cap = state.sides[0]
    assert cap.verdict == "CAP HOLDING" and cap.prox == "FAR"
    assert cap.action == "WAIT" and cap.wait_reason == "(too far — 300pts)"
    assert "too far, just watch" in cap.action_line


def test_action_line_wired_into_build_state(monkeypatch):
    from schemas.market import IndexMetrics, Ladder
    td = date(2026, 6, 19)
    nexp, sexp = date(2026, 6, 23), date(2026, 6, 25)
    monitored = [_ms("CAP", "NIFTY", "CE", 24400, 50, nexp),
                 _ms("CAP", "SENSEX", "CE", 78100, 100, sexp)]
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    # Dominant wall OI → strength ≥3 → genuinely fade-able (FADE OK), so the AT line
    # invites the fade rather than "too thin to fade".
    def _series(i, o, s, e, since):
        return _building_series(6000 if s == (24400 if i == "NIFTY" else 78100) else 800)
    monkeypatch.setattr(engine, "read_oi_series", _series)
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {
        "NIFTY": IndexMetrics(index_name="NIFTY", expiry=nexp, spot=24400.0),
        "SENSEX": IndexMetrics(index_name="SENSEX", expiry=sexp, spot=78100.0)})
    monkeypatch.setattr(engine, "get_ladders", lambda d: {
        "NIFTY": Ladder(index_name="NIFTY", expiry=nexp, spot_at_lock=24400.0, atm=24400,
                        interval=50, strikes=[24550, 24500, 24450, 24400, 24350, 24300, 24250, 24200]),
        "SENSEX": Ladder(index_name="SENSEX", expiry=sexp, spot_at_lock=78100.0, atm=78100,
                         interval=100, strikes=[78400, 78300, 78200, 78100, 78000, 77900, 77800, 77700])})
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    cap = state.sides[0]
    assert cap.verdict == "CAP HOLDING" and cap.prox == "AT" and cap.action == "FADE OK"
    assert cap.action_line.startswith("Top holding, both agree, price 0pts away → FADE-NOW zone (buy PE)")


def test_action_wait_when_flat_and_thin_wall():
    v = side_verdict("CAP", _wall_str("NIFTY", "flat", 2), _wall("SENSEX", "flat"), _assess())
    assert v.verdict == "NO SIGNAL" and v.action == "WAIT"


def test_build_state_attaches_strength_and_action(monkeypatch):
    from schemas.market import IndexMetrics, Ladder
    td = date(2026, 6, 19)
    nexp, sexp = date(2026, 6, 23), date(2026, 6, 25)
    monitored = [_ms("CAP", "NIFTY", "CE", 24400, 50, nexp),
                 _ms("CAP", "SENSEX", "CE", 78000, 100, sexp)]
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    # The wall's OI series dominates its ladder (latest ≈7080 vs ≈944) → strength 5;
    # all rungs build → CAP HOLDING. Strength now derives from the scored series
    # (read_latest_oi is no longer used by the engine).
    def _series(i, o, s, e, since):
        return _building_series(6000 if s == (24400 if i == "NIFTY" else 78000) else 800)
    monkeypatch.setattr(engine, "read_oi_series", _series)
    # Spot AT the Nifty wall → prox AT, so a strong HOLDING stays FADE OK (a far/no-spot
    # wall would be downgraded to WAIT — §5.4).
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {
        "NIFTY": IndexMetrics(index_name="NIFTY", expiry=nexp, spot=24400.0),
        "SENSEX": IndexMetrics(index_name="SENSEX", expiry=sexp, spot=78000.0)})
    monkeypatch.setattr(engine, "get_ladders", lambda d: {
        "NIFTY": Ladder(index_name="NIFTY", expiry=nexp, spot_at_lock=24400.0, atm=24400,
                        interval=50, strikes=[24550, 24500, 24450, 24400, 24350, 24300, 24250, 24200]),
        "SENSEX": Ladder(index_name="SENSEX", expiry=sexp, spot_at_lock=78000.0, atm=78000,
                         interval=100, strikes=[78300, 78200, 78100, 78000, 77900, 77800, 77700, 77600]),
    })
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    cap = state.sides[0]
    assert cap.nifty.strength == 5 and cap.nifty.dominance is not None
    assert cap.verdict == "CAP HOLDING" and cap.action == "FADE OK"
    # the full 8-rung ladder is now scored and attached (descending by strike)
    assert [s.strike for s in cap.nifty.ladder] == [24550, 24500, 24450, 24400,
                                                    24350, 24300, 24250, 24200]


def test_build_state_flags_off_ladder_wall_and_callout(monkeypatch):
    from schemas.market import IndexMetrics, Ladder
    td = date(2026, 6, 19)
    nexp, sexp = date(2026, 6, 23), date(2026, 6, 25)
    nifty_ladder = Ladder(index_name="NIFTY", expiry=nexp, spot_at_lock=24300.0, atm=24300,
                          interval=50, strikes=[24450, 24400, 24350, 24300, 24250, 24200, 24150, 24100])
    sensex_ladder = Ladder(index_name="SENSEX", expiry=sexp, spot_at_lock=78000.0, atm=78000,
                           interval=100, strikes=[78300, 78200, 78100, 78000, 77900, 77800, 77700, 77600])
    monitored = [
        _ms("CAP", "NIFTY", "CE", 24700, 50, nexp),      # OFF the ladder (top rung 24450)
        _ms("CAP", "SENSEX", "CE", 78500, 100, sexp),    # OFF the ladder (top rung 78300)
    ]
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    monkeypatch.setattr(engine, "read_oi_series", lambda i, o, s, e, since: _building_series(1000 + s))
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {
        "NIFTY": IndexMetrics(index_name="NIFTY", expiry=nexp, spot=24300.0),
        "SENSEX": IndexMetrics(index_name="SENSEX", expiry=sexp, spot=78000.0)})
    monkeypatch.setattr(engine, "get_ladders", lambda d: {"NIFTY": nifty_ladder, "SENSEX": sensex_ladder})
    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    cap = state.sides[0]
    assert cap.nifty.wall_off_ladder is True
    assert cap.nifty.strength is not None            # off-ladder wall STILL gets a strength (dominance fix)
    assert cap.wall_callout is not None
    assert cap.wall_callout.nifty.strike == 24700
    assert cap.wall_callout.sensex.strike == 78500   # both legs off-ladder → both filled


def test_build_state_pairs_ladders_by_level(monkeypatch):
    from schemas.market import IndexMetrics, Ladder
    td = date(2026, 6, 19)
    nexp, sexp = date(2026, 6, 23), date(2026, 6, 25)
    monitored = [_ms("CAP", "NIFTY", "CE", 24400, 50, nexp),
                 _ms("CAP", "SENSEX", "CE", 78100, 100, sexp)]
    monkeypatch.setattr(engine, "read_monitored_strikes", lambda d: monitored)
    monkeypatch.setattr(engine, "read_oi_series", lambda i, o, s, e, since: _building_series(1000))
    # spots → live ratio = 78100 / 24400 ≈ 3.2008 (the pairing axis)
    monkeypatch.setattr(engine, "read_latest_metrics", lambda d: {
        "NIFTY": IndexMetrics(index_name="NIFTY", expiry=nexp, spot=24400.0),
        "SENSEX": IndexMetrics(index_name="SENSEX", expiry=sexp, spot=78100.0),
    })
    monkeypatch.setattr(engine, "get_ladders", lambda d: {
        "NIFTY": Ladder(index_name="NIFTY", expiry=nexp, spot_at_lock=24400.0, atm=24400,
                        interval=50, strikes=[24550, 24500, 24450, 24400, 24350, 24300, 24250, 24200]),
        "SENSEX": Ladder(index_name="SENSEX", expiry=sexp, spot_at_lock=78100.0, atm=78100,
                         interval=100, strikes=[78400, 78300, 78200, 78100, 78000, 77900, 77800, 77700]),
    })

    state = engine.build_state(trading_date=td, now=NOW, window_minutes=15)
    assert state.live_ratio == round(78100 / 24400, 4)
    cap = state.sides[0]
    # §0/§5.4: Nifty wall 24400 vs Nifty spot 24400 → 0 pts away → AT the wall.
    assert cap.dist_pts == 0 and cap.dist_pct == 0.0 and cap.prox == "AT"
    # every Nifty ladder rung appears, descending (plus Sensex-only rows under union)
    assert [r.nifty.strike for r in cap.paired if r.nifty] == [24550, 24500, 24450, 24400,
                                                               24350, 24300, 24250, 24200]
    by_n = {r.nifty.strike: r for r in cap.paired}
    assert all(r.sensex is not None for r in cap.paired)   # nearest match → no blanks
    # 24400 × ratio = 78100 exactly → wall↔wall, ALIGNED (both building)
    assert by_n[24400].sensex.strike == 78100
    assert by_n[24400].is_wall is True and by_n[24400].agree == "ALIGNED"
    # 24450 × ratio ≈ 78260 → nearest Sensex 78300
    assert by_n[24450].sensex.strike == 78300
    # 24550 × ratio ≈ 78580 → off the top → filled with the nearest edge rung 78400
    assert by_n[24550].sensex.strike == 78400
