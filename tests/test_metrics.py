"""v3 — ladder geometry + chain metrics (§3, §6)."""
from datetime import date, datetime, timezone

from compute.metrics import (
    build_ladder,
    compute_atm,
    index_metrics_from_chain,
    ladder_broken,
    max_pain,
    pcr,
)
from schemas.market import ChainSnapshot, StrikeOI

EXP = date(2026, 6, 24)


def _s(strike, ot, oi, idx="NIFTY"):
    return StrikeOI(index_name=idx, option_type=ot, strike=strike, expiry=EXP, oi=oi)


def test_compute_atm_rounds_half_up():
    assert compute_atm(24013, 50) == 24000
    assert compute_atm(24025, 50) == 24050      # exact .5 rounds up
    assert compute_atm(77430, 100) == 77400


def test_build_ladder_reach_and_order():
    # ATM + 3 up + 4 down, descending (the brief's exact example)
    assert build_ladder(24000, 50) == [24150, 24100, 24050, 24000, 23950, 23900, 23850, 23800]
    assert build_ladder(77400, 100) == [77700, 77600, 77500, 77400, 77300, 77200, 77100, 77000]


def test_ladder_broken_boundaries_inclusive():
    lad = build_ladder(24000, 50)
    assert ladder_broken(24151, lad) is True       # above top
    assert ladder_broken(23799, lad) is True       # below bottom
    assert ladder_broken(24150, lad) is False      # on the boundary
    assert ladder_broken(24000, lad) is False


def test_max_pain_argmin_writer_payout():
    # CE heavy high, PE heavy low → max-pain pinned at the middle strike.
    strikes = [_s(100, "CE", 0), _s(100, "PE", 30),
               _s(200, "CE", 10), _s(200, "PE", 10),
               _s(300, "CE", 30), _s(300, "PE", 0)]
    assert max_pain(strikes) == 200
    assert max_pain([]) is None


def test_pcr():
    assert pcr(100, 150) == 1.5
    assert pcr(0, 100) is None          # no call OI
    assert pcr(100, None) is None


def test_index_metrics_from_chain():
    strikes = [_s(24000, "CE", 50), _s(24000, "PE", 80),
               _s(24100, "CE", 200), _s(24100, "PE", 30)]
    ch = ChainSnapshot(index_name="NIFTY", fetched_at=datetime.now(timezone.utc),
                       expiry=EXP, spot=24013.0, call_oi=1000, put_oi=1500, strikes=strikes)
    m = index_metrics_from_chain(ch, 50)
    assert m.atm == 24000 and m.spot == 24013.0
    assert m.pcr == 1.5 and m.max_pain in (24000, 24100)
