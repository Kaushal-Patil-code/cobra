"""v3 — strike selection over the spot-anchored ladder (§3). PURE, no DB.

There are no typed zones anymore. Each index anchors a ladder on its OWN live
spot (compute.metrics.build_ladder); on that ladder we pick two walls per index,
re-centered + re-picked EACH TICK (v3 item 3):

  CAP   = highest CE OI at/above spot   (the resistance the trader fades)
  FLOOR = highest PE OI at/below spot   (the support the trader fades)

Re-picked from current OI every tick, but with hysteresis (`sticky_margin`): the
incumbent wall is held unless a challenger's OI beats it by the margin, so it doesn't
flip on tiny ties. `check_migration` still flags when a bigger neighbor exists
despite the sticky hold.
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

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
    spot: float,
    incumbent: Optional[int] = None,
    sticky_margin: float = 0.0,
) -> Optional[WallSelection]:
    """Highest-OI ladder strike of the side's type, on the correct side of spot.

    CAP fades CALL OI at/above spot (resistance); FLOOR fades PUT OI at/below spot
    (support). Re-picked each tick but STICKY: the incumbent wall is held unless a
    challenger's OI beats it by `sticky_margin` (hysteresis vs tiny ties). A forced
    re-pick happens when the incumbent is no longer eligible — off the re-centered
    ladder, or now on the wrong side of spot. Returns None if no strike of that type
    sits on the ladder at/above (CAP) / at/below (FLOOR) spot.
    """
    option_type = SIDE_OPTION[side]
    cands = set(ladder_strikes)

    def _eligible(strike: int) -> bool:
        if strike not in cands:
            return False
        return strike >= spot if side == "CAP" else strike <= spot

    oi_by_strike: Dict[int, int] = {
        s.strike: s.oi
        for s in strikes
        if s.option_type == option_type and _eligible(s.strike)
    }
    if not oi_by_strike:
        return None

    # Challenger = highest OI; ties break toward the ladder centre (≈ ATM).
    centre = (min(ladder_strikes) + max(ladder_strikes)) / 2
    challenger = max(oi_by_strike, key=lambda k: (oi_by_strike[k], -abs(k - centre)))

    # Stickiness: hold the still-eligible incumbent unless the challenger's OI beats
    # it by the margin — so the wall doesn't flip on tiny ties / noise.
    wall = challenger
    if (
        incumbent is not None
        and incumbent in oi_by_strike
        and incumbent != challenger
        and oi_by_strike[challenger] < oi_by_strike[incumbent] * (1 + sticky_margin)
    ):
        wall = incumbent

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
    chain: ChainSnapshot,
    ladder: Ladder,
    incumbents: Optional[Dict[Side, int]] = None,
    sticky_margin: float = 0.0,
) -> Dict[Side, WallSelection]:
    """Both walls (CAP via CE at/above spot, FLOOR via PE at/below spot), sticky.

    `incumbents` maps side → last tick's wall strike, for the hysteresis. Empty on
    the first tick / after an expiry roll → fresh picks.
    """
    out: Dict[Side, WallSelection] = {}
    if chain.spot is None:
        return out
    incumbents = incumbents or {}
    for side in ("CAP", "FLOOR"):
        sel = select_wall(
            chain.strikes, side, ladder.strikes,
            chain.index_name, chain.expiry, ladder.interval,
            spot=chain.spot, incumbent=incumbents.get(side), sticky_margin=sticky_margin,
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
