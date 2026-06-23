"""v3 — strike selection over the spot-anchored ladder (§3). PURE, no DB.

There are no typed zones anymore. Each index anchors a ladder on its OWN live
spot (compute.metrics.build_ladder); on that ladder we lock two walls per index:

  CAP   = ladder strike with the highest CE OI   (the resistance the trader fades)
  FLOOR = ladder strike with the highest PE OI   (the support the trader fades)

Walls are LOCKED for the session; if OI later migrates to a neighbor,
`check_migration` FLAGS it — the caller never silently re-picks the wall (§3).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from compute.metrics import build_ladder, compute_atm
from schemas.market import ChainSnapshot, Instrument, Ladder, Side, StrikeOI
from schemas.strikes import MigrationFlag, WallSelection

# CAP fades CALL OI, FLOOR fades PUT OI (v3 §3).
SIDE_OPTION: Dict[Side, str] = {"CAP": "CE", "FLOOR": "PE"}


def select_wall(
    strikes: Sequence[StrikeOI],
    side: Side,
    ladder_strikes: Sequence[int],
    index_name: str,
    expiry,
    interval: int,
) -> Optional[WallSelection]:
    """Highest-OI ladder strike of the side's option type (CAP→CE, FLOOR→PE).

    Returns None if no strike of that type sits on the ladder in the chain (e.g.
    the chain's strikecount didn't reach a ladder rung — caller should widen it).
    """
    option_type = SIDE_OPTION[side]
    cands = set(ladder_strikes)
    oi_by_strike: Dict[int, int] = {
        s.strike: s.oi
        for s in strikes
        if s.option_type == option_type and s.strike in cands
    }
    if not oi_by_strike:
        return None

    # Highest OI wins; ties break toward the ladder centre (≈ ATM) for determinism.
    centre = (min(ladder_strikes) + max(ladder_strikes)) / 2
    wall = max(oi_by_strike, key=lambda k: (oi_by_strike[k], -abs(k - centre)))
    return WallSelection(
        side=side,
        index_name=index_name,
        option_type=option_type,
        expiry=expiry,
        wall_strike=wall,
        wall_oi=oi_by_strike[wall],
        monitored=[wall - interval, wall, wall + interval],
        interval=interval,
    )


def select_index_walls(
    chain: ChainSnapshot, ladder: Ladder
) -> Dict[Side, WallSelection]:
    """Select both walls (CAP via CE, FLOOR via PE) for one index's ladder."""
    out: Dict[Side, WallSelection] = {}
    for side in ("CAP", "FLOOR"):
        sel = select_wall(
            chain.strikes, side, ladder.strikes,
            chain.index_name, chain.expiry, ladder.interval,
        )
        if sel is not None:
            out[side] = sel
    return out


def plan_ladders(
    chains: Dict[str, ChainSnapshot], instruments: Dict[str, Instrument]
) -> Dict[str, Ladder]:
    """Build each index's spot-anchored ladder (v3 §3) — skip indices with no spot."""
    out: Dict[str, Ladder] = {}
    for name, chain in chains.items():
        inst = instruments.get(name)
        if inst is None or chain.spot is None:
            continue
        out[name] = Ladder(
            index_name=name,
            expiry=chain.expiry,
            spot_at_lock=chain.spot,
            atm=compute_atm(chain.spot, inst.strike_interval),
            interval=inst.strike_interval,
            strikes=build_ladder(chain.spot, inst.strike_interval),
        )
    return out


def plan_locks(
    chains: Dict[str, ChainSnapshot],
    ladders: Dict[str, Ladder],
    already_locked: set,
) -> List[Tuple[Side, WallSelection]]:
    """Decide which (side, wall) to newly lock — PURE, no DB (v3 §3 + expiry roll).

    For each index with a ladder, select CAP + FLOOR; skip any
    `(side, index, expiry)` already locked (same-day re-run is a no-op), while a
    **rolled expiry** has no matching key → it locks fresh on the new chain.
    """
    planned: List[Tuple[Side, WallSelection]] = []
    for name, chain in chains.items():
        ladder = ladders.get(name)
        if ladder is None:
            continue
        for side, sel in select_index_walls(chain, ladder).items():
            if (side, name, chain.expiry) in already_locked:
                continue
            planned.append((side, sel))
    return planned


def check_migration(sel: WallSelection, current_oi: Dict[int, int]) -> MigrationFlag:
    """Flag if a neighbor's current OI now exceeds the locked wall's (v3 §3).

    `current_oi` maps strike → latest OI for the monitored set. Reports the
    larger-neighbor direction; does NOT move the wall.
    """
    wall_oi = current_oi.get(sel.wall_strike, 0)
    below = current_oi.get(sel.wall_strike - sel.interval, 0)
    above = current_oi.get(sel.wall_strike + sel.interval, 0)

    if above >= below:
        nb_strike, nb_oi, direction = sel.wall_strike + sel.interval, above, "up"
    else:
        nb_strike, nb_oi, direction = sel.wall_strike - sel.interval, below, "down"

    if nb_oi > wall_oi:
        return MigrationFlag(
            shifting=True,
            from_strike=sel.wall_strike,
            to_strike=nb_strike,
            direction=direction,
            detail=f"{sel.side} shifting {direction}: {sel.wall_strike} → {nb_strike}",
        )
    return MigrationFlag(shifting=False, from_strike=sel.wall_strike, detail="wall holding")
