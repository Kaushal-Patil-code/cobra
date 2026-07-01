"""v4 — strike selection via wide-scan over the full chain (§4). PURE, no DB.

There are no typed zones anymore. Each index picks two walls per index,
re-picked EACH TICK (v4 wide scan):

  CAP   = highest CE OI in [spot, spot+scan_reach]   (the resistance the trader fades)
  FLOOR = highest PE OI in [spot-scan_reach, spot]   (the support the trader fades)

Detection scans the FULL chain within the spot window — the ladder is display-only
and does NOT limit wall candidates. Re-picked from current OI every tick, but with
hysteresis (`sticky_margin`): the incumbent wall is held unless a challenger's OI
beats it by the margin, so it doesn't flip on tiny ties. `check_migration` still
flags when a bigger neighbor exists despite the sticky hold. Ties break toward spot
(nearest actionable wall).
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

from compute.metrics import build_ladder, compute_atm
from schemas.market import ChainSnapshot, Instrument, Ladder, Side, StrikeOI
from schemas.strikes import MigrationFlag, WallSelection

# CAP fades CALL OI, FLOOR fades PUT OI (v4 §4).
SIDE_OPTION: Dict[Side, str] = {"CAP": "CE", "FLOOR": "PE"}


def select_wall(
    strikes: Sequence[StrikeOI],
    side: Side,
    index_name: str,
    expiry,
    interval: int,
    spot: float,
    scan_reach: float,
    incumbent: Optional[int] = None,
    sticky_margin: float = 0.0,
) -> Optional[WallSelection]:
    """Highest-OI strike of the side's type within a spot window (v4 wide scan).

    Candidates are FULL-CHAIN strikes (never limited to the displayed ladder):
    CAP scans CE OI in [spot, spot+scan_reach]; FLOOR scans PE OI in
    [spot-scan_reach, spot]. Re-picked each tick but STICKY: the incumbent is held
    unless a challenger's OI beats it by `sticky_margin`. Spot crossing the wall
    drops the incumbent out of the window → forced re-pick. Ties break toward spot
    (nearest actionable wall). None if no strike of that type sits in the window.
    """
    option_type = SIDE_OPTION[side]
    lo, hi = (spot, spot + scan_reach) if side == "CAP" else (spot - scan_reach, spot)

    oi_by_strike: Dict[int, int] = {
        s.strike: s.oi
        for s in strikes
        if s.option_type == option_type and lo <= s.strike <= hi
    }
    if not oi_by_strike:
        return None

    # Challenger = highest OI; ties break toward spot (nearest actionable wall).
    challenger = max(oi_by_strike, key=lambda k: (oi_by_strike[k], -abs(k - spot)))

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
    interval: int,
    scan_reach: float,
    incumbents: Optional[Dict[Side, int]] = None,
    sticky_margin: float = 0.0,
) -> Dict[Side, WallSelection]:
    """Both walls (CAP via CE above spot, FLOOR via PE below spot) over the scan
    window, sticky. `interval` is the index's strike step (for `monitored`);
    `scan_reach` is this index's own-points reach; `incumbents` maps side → last
    tick's wall strike for the hysteresis. `ChainSnapshot` carries no interval, so
    `lock_walls` passes the instrument's `strike_interval`."""
    out: Dict[Side, WallSelection] = {}
    if chain.spot is None:
        return out
    incumbents = incumbents or {}
    for side in ("CAP", "FLOOR"):
        sel = select_wall(
            chain.strikes, side, chain.index_name, chain.expiry, interval,
            spot=chain.spot, scan_reach=scan_reach,
            incumbent=incumbents.get(side), sticky_margin=sticky_margin,
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
