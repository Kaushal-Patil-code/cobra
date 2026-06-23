"""v3 — lock planning (pure): ladders + CAP/FLOOR walls, incl. expiry-roll re-lock."""
from datetime import date, datetime, timezone

from compute.strikes import plan_ladders, plan_locks
from schemas.market import ChainSnapshot, Instrument, StrikeOI

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


def test_plan_locks_both_sides_both_indices_when_none_locked():
    chains = {"NIFTY": _nifty(), "SENSEX": _sensex()}
    ladders = plan_ladders(chains, INSTR)
    planned = plan_locks(chains, ladders, set())
    got = {(side, sel.index_name): sel.wall_strike for side, sel in planned}
    assert got[("CAP", "NIFTY")] == 24350 and got[("FLOOR", "NIFTY")] == 24200
    assert got[("CAP", "SENSEX")] == 77600 and got[("FLOOR", "SENSEX")] == 77200
    assert len(planned) == 4


def test_plan_locks_skips_already_locked():
    chains = {"NIFTY": _nifty()}
    ladders = plan_ladders(chains, INSTR)
    already = {("CAP", "NIFTY", NIFTY_EXP)}
    planned = plan_locks(chains, ladders, already)
    assert {side for side, _ in planned} == {"FLOOR"}


def test_plan_locks_relocks_on_expiry_roll():
    rolled = date(2026, 6, 30)                       # new nearest Nifty expiry
    chains = {"NIFTY": _nifty(expiry=rolled)}
    ladders = plan_ladders(chains, INSTR)
    already = {("CAP", "NIFTY", NIFTY_EXP), ("FLOOR", "NIFTY", NIFTY_EXP)}  # old expiry locked
    planned = plan_locks(chains, ladders, already)
    assert len(planned) == 2 and all(sel.expiry == rolled for _, sel in planned)
