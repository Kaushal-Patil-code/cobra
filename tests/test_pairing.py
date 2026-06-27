"""v3 item 1 — level-based pairing of the two indices' ladders.

Each row is a NIFTY rung beside the SENSEX rung closest to its level-equivalent
(nifty_strike × the live Sensex/Nifty ratio). Pure function, no DB.
"""
from datetime import date

from compute.pairing import pair_ladders_by_level
from schemas.verdict import StrikeSignal

EXP = date(2026, 6, 24)


def _sig(strike, direction="flat", ot="CE", idx="NIFTY", oi=None, wall=False):
    return StrikeSignal(
        index_name=idx, option_type=ot, strike=strike, expiry=EXP,
        is_wall=wall, direction=direction, oi_latest=oi,
    )


def test_pairs_each_nifty_to_nearest_sensex_by_level():
    # ratio 4.0 lands each Nifty target exactly on a Sensex strike.
    nifty = [_sig(200), _sig(150), _sig(100)]
    sensex = [_sig(s, idx="SENSEX") for s in (800, 700, 600, 500, 400)]
    rows = pair_ladders_by_level(nifty, sensex, ratio=4.0, sensex_interval=100)
    pairs = {r.nifty.strike: (r.sensex.strike if r.sensex else None) for r in rows if r.nifty}
    assert pairs == {200: 800, 150: 600, 100: 400}


def test_unmatched_when_target_beyond_half_interval():
    # 220×4 = 880; nearest Sensex 800 is 80 pts away (> 50 tol) → no level match.
    nifty = [_sig(220)]
    sensex = [_sig(s, idx="SENSEX") for s in (800, 700, 600)]
    rows = pair_ladders_by_level(nifty, sensex, ratio=4.0, sensex_interval=100)
    assert rows[0].sensex is None
    assert rows[0].agree is None
    assert rows[0].level_gap is None


def test_gap_exactly_half_interval_is_matched():
    # 250×3.4 = 850; nearest Sensex 800 is exactly 50 away (== tol, inclusive).
    nifty = [_sig(250)]
    sensex = [_sig(s, idx="SENSEX") for s in (800, 700, 600)]
    rows = pair_ladders_by_level(nifty, sensex, ratio=3.4, sensex_interval=100)
    assert rows[0].sensex.strike == 800
    assert rows[0].level_gap == 50


def test_agreement_aligned_divergent_and_flat():
    sensex_up = [_sig(400, direction="up", idx="SENSEX")]
    aligned = pair_ladders_by_level([_sig(100, direction="up")], sensex_up, 4.0, 100)
    assert aligned[0].agree == "ALIGNED"
    diverg = pair_ladders_by_level([_sig(100, direction="down")], sensex_up, 4.0, 100)
    assert diverg[0].agree == "DIVERGENT"
    # one leg flat → no agreement
    flat = pair_ladders_by_level([_sig(100, direction="flat")], sensex_up, 4.0, 100)
    assert flat[0].agree is None


def test_sensex_missing_all_legs_none_but_nifty_kept():
    rows = pair_ladders_by_level([_sig(100), _sig(150)], [], ratio=4.0, sensex_interval=100)
    assert all(r.sensex is None and r.agree is None for r in rows)
    assert [r.nifty.strike for r in rows] == [150, 100]  # still ordered desc


def test_ratio_none_disables_pairing():
    sensex = [_sig(400, idx="SENSEX")]
    rows = pair_ladders_by_level([_sig(100)], sensex, ratio=None, sensex_interval=100)
    assert rows[0].sensex is None


def test_orders_by_nifty_strike_descending():
    nifty = [_sig(100), _sig(200), _sig(150)]  # unsorted input
    rows = pair_ladders_by_level(nifty, [], ratio=4.0, sensex_interval=100)
    assert [r.nifty.strike for r in rows] == [200, 150, 100]


def test_wall_flag_when_either_leg_is_wall():
    n_wall = pair_ladders_by_level(
        [_sig(100, wall=True)], [_sig(400, idx="SENSEX")], 4.0, 100)
    assert n_wall[0].is_wall is True
    s_wall = pair_ladders_by_level(
        [_sig(100)], [_sig(400, idx="SENSEX", wall=True)], 4.0, 100)
    assert s_wall[0].is_wall is True
    neither = pair_ladders_by_level(
        [_sig(100)], [_sig(400, idx="SENSEX")], 4.0, 100)
    assert neither[0].is_wall is False


def test_realistic_ratio_outer_rungs_have_no_level_equivalent():
    # NIFTY ladder around 25000; SENSEX ladder around 80100 (ratio ~3.202). The
    # Nifty ladder is wider in level-terms, so its outer rungs fall off the Sensex
    # ladder and honestly read "—".
    nifty = [_sig(s) for s in (25150, 25100, 25050, 25000, 24950, 24900, 24850, 24800)]
    sensex = [_sig(s, idx="SENSEX")
              for s in (80400, 80300, 80200, 80100, 80000, 79900, 79800, 79700)]
    rows = pair_ladders_by_level(nifty, sensex, ratio=3.202, sensex_interval=100)
    by_n = {r.nifty.strike: r for r in rows if r.nifty}
    assert by_n[25100].sensex.strike == 80400   # 25100×3.202=80370 → 80400 (30)
    assert by_n[25050].sensex.strike == 80200   # 80210 → 80200 (10)
    assert by_n[25000].sensex is not None and by_n[25000].level_gap == 50  # 80050 → 80000/80100
    assert by_n[24800].sensex is None           # 79410 — below the Sensex ladder


def test_sensex_wall_is_rescued_when_dropped_by_geometry():
    # At ratio 3.202 the Nifty grid is coarser, so 80300 is nearest to NO Nifty rung
    # and would be dropped. Marked as the Sensex wall, it must still appear.
    nifty = [_sig(s) for s in (25150, 25100, 25050, 25000, 24950, 24900, 24850, 24800)]
    sensex = [_sig(s, idx="SENSEX", wall=(s == 80300))
              for s in (80400, 80300, 80200, 80100, 80000, 79900, 79800, 79700)]
    rows = pair_ladders_by_level(nifty, sensex, 3.202, 100)
    wall_rows = [r for r in rows if r.sensex and r.sensex.strike == 80300]
    assert len(wall_rows) == 1
    assert wall_rows[0].is_wall is True and wall_rows[0].nifty is None  # Sensex-only rescue
    # rows stay ordered by level (Nifty-equivalent) descending, rescue row included
    keys = [(r.nifty.strike if r.nifty else r.sensex.strike / 3.202) for r in rows]
    assert keys == sorted(keys, reverse=True)


def test_all_sensex_rungs_appear_under_union():
    # 200→800, 150→600, 100→400 match; 700 and 500 are nearest to no Nifty rung →
    # they still appear as Sensex-only rows so the full Sensex ladder stays visible.
    nifty = [_sig(200), _sig(150), _sig(100)]
    sensex = [_sig(s, idx="SENSEX") for s in (800, 700, 600, 500, 400)]
    rows = pair_ladders_by_level(nifty, sensex, 4.0, 100)
    assert sorted(r.sensex.strike for r in rows if r.sensex) == [400, 500, 600, 700, 800]
    only = sorted(r.sensex.strike for r in rows if r.sensex and r.nifty is None)
    assert only == [500, 700]
    keys = [(r.nifty.strike if r.nifty else r.sensex.strike / 4.0) for r in rows]
    assert keys == sorted(keys, reverse=True)   # rows stay level-descending


def test_equidistant_tie_breaks_to_lower_sensex_strike():
    nifty = [_sig(150)]  # 150 × 5 = 750, exactly between 700 and 800
    for order in ([800, 700], [700, 800]):  # input order must not matter
        sensex = [_sig(s, idx="SENSEX") for s in order]
        rows = pair_ladders_by_level(nifty, sensex, 5.0, 100)
        nrow = next(r for r in rows if r.nifty)          # the Nifty 150 row
        assert nrow.sensex.strike == 700 and nrow.level_gap == 50
