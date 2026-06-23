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
    sel = select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50)
    assert sel.wall_strike == 24350 and sel.wall_oi == 900
    assert sel.monitored == [24300, 24350, 24400]
    assert sel.option_type == "CE" and sel.side == "CAP"


def test_select_wall_floor_picks_max_pe_oi_on_ladder():
    strikes = [_mk(24150, "PE", 700), _mk(24200, "PE", 1200), _mk(24250, "PE", 300),
               _mk(24200, "CE", 99999)]   # CE ignored for FLOOR
    sel = select_wall(strikes, "FLOOR", LADDER, "NIFTY", EXP, 50)
    assert sel.wall_strike == 24200 and sel.option_type == "PE" and sel.side == "FLOOR"


def test_select_wall_none_when_no_ladder_strike_in_chain():
    strikes = [_mk(26000, "CE", 500)]   # off-ladder
    assert select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50) is None


def test_select_wall_tiebreak_prefers_ladder_centre():
    # 24250 and 24350 tie on OI; ladder centre = 24275, so 24250 (dist 25) wins.
    strikes = [_mk(24250, "CE", 900), _mk(24350, "CE", 900), _mk(24300, "CE", 100)]
    sel = select_wall(strikes, "CAP", LADDER, "NIFTY", EXP, 50)
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


def test_fixture_smoke_nifty_cap():
    raw = json.load(open(os.path.join(FIX, "nifty_optionchain.json")))
    chain = parse_chain(raw, "NIFTY", datetime(2026, 6, 18, tzinfo=timezone.utc))
    assert chain.spot is not None                       # spot extracted from the index row
    ladder = build_ladder(chain.spot, 50)
    sel = select_wall(chain.strikes, "CAP", ladder, "NIFTY", chain.expiry, 50)
    assert sel is not None
    # independently confirm it's the true max-OI CE strike on the ladder
    on_ladder = set(ladder)
    ce = {s.strike: s.oi for s in chain.strikes if s.option_type == "CE" and s.strike in on_ladder}
    assert sel.wall_strike == max(ce, key=ce.get)
    assert sel.expiry == date(2026, 6, 23)
