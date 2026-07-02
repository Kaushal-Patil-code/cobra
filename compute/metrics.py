"""v3 — spot-anchored ladder + chain metrics (§3, §6). PURE, no I/O, no LLM.

Two concerns, both derived from data we already pull (no extra Fyers calls):

  §3  ladder geometry — ATM from live spot, the 8-rung ladder, range-broken check.
  §6  chain metrics   — max-pain (the pin magnet) and PCR, per index.
"""
from __future__ import annotations

import math
import statistics
from typing import Optional, Sequence, Tuple

from config.thresholds import (
    PROX_BANDS,
    VIX_CALM_MAX,
    VIX_INTRADAY_JUMP_PCT,
    VIX_NORMAL_MAX,
)
from schemas.market import ChainSnapshot, IndexMetrics, StrikeOI

# Ladder reach (v3 §3 / §13): ATM + 3 rungs up, 4 down (intentionally skewed down).
LADDER_UP = 3
LADDER_DOWN = 4

# Wall STRENGTH (size, not change): dominance = wall OI ÷ median of the OTHER
# ladder strikes of the same type → bucket 1-5. Ratio-based on purpose so it
# self-adjusts and never goes stale (absolute OI thresholds would). Cutoffs are a
# starting guess — tune on logged data. The bucket edges (lower-inclusive):
#   <1.3 → 1 · 1.3–1.8 → 2 · 1.8–2.5 → 3 · 2.5–3.5 → 4 · ≥3.5 → 5
STRENGTH_CUTOFFS = (1.3, 1.8, 2.5, 3.5)


def strength_bucket(dominance: float) -> int:
    """Map a dominance ratio to a 1-5 strength bucket."""
    return 1 + sum(1 for c in STRENGTH_CUTOFFS if dominance >= c)


def dominance_strength(
    wall_oi: Optional[int], other_ois: Sequence[Optional[int]]
) -> tuple[Optional[float], Optional[int]]:
    """(dominance, strength 1-5) for a wall vs the rest of its ladder.

    dominance = wall_oi ÷ median(other strikes' OI). Returns (None, None) when we
    can't judge (no wall OI / no peers). If the peers are essentially empty
    (median 0) the wall is maximally dominant → strength 5 (dominance reported as
    None since the ratio is unbounded).
    """
    others = [o for o in other_ois if o is not None]
    if wall_oi is None or not others:
        return None, None
    med = statistics.median(others)
    if med <= 0:
        return (None, 5) if wall_oi > 0 else (None, 1)
    dom = wall_oi / med
    # Bucket on the true ratio, but clamp the stored/displayed dominance to fit the
    # verdicts log's numeric(8,2) column (max 999999.99). Strength already saturates
    # at 5, so an astronomically dominant wall (median ≈ 0) loses nothing here.
    return round(min(dom, 999999.99), 2), strength_bucket(dom)


def compute_atm(spot: float, interval: int) -> int:
    """Nearest strike to `spot` on the `interval` grid (round half up)."""
    return int(math.floor(spot / interval + 0.5)) * interval


def build_ladder(spot: float, interval: int) -> list[int]:
    """The 8-strike ladder around `spot`: ATM+3i … ATM-4i, descending (v3 §3)."""
    atm = compute_atm(spot, interval)
    return [atm + k * interval for k in range(LADDER_UP, -LADDER_DOWN - 1, -1)]


def ladder_broken(spot: float, strikes: Sequence[int]) -> bool:
    """True if live `spot` has left the locked ladder's range (v3 §3 RANGE BROKEN)."""
    if not strikes:
        return False
    return spot < min(strikes) or spot > max(strikes)


def wall_distance(
    wall_strike: Optional[int], spot: Optional[float]
) -> Tuple[Optional[int], Optional[float]]:
    """Signed distance from `spot` to the wall (spec §0): `wall_strike − spot`.

    Returns (points, percent_of_spot). Positive = wall ABOVE spot (a CAP still has
    headroom); negative = wall BELOW spot (a FLOOR still has room, or the wall was
    breached). None when either input is missing or spot is 0.
    """
    if wall_strike is None or spot is None or spot == 0:
        return None, None
    pts = wall_strike - spot
    return round(pts), round(pts / spot * 100.0, 2)


def proximity(index_name: str, dist_pts: Optional[float]) -> Optional[str]:
    """AT / APPROACHING / FAR from |dist| using the index's own bands (spec §5.4).

    A fade only exists near the wall, so this says when to actually watch. None when
    the distance is unknown. Bands default to the Nifty set for an unknown index.
    """
    if dist_pts is None:
        return None
    at_max, approaching_max = PROX_BANDS.get(index_name, PROX_BANDS["NIFTY"])
    a = abs(dist_pts)
    if a <= at_max:
        return "AT"
    if a <= approaching_max:
        return "APPROACHING"
    return "FAR"


def max_pain(strikes: Sequence[StrikeOI], atm: Optional[int] = None) -> Optional[int]:
    """Strike S minimising total option-writer payout over the full chain (v3 §6).

        loss(S) = Σ_k CE_OI_k·max(0, S−K_k) + Σ_k PE_OI_k·max(0, K_k−S)

    Evaluated over every distinct strike K in the chain; max-pain = argmin_S loss(S).
    Ties break toward the strike nearest `atm` (else the lowest strike), for a
    deterministic result. Returns None if the chain carries no OI.
    """
    ce: dict[int, int] = {}
    pe: dict[int, int] = {}
    for s in strikes:
        book = ce if s.option_type == "CE" else pe
        book[s.strike] = book.get(s.strike, 0) + (s.oi or 0)
    candidates = sorted(set(ce) | set(pe))
    if not candidates or (sum(ce.values()) + sum(pe.values())) == 0:
        return None

    def loss(S: int) -> int:
        call_pay = sum(oi * max(0, S - k) for k, oi in ce.items())
        put_pay = sum(oi * max(0, k - S) for k, oi in pe.items())
        return call_pay + put_pay

    ref = atm if atm is not None else candidates[0]
    return min(candidates, key=lambda S: (loss(S), abs(S - ref)))


def pcr(call_oi: Optional[int], put_oi: Optional[int]) -> Optional[float]:
    """Put/Call OI ratio from chain totals (v3 §6). None if call OI is missing/zero."""
    if not call_oi or call_oi <= 0 or put_oi is None:
        return None
    return round(put_oi / call_oi, 3)


def vix_regime(vix: Optional[float], vix_open: Optional[float] = None) -> Optional[str]:
    """calm / normal / spiking from the VIX level, or a >5% intraday jump (spec §5.3).

    `vix_open` is the session-open VIX baseline for the jump override — a fast rise
    means a trend day even if the absolute level is still moderate. None when VIX is
    unknown; the jump is skipped when `vix_open` is missing or non-positive.
    """
    if vix is None:
        return None
    jumped = (
        vix_open is not None
        and vix_open > 0
        and (vix - vix_open) / vix_open * 100.0 > VIX_INTRADAY_JUMP_PCT
    )
    if vix > VIX_NORMAL_MAX or jumped:
        return "spiking"
    if vix < VIX_CALM_MAX:
        return "calm"
    return "normal"


def vix_line(vix: Optional[float], regime: Optional[str]) -> Optional[str]:
    """The one-glance VIX line for the dashboard (spec §5.3). None when unknown."""
    if vix is None or regime is None:
        return None
    v = round(vix, 2)
    if regime == "calm":
        return f"VIX {v} — calm, fade-friendly"
    if regime == "spiking":
        return f"VIX {v} — spiking, trend risk, don't fade"
    return f"VIX {v} — normal"


def index_metrics_from_chain(chain: ChainSnapshot, interval: int) -> IndexMetrics:
    """Per-index, per-tick metrics from a parsed chain (v3 §6) — no extra API call."""
    atm = compute_atm(chain.spot, interval) if chain.spot is not None else None
    return IndexMetrics(
        index_name=chain.index_name,
        expiry=chain.expiry,
        spot=chain.spot,
        atm=atm,
        max_pain=max_pain(chain.strikes, atm),
        pcr=pcr(chain.call_oi, chain.put_oi),
        call_oi=chain.call_oi,
        put_oi=chain.put_oi,
        vix=chain.vix,
    )
