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
    proximity,
    strength_bucket,
    wall_distance,
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
    # extreme dominance is clamped to fit numeric(8,2); strength still saturates at 5
    dom3, s3 = dominance_strength(12_000_000, [1, 2, 5, 10, 50, 100, 200])  # median 10 → 1.2M
    assert dom3 == 999999.99 and s3 == 5


def test_wall_distance_signed_pts_and_pct():
    # CAP wall above spot → positive; FLOOR wall below → negative. % is of spot.
    pts, pct = wall_distance(24450, 24400.0)
    assert pts == 50 and pct == round(50 / 24400 * 100, 2)
    pts, pct = wall_distance(24000, 24120.0)
    assert pts == -120 and pct == round(-120 / 24120 * 100, 2)
    # unknown inputs
    assert wall_distance(None, 24400.0) == (None, None)
    assert wall_distance(24450, None) == (None, None)
    assert wall_distance(24450, 0) == (None, None)


def test_proximity_bands_per_index():
    # Nifty bands 25 / 60 (§5.4), on |dist|.
    assert proximity("NIFTY", 20) == "AT"
    assert proximity("NIFTY", -25) == "AT"          # boundary inclusive, abs()
    assert proximity("NIFTY", 40) == "APPROACHING"
    assert proximity("NIFTY", 61) == "FAR"
    # Sensex bands 75 / 190 (≈ ×3.2).
    assert proximity("SENSEX", 70) == "AT"
    assert proximity("SENSEX", 150) == "APPROACHING"
    assert proximity("SENSEX", 200) == "FAR"
    # unknown distance → None; unknown index falls back to Nifty bands.
    assert proximity("NIFTY", None) is None
    assert proximity("MYSTERY", 20) == "AT"


def test_index_metrics_from_chain():
    strikes = [_s(24000, "CE", 50), _s(24000, "PE", 80),
               _s(24100, "CE", 200), _s(24100, "PE", 30)]
    ch = ChainSnapshot(index_name="NIFTY", fetched_at=datetime.now(timezone.utc),
                       expiry=EXP, spot=24013.0, call_oi=1000, put_oi=1500, vix=13.5,
                       strikes=strikes)
    m = index_metrics_from_chain(ch, 50)
    assert m.atm == 24000 and m.spot == 24013.0
    assert m.pcr == 1.5 and m.max_pain in (24000, 24100)
    assert m.vix == 13.5                    # §5.3: VIX carried through from the chain


def test_vix_regime_bands_and_jump():
    from compute.metrics import vix_regime
    assert vix_regime(None) is None
    assert vix_regime(12.0) == "calm"
    assert vix_regime(13.99) == "calm"
    assert vix_regime(14.0) == "normal"       # 14 inclusive → normal
    assert vix_regime(20.0) == "normal"       # 20 inclusive → normal
    assert vix_regime(20.1) == "spiking"
    # intraday jump > 5% off the session-open flips a moderate level to spiking
    assert vix_regime(15.0, vix_open=14.0) == "spiking"   # +7.1%
    assert vix_regime(14.5, vix_open=14.0) == "normal"    # +3.6% (below 5%)
    assert vix_regime(13.0, vix_open=20.0) == "calm"      # a DROP is never a jump
    assert vix_regime(15.0, vix_open=0) == "normal"       # non-positive baseline ignored


def test_vix_line_wording():
    from compute.metrics import vix_line
    assert vix_line(None, None) is None
    assert vix_line(12.34, "calm") == "VIX 12.34 — calm, fade-friendly"
    assert vix_line(16.0, "normal") == "VIX 16.0 — normal"
    assert vix_line(24.5, "spiking") == "VIX 24.5 — spiking, trend risk, don't fade"
