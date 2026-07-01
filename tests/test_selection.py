"""v3/v4 — ladder planning (pure): spot-anchored ladders per index, re-centered each tick."""
from datetime import date, datetime, timezone

from compute.strikes import plan_ladders, select_index_walls
from schemas.market import ChainSnapshot, Instrument, StrikeOI


class _NullCur:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k): pass
    rowcount = 0
class _NullConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return _NullCur()

REACH_N = 400
REACH_S = 1280   # 400 × ~3.20

TS = datetime(2026, 6, 18, tzinfo=timezone.utc)
NIFTY_EXP = date(2026, 6, 23)
SENSEX_EXP = date(2026, 6, 18)

INSTR = {
    "NIFTY": Instrument(name="NIFTY", symbol="NSE:NIFTY50-INDEX", strike_interval=50,
                        lot_size=75, expiry_weekday="TUE", price_mult=1.0),
    "SENSEX": Instrument(name="SENSEX", symbol="BSE:SENSEX-INDEX", strike_interval=100,
                         lot_size=20, expiry_weekday="THU", price_mult=3.20),
}


def _nifty(expiry=NIFTY_EXP):
    # spot 24300 → ladder 24450..24100; CE peak 24350, PE peak 24200.
    strikes = [StrikeOI(index_name="NIFTY", option_type="CE", strike=s, expiry=expiry, oi=oi)
               for s, oi in [(24300, 500), (24350, 900), (24400, 300)]]
    strikes += [StrikeOI(index_name="NIFTY", option_type="PE", strike=s, expiry=expiry, oi=oi)
                for s, oi in [(24150, 600), (24200, 1200), (24250, 300)]]
    return ChainSnapshot(index_name="NIFTY", fetched_at=TS, expiry=expiry, spot=24300.0,
                         strikes=strikes)


def _sensex(expiry=SENSEX_EXP):
    # spot 77400 → ladder 77700..77000; CE peak 77600, PE peak 77200.
    strikes = [StrikeOI(index_name="SENSEX", option_type="CE", strike=s, expiry=expiry, oi=oi)
               for s, oi in [(77500, 400), (77600, 1200), (77700, 300)]]
    strikes += [StrikeOI(index_name="SENSEX", option_type="PE", strike=s, expiry=expiry, oi=oi)
                for s, oi in [(77100, 500), (77200, 1500), (77300, 200)]]
    return ChainSnapshot(index_name="SENSEX", fetched_at=TS, expiry=expiry, spot=77400.0,
                         strikes=strikes)


def test_plan_ladders_anchors_each_index_on_own_spot():
    ladders = plan_ladders({"NIFTY": _nifty(), "SENSEX": _sensex()}, INSTR)
    assert ladders["NIFTY"].atm == 24300 and len(ladders["NIFTY"].strikes) == 8
    assert ladders["SENSEX"].atm == 77400 and ladders["SENSEX"].interval == 100


def test_plan_ladders_skips_index_without_spot():
    ch = _nifty()
    ch.spot = None
    assert plan_ladders({"NIFTY": ch}, INSTR) == {}


def test_select_index_walls_picks_spot_side_walls():
    n = select_index_walls(_nifty(), 50, REACH_N)
    s = select_index_walls(_sensex(), 100, REACH_S)
    assert n["CAP"].wall_strike == 24350 and n["FLOOR"].wall_strike == 24200
    assert s["CAP"].wall_strike == 77600 and s["FLOOR"].wall_strike == 77200


def test_select_index_walls_holds_incumbent_within_margin():
    held = select_index_walls(_nifty(), 50, REACH_N, {"CAP": 24350}, sticky_margin=0.05)
    assert held["CAP"].wall_strike == 24350


def test_lock_walls_scales_sensex_reach_by_live_ratio(monkeypatch):
    import market.selection as selmod
    captured = []

    def fake_select(chain, interval, scan_reach, incumbents=None, sticky_margin=0.0):
        captured.append((chain.index_name, round(scan_reach)))
        return {}

    monkeypatch.setattr(selmod, "select_index_walls", fake_select)
    monkeypatch.setattr(selmod, "upsert_ladders", lambda td, lads: 0)
    monkeypatch.setattr(selmod, "read_incumbent_walls", lambda td: {})
    monkeypatch.setattr(selmod, "all_instruments", lambda: INSTR)
    # no DB writes happen (select returns {}), so get_conn is only opened for the loop.
    monkeypatch.setattr(selmod, "get_conn", lambda: _NullConn())
    selmod.lock_walls(NIFTY_EXP, {"NIFTY": _nifty(), "SENSEX": _sensex()})
    reach = dict(captured)
    assert reach["NIFTY"] == 400
    # 400 × (77400 / 24300) ≈ 1274
    assert 1260 <= reach["SENSEX"] <= 1290
