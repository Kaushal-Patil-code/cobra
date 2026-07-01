"""v3 item 1 — level pairing (round to NEAREST, no blank cells).

Each row is a NIFTY rung filled with the SENSEX rung closest to its level-equivalent
(nifty_strike × the live ratio). Every Nifty rung gets a partner (no blanks); the
Sensex leg is None only when the ratio / Sensex data is missing. Pure, no DB.
"""
from datetime import date

from compute.pairing import build_wall_callout, pair_ladders_by_level
from schemas.verdict import StrikeSignal, WallSignal

EXP = date(2026, 6, 24)


def _sig(strike, direction="flat", ot="CE", idx="NIFTY", oi=None, wall=False):
    return StrikeSignal(
        index_name=idx, option_type=ot, strike=strike, expiry=EXP,
        is_wall=wall, direction=direction, oi_latest=oi,
    )


def test_pairs_each_nifty_to_nearest_sensex_by_level():
    nifty = [_sig(200), _sig(150), _sig(100)]
    sensex = [_sig(s, idx="SENSEX") for s in (800, 700, 600, 500, 400)]
    rows = pair_ladders_by_level(nifty, sensex, ratio=4.0)
    assert {r.nifty.strike: r.sensex.strike for r in rows} == {200: 800, 150: 600, 100: 400}
    assert len(rows) == 3   # one row per Nifty rung, no extra rows


def test_always_matches_nearest_even_when_far_no_blanks():
    # 220×4 = 880; nearest Sensex 800 is 80 pts away — still FILLED (no blank cell).
    nifty = [_sig(220)]
    sensex = [_sig(s, idx="SENSEX") for s in (800, 700, 600)]
    rows = pair_ladders_by_level(nifty, sensex, 4.0)
    assert rows[0].sensex.strike == 800 and rows[0].level_gap == 80


def test_level_gap_is_reported():
    nifty = [_sig(250)]  # 250×3.4 = 850 → nearest 800, gap 50
    sensex = [_sig(s, idx="SENSEX") for s in (800, 700, 600)]
    rows = pair_ladders_by_level(nifty, sensex, 3.4)
    assert rows[0].sensex.strike == 800 and rows[0].level_gap == 50


def test_agreement_aligned_divergent_and_flat():
    sensex_up = [_sig(400, direction="up", idx="SENSEX")]
    assert pair_ladders_by_level([_sig(100, direction="up")], sensex_up, 4.0)[0].agree == "ALIGNED"
    assert pair_ladders_by_level([_sig(100, direction="down")], sensex_up, 4.0)[0].agree == "DIVERGENT"
    assert pair_ladders_by_level([_sig(100, direction="flat")], sensex_up, 4.0)[0].agree is None


def test_sensex_missing_leaves_sensex_leg_none():
    rows = pair_ladders_by_level([_sig(100), _sig(150)], [], ratio=4.0)
    assert all(r.sensex is None and r.agree is None for r in rows)
    assert [r.nifty.strike for r in rows] == [150, 100]   # still ordered desc


def test_ratio_none_disables_pairing():
    rows = pair_ladders_by_level([_sig(100)], [_sig(400, idx="SENSEX")], ratio=None)
    assert rows[0].sensex is None


def test_orders_by_nifty_strike_descending():
    rows = pair_ladders_by_level([_sig(100), _sig(200), _sig(150)], [], ratio=4.0)
    assert [r.nifty.strike for r in rows] == [200, 150, 100]


def test_wall_flag_when_either_leg_is_wall():
    assert pair_ladders_by_level([_sig(100, wall=True)], [_sig(400, idx="SENSEX")], 4.0)[0].is_wall
    assert pair_ladders_by_level([_sig(100)], [_sig(400, idx="SENSEX", wall=True)], 4.0)[0].is_wall
    assert not pair_ladders_by_level([_sig(100)], [_sig(400, idx="SENSEX")], 4.0)[0].is_wall


def test_no_blank_legs_every_nifty_rung_filled_realistic():
    # NIFTY ~25000, SENSEX ~80100 (ratio ~3.202). Every Nifty rung — including the
    # outer ones that fall off the Sensex ladder — is filled with its nearest rung.
    nifty = [_sig(s) for s in (25150, 25100, 25050, 25000, 24950, 24900, 24850, 24800)]
    sensex = [_sig(s, idx="SENSEX")
              for s in (80400, 80300, 80200, 80100, 80000, 79900, 79800, 79700)]
    rows = pair_ladders_by_level(nifty, sensex, 3.202)
    assert len(rows) == 8
    assert all(r.nifty is not None and r.sensex is not None for r in rows)   # NO blanks
    by_n = {r.nifty.strike: r for r in rows}
    assert by_n[25100].sensex.strike == 80400   # 80370 → 80400
    assert by_n[24800].sensex.strike == 79700   # 79410 → nearest edge 79700 (filled, gap large)


def test_equidistant_tie_breaks_to_lower_sensex_strike():
    nifty = [_sig(150)]  # 150×5 = 750, exactly between 700 and 800
    for order in ([800, 700], [700, 800]):   # input order must not matter
        sensex = [_sig(s, idx="SENSEX") for s in order]
        rows = pair_ladders_by_level(nifty, sensex, 5.0)
        assert rows[0].sensex.strike == 700 and rows[0].level_gap == 50


# --- build_wall_callout (v4 off-ladder callout row) --------------------------

def _ws(index, strike, direction, off_ladder):
    sig = StrikeSignal(index_name=index, option_type="CE", strike=strike,
                       expiry=EXP, is_wall=True, direction=direction, oi_latest=1000)
    return WallSignal(index_name=index, state="building", wall=sig,
                      wall_off_ladder=off_ladder)


def test_callout_none_when_both_on_ladder():
    n = _ws("NIFTY", 24300, "up", False)
    s = _ws("SENSEX", 77800, "up", False)
    assert build_wall_callout(n, s, 3.2) is None


def test_callout_pairs_off_ladder_walls_with_agree():
    n = _ws("NIFTY", 24550, "up", True)
    s = _ws("SENSEX", 78560, "up", True)      # 24550×3.2 = 78560
    row = build_wall_callout(n, s, 3.2)
    assert row.nifty.strike == 24550 and row.sensex.strike == 78560
    assert row.agree == "ALIGNED" and row.is_wall and row.level_gap == 0


def test_callout_one_leg_when_only_nifty_off_ladder():
    n = _ws("NIFTY", 24550, "up", True)
    s = _ws("SENSEX", 77800, "down", False)
    row = build_wall_callout(n, s, 3.2)
    assert row.nifty.strike == 24550 and row.sensex is None and row.agree is None
