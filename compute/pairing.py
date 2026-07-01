"""v3 item 1 — level pairing of the two ladders (round to NEAREST, no blanks).

Each row is a NIFTY rung filled with the SENSEX rung closest to its level-equivalent
(nifty_strike × the live Sensex/Nifty ratio = sensex_spot / nifty_spot), plus an
ALIGNED / DIVERGENT column. ONE row per Nifty rung, so every cell is filled — the
`level_gap` shows how loose the nearest match is (the two strike grids don't align
1:1, so outer rungs pair to a further Sensex strike rather than showing a blank).
The Sensex leg is None only when the ratio / Sensex data is missing (NIFTY-ONLY).
PURE, no I/O.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from schemas.verdict import PairedRung, StrikeSignal

_MOVING = ("up", "down")


def _agree(nifty: Optional[StrikeSignal], sensex: Optional[StrikeSignal]) -> Optional[str]:
    """ALIGNED if both legs move the same way, DIVERGENT if opposite, else None."""
    if nifty is None or sensex is None:
        return None
    nd, sd = nifty.direction, sensex.direction
    if nd not in _MOVING or sd not in _MOVING:
        return None
    return "ALIGNED" if nd == sd else "DIVERGENT"


def pair_ladders_by_level(
    nifty_rungs: Sequence[StrikeSignal],
    sensex_rungs: Sequence[StrikeSignal],
    ratio: Optional[float],
) -> List[PairedRung]:
    """Pair each Nifty rung with the NEAREST Sensex rung by level (no blank cells).

    One row per Nifty rung, ordered by strike descending. Every Nifty rung is filled
    with its closest Sensex rung (min |sensex_strike − nifty_strike×ratio|); the
    Sensex leg is None only when the ratio or Sensex data is missing entirely.
    """
    # Ascending sort makes a tie on |strike − target| resolve to the lower strike.
    sensex_sorted = sorted(sensex_rungs, key=lambda s: s.strike)
    rows: List[PairedRung] = []
    for n in sorted(nifty_rungs, key=lambda s: -s.strike):
        match: Optional[StrikeSignal] = None
        gap: Optional[int] = None
        if ratio and sensex_sorted:
            target = n.strike * ratio
            match = min(sensex_sorted, key=lambda s: abs(s.strike - target))
            gap = round(abs(match.strike - target))
        rows.append(PairedRung(
            nifty=n,
            sensex=match,
            agree=_agree(n, match),
            is_wall=bool(n.is_wall or (match is not None and match.is_wall)),
            level_gap=gap,
        ))
    return rows
