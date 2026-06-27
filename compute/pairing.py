"""v3 item 1 — level-based pairing of the two indices' ladders (PURE, no I/O).

Each row pairs strikes by LEVEL: a NIFTY rung beside the SENSEX rung closest to its
level-equivalent (nifty_strike × the live Sensex/Nifty ratio = sensex_spot /
nifty_spot), with an ALIGNED / DIVERGENT column for the at-a-glance cross-check.

Geometry note (why it's not a clean 1:1 table): NIFTY strikes step 50, SENSEX 100,
ratio ~3.20 — so a Nifty rung step is ≈50×3.20 ≈ 160 in Sensex-level terms, COARSER
than the Sensex 100-pt step. Anchoring one row per Nifty rung therefore (a) leaves
the outermost Nifty rungs with no Sensex strike at their level (the ladder spans
differ → those legs read "—"), and (b) skips the Sensex rungs that are nearest to no
Nifty rung. To keep BOTH full 8-rung ladders visible, every such skipped Sensex rung
is added as its own Sensex-only row (nifty None), and all rows are sorted by level.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from schemas.verdict import PairedRung, StrikeSignal

# A Nifty rung pairs to a Sensex strike only if that strike sits within this
# fraction of a Sensex interval of the level-equivalent (nifty × ratio). Half an
# interval keeps every interior rung matched while honestly nulling rungs that fall
# off the Sensex ladder. Tunable — raise toward 1.0 to always show the closest rung.
LEVEL_MATCH_TOL_FRAC = 0.5

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
    sensex_interval: int,
) -> List[PairedRung]:
    """Pair the two ladders by level; show every rung of both, ordered by level desc.

    One row per Nifty rung (Sensex leg = the rung closest to nifty_strike × ratio, or
    None when ratio/Sensex data is missing or the nearest strike is > tolerance away).
    Every Sensex rung that matched no Nifty rung is then appended as a Sensex-only row
    (nifty None), so both full 8-rung ladders always appear.
    """
    tol = sensex_interval * LEVEL_MATCH_TOL_FRAC
    # Ascending sort makes a tie on |strike − target| resolve to the lower strike.
    sensex_sorted = sorted(sensex_rungs, key=lambda s: s.strike)
    matched: set = set()
    rows: List[PairedRung] = []
    for n in sorted(nifty_rungs, key=lambda s: -s.strike):
        match: Optional[StrikeSignal] = None
        gap: Optional[int] = None
        if ratio and sensex_sorted:
            target = n.strike * ratio
            best = min(sensex_sorted, key=lambda s: abs(s.strike - target))
            d = abs(best.strike - target)
            if d <= tol:
                match, gap = best, round(d)
                matched.add(best.strike)
        rows.append(PairedRung(
            nifty=n,
            sensex=match,
            agree=_agree(n, match),
            is_wall=bool(n.is_wall or (match is not None and match.is_wall)),
            level_gap=gap,
        ))

    # Keep the FULL Sensex ladder visible: any Sensex rung nearest to no Nifty rung
    # (the strike grids don't align 1:1) becomes its own Sensex-only row, placed by
    # its Nifty-equivalent level. Needs the ratio to position it.
    if ratio:
        for s in sensex_sorted:
            if s.strike not in matched:
                rows.append(PairedRung(nifty=None, sensex=s, agree=None, is_wall=s.is_wall))
        rows.sort(
            key=lambda r: r.nifty.strike if r.nifty else r.sensex.strike / ratio,
            reverse=True,
        )
    return rows
