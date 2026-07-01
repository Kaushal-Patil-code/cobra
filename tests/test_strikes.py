"""v4 — wide-scan wall detection (§4).

Deterministic synthetic chains for CAP/FLOOR selection + migration, plus a smoke
test on the captured live fixture. Detection scans the full chain in a spot window;
the ladder is display-only.
"""
import json
import os
from datetime import date, datetime, timezone

from compute.strikes import (
    check_migration,
    select_index_walls,
    select_wall,
)
from market.parse import parse_chain
from schemas.market import StrikeOI
from schemas.strikes import WallSelection

EXP = date(2026, 6, 23)
FIX = os.path.join(os.path.dirname(__file__), "fixtures")
REACH = 400  # NIFTY scan reach in points


def _mk(strike, ot, oi, idx="NIFTY"):
    return StrikeOI(index_name=idx, option_type=ot, strike=strike, expiry=EXP, oi=oi)


def test_select_wall_cap_picks_max_ce_oi_in_window():
    strikes = [
        _mk(24350, "CE", 900), _mk(24400, "CE", 300), _mk(24700, "CE", 5000),  # 24700 > spot+400 → excluded
        _mk(24350, "PE", 99999),                                               # PE ignored for CAP
    ]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH)
    assert sel.wall_strike == 24350 and sel.wall_oi == 900
    assert sel.monitored == [24300, 24350, 24400]
    assert sel.option_type == "CE" and sel.side == "CAP"


def test_select_wall_floor_picks_max_pe_oi_in_window():
    strikes = [_mk(24150, "PE", 700), _mk(24200, "PE", 1200), _mk(23800, "PE", 9000),  # 23800 < spot-400 → excluded
               _mk(24200, "CE", 99999)]
    sel = select_wall(strikes, "FLOOR", "NIFTY", EXP, 50, 24300, REACH)
    assert sel.wall_strike == 24200 and sel.option_type == "PE" and sel.side == "FLOOR"


def test_select_wall_finds_off_ladder_wall_inside_window():
    # 24550 is beyond the 8-rung display (build_ladder(24300) tops at 24450) but
    # inside [spot, spot+400] and dominant → detection must still pick it.
    strikes = [_mk(24350, "CE", 500), _mk(24550, "CE", 4000)]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH)
    assert sel.wall_strike == 24550


def test_select_wall_none_when_window_empty():
    strikes = [_mk(26000, "CE", 500)]                       # far outside spot+400
    assert select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH) is None


def test_select_wall_cap_excludes_below_spot():
    strikes = [_mk(24250, "CE", 9999), _mk(24350, "CE", 900)]   # 24250 < spot → out
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH)
    assert sel.wall_strike == 24350


def test_select_wall_tiebreak_prefers_nearest_spot():
    # 24350 and 24450 tie on OI; nearest spot (24300) wins → 24350.
    strikes = [_mk(24350, "CE", 900), _mk(24450, "CE", 900)]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH)
    assert sel.wall_strike == 24350


def test_select_wall_sticky_holds_within_5pct():
    # incumbent 24350 (1000) vs challenger 24400 (1040, +4% < 5%) → hold.
    strikes = [_mk(24350, "CE", 1000), _mk(24400, "CE", 1040)]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH,
                      incumbent=24350, sticky_margin=0.05)
    assert sel.wall_strike == 24350


def test_select_wall_switches_when_challenger_beats_5pct():
    # challenger 24400 (1060, +6% > 5%) is decisive → switch.
    strikes = [_mk(24350, "CE", 1000), _mk(24400, "CE", 1060)]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH,
                      incumbent=24350, sticky_margin=0.05)
    assert sel.wall_strike == 24400


def test_select_wall_forced_repick_when_spot_crossed_incumbent():
    # spot rose to 24380; incumbent 24350 is now below spot → out of window → re-pick.
    strikes = [_mk(24400, "CE", 500), _mk(24450, "CE", 900)]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24380, REACH,
                      incumbent=24350, sticky_margin=0.05)
    assert sel.wall_strike == 24450


def test_fixture_smoke_nifty_cap():
    raw = json.load(open(os.path.join(FIX, "nifty_optionchain.json")))
    chain = parse_chain(raw, "NIFTY", datetime(2026, 6, 18, tzinfo=timezone.utc))
    assert chain.spot is not None
    sel = select_wall(chain.strikes, "CAP", "NIFTY", chain.expiry, 50, chain.spot, REACH)
    assert sel is not None
    # independently: max-OI CE strike in [spot, spot+400]
    ce = {s.strike: s.oi for s in chain.strikes
          if s.option_type == "CE" and chain.spot <= s.strike <= chain.spot + REACH}
    assert sel.wall_strike == max(ce, key=ce.get)
    assert sel.expiry == date(2026, 6, 23)


def test_check_migration():
    sel = WallSelection(side="CAP", index_name="NIFTY", option_type="CE", expiry=EXP,
                        wall_strike=24350, wall_oi=900, monitored=[24300, 24350, 24400],
                        interval=50)
    assert not check_migration(sel, {24300: 100, 24350: 900, 24400: 300}).shifting
    up = check_migration(sel, {24300: 100, 24350: 900, 24400: 1200})
    assert up.shifting and up.direction == "up" and up.to_strike == 24400
