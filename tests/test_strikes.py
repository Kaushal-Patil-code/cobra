"""v3 — ladder-based wall selection (§3).

Deterministic synthetic chains for CAP/FLOOR selection + migration, plus a smoke
test on the captured live fixture.
"""
import json
import os
from datetime import date, datetime, timezone

from compute.metrics import build_ladder
from compute.strikes import (
    check_migration,
    select_index_walls,
    select_wall,
)
from market.parse import parse_chain
from schemas.market import ChainSnapshot, Ladder, StrikeOI
from schemas.strikes import WallSelection

EXP = date(2026, 6, 23)
FIX = os.path.join(os.path.dirname(__file__), "fixtures")
LADDER = build_ladder(24300, 50)   # [24450,24400,24350,24300,24250,24200,24150,24100]


def _mk(strike, ot, oi, idx="NIFTY"):
    return StrikeOI(index_name=idx, option_type=ot, strike=strike, expiry=EXP, oi=oi)


def test_select_wall_cap_picks_max_ce_oi_on_ladder():
    strikes = [
        _mk(24250, "CE", 100), _mk(24300, "CE", 500), _mk(24350, "CE", 900),
        _mk(24400, "CE", 300),
        _mk(24350, "PE", 99999),   # PE must be ignored when selecting a CAP
    ]
    sel = select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50, spot=24300)
    assert sel.wall_strike == 24350 and sel.wall_oi == 900
    assert sel.monitored == [24300, 24350, 24400]
    assert sel.option_type == "CE" and sel.side == "CAP"


def test_select_wall_floor_picks_max_pe_oi_on_ladder():
    strikes = [_mk(24150, "PE", 700), _mk(24200, "PE", 1200), _mk(24250, "PE", 300),
               _mk(24200, "CE", 99999)]   # CE ignored for FLOOR
    sel = select_wall(strikes, "FLOOR", LADDER, "NIFTY", EXP, 50, spot=24300)
    assert sel.wall_strike == 24200 and sel.option_type == "PE" and sel.side == "FLOOR"


def test_select_wall_none_when_no_ladder_strike_in_chain():
    strikes = [_mk(26000, "CE", 500)]   # off-ladder
    assert select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50, spot=24300) is None


def test_select_wall_tiebreak_prefers_ladder_centre():
    # spot 24250 keeps both eligible; 24250 and 24350 tie on OI; ladder centre =
    # 24275, so 24250 (dist 25) wins.
    strikes = [_mk(24250, "CE", 900), _mk(24350, "CE", 900), _mk(24300, "CE", 100)]
    sel = select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50, spot=24250)
    assert sel.wall_strike == 24250


def test_select_index_walls_returns_cap_and_floor():
    strikes = [_mk(24350, "CE", 900), _mk(24400, "CE", 200),
               _mk(24150, "PE", 800), _mk(24200, "PE", 1500)]
    chain = ChainSnapshot(index_name="NIFTY", fetched_at=datetime.now(timezone.utc),
                          expiry=EXP, spot=24300.0, strikes=strikes)
    ladder = Ladder(index_name="NIFTY", expiry=EXP, spot_at_lock=24300.0, atm=24300,
                    interval=50, strikes=LADDER)
    walls = select_index_walls(chain, ladder)
    assert walls["CAP"].wall_strike == 24350
    assert walls["FLOOR"].wall_strike == 24200


def test_check_migration():
    sel = WallSelection(side="CAP", index_name="NIFTY", option_type="CE", expiry=EXP,
                        wall_strike=24350, wall_oi=900, monitored=[24300, 24350, 24400],
                        interval=50)
    assert not check_migration(sel, {24300: 100, 24350: 900, 24400: 300}).shifting
    up = check_migration(sel, {24300: 100, 24350: 900, 24400: 1200})
    assert up.shifting and up.direction == "up" and up.to_strike == 24400


# --- v3 item 3: spot-side filter + stickiness (dynamic re-pick) ------------

def test_select_wall_cap_excludes_strikes_below_spot():
    # 24250 has the most CE OI but is BELOW spot 24300 → ineligible for a CAP.
    strikes = [_mk(24250, "CE", 9999), _mk(24350, "CE", 900), _mk(24300, "CE", 100)]
    sel = select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50, spot=24300)
    assert sel.wall_strike == 24350


def test_select_wall_floor_excludes_strikes_above_spot():
    # 24400 has the most PE OI but is ABOVE spot 24300 → ineligible for a FLOOR.
    strikes = [_mk(24400, "PE", 9999), _mk(24200, "PE", 800), _mk(24300, "PE", 100)]
    sel = select_wall(strikes, "FLOOR", LADDER, "NIFTY", EXP, 50, spot=24300)
    assert sel.wall_strike == 24200


def test_select_wall_none_when_no_strike_on_spot_side():
    # spot above the whole ladder → no CE strike at/above spot for a CAP.
    strikes = [_mk(24100, "CE", 500), _mk(24200, "CE", 700)]
    assert select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50, spot=24500) is None


def test_select_wall_sticky_holds_incumbent_within_margin():
    # incumbent 24300 (1000) vs challenger 24350 (1100, +10% < 15% margin) → hold.
    strikes = [_mk(24300, "CE", 1000), _mk(24350, "CE", 1100)]
    sel = select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50, spot=24250,
                      incumbent=24300, sticky_margin=0.15)
    assert sel.wall_strike == 24300 and sel.wall_oi == 1000


def test_select_wall_switches_when_challenger_beats_margin():
    # challenger 24350 (1200, +20% > 15%) is decisive → switch off the incumbent.
    strikes = [_mk(24300, "CE", 1000), _mk(24350, "CE", 1200)]
    sel = select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50, spot=24250,
                      incumbent=24300, sticky_margin=0.15)
    assert sel.wall_strike == 24350


def test_select_wall_forced_repick_when_incumbent_ineligible():
    # incumbent 24200 is now BELOW spot 24300 → can't stay a CAP → re-pick fresh.
    strikes = [_mk(24300, "CE", 500), _mk(24350, "CE", 900)]
    sel = select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50, spot=24300,
                      incumbent=24200, sticky_margin=0.15)
    assert sel.wall_strike == 24350


def test_select_wall_incumbent_kept_when_it_is_also_the_challenger():
    strikes = [_mk(24300, "CE", 100), _mk(24350, "CE", 900)]
    sel = select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50, spot=24250,
                      incumbent=24350, sticky_margin=0.15)
    assert sel.wall_strike == 24350


def test_fixture_smoke_nifty_cap():
    raw = json.load(open(os.path.join(FIX, "nifty_optionchain.json")))
    chain = parse_chain(raw, "NIFTY", datetime(2026, 6, 18, tzinfo=timezone.utc))
    assert chain.spot is not None                       # spot extracted from the index row
    ladder = build_ladder(chain.spot, 50)
    sel = select_wall(chain.strikes, "CAP", ladder, "NIFTY", chain.expiry, 50, spot=chain.spot)
    assert sel is not None
    # independently confirm it's the true max-OI CE strike on the ladder AT/ABOVE spot
    on_ladder = set(ladder)
    ce = {s.strike: s.oi for s in chain.strikes
          if s.option_type == "CE" and s.strike in on_ladder and s.strike >= chain.spot}
    assert sel.wall_strike == max(ce, key=ce.get)
    assert sel.expiry == date(2026, 6, 23)
