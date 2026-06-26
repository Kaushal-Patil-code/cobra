"""v3 — ladder geometry + chain metrics (§3, §6)."""
from datetime import date, datetime, timezone

from compute.metrics import (
    build_ladder,
    compute_atm,
    dominance_strength,
    index_metrics_from_chain,
    ladder_broken,
    max_pain,
    pcr,
    strength_bucket,
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


def test_strength_bucket_edges():
    assert strength_bucket(1.0) == 1
    assert strength_bucket(1.3) == 2      # lower-inclusive
    assert strength_bucket(1.79) == 2
    assert strength_bucket(1.8) == 3
    assert strength_bucket(2.5) == 4
    assert strength_bucket(3.5) == 5
    assert strength_bucket(10) == 5


def test_dominance_strength():
    # wall 1000 vs peers median 300 → dominance 3.33 → bucket 4
    dom, s = dominance_strength(1000, [200, 250, 300, 300, 350, 400, 500])
    assert dom == round(1000 / 300, 2) and s == 4
    # can't judge without a wall OI or peers
    assert dominance_strength(None, [1, 2, 3]) == (None, None)
    assert dominance_strength(1000, []) == (None, None)
    # peers essentially empty (median 0) → maximally dominant
    assert dominance_strength(500, [0, 0, 0]) == (None, 5)
    assert dominance_strength(0, [0, 0, 0]) == (None, 1)
    # None peers are ignored
    dom2, s2 = dominance_strength(900, [None, 600, None, 300])  # median(600,300)=450 → 2.0 → 3
    assert s2 == 3


def test_index_metrics_from_chain():
    strikes = [_s(24000, "CE", 50), _s(24000, "PE", 80),
               _s(24100, "CE", 200), _s(24100, "PE", 30)]
    ch = ChainSnapshot(index_name="NIFTY", fetched_at=datetime.now(timezone.utc),
                       expiry=EXP, spot=24013.0, call_oi=1000, put_oi=1500, strikes=strikes)
    m = index_metrics_from_chain(ch, 50)
    assert m.atm == 24000 and m.spot == 24013.0
    assert m.pcr == 1.5 and m.max_pain in (24000, 24100)
