"""v4 — dynamic wall detection + CAP/FLOOR wall refresh (§3).

Per tick (NOT lock-once): re-center each index's DISPLAY ladder on live spot, then
re-pick its CAP/FLOOR walls by WIDE-SCANNING the full chain in a spot window —
CAP = max CE OI in [spot, spot+reach], FLOOR = max PE OI in [spot-reach, spot] — so a
dominant wall beyond the 8 visible rungs is still found. `reach` is per index
(NIFTY = WALL_SCAN_REACH_POINTS; SENSEX scaled by the live ratio). Held with
hysteresis (WALL_STICKY_MARGIN) so the wall doesn't flip on tiny ties, and a
`broken_level` is tracked when spot crosses the wall (sticky BROKEN badge). The ladder
and the walls are UPSERTed (one mutable row per day/index/expiry), so a rolled expiry
starts fresh on the new chain. No human zone input; `lock_walls` refreshes every tick.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List, Optional, Tuple

import psycopg

from compute.strikes import compute_broken_level, plan_ladders, select_index_walls
from config.thresholds import WALL_SCAN_REACH_POINTS, WALL_STICKY_MARGIN
from db.db import get_conn
from market.fetch import fetch_chains
from market.instruments import all_instruments
from market.ladders import upsert_ladders
from schemas.market import ChainSnapshot

logger = logging.getLogger(__name__)

SIDES = ("CAP", "FLOOR")


def read_incumbent_walls(
    trading_date: date,
) -> Dict[Tuple[str, str, date], Tuple[Optional[int], Optional[int]]]:
    """{(side, index_name, expiry): (wall_strike, broken_level)} — last tick's walls
    (for stickiness + the sticky BROKEN badge). Degrades to (wall, None) if the
    broken_level column isn't migrated yet."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT side, index_name, expiry, wall_strike, broken_level "
                "FROM monitored_strikes WHERE trading_date = %s",
                (trading_date,),
            )
            return {
                (r["side"], r["index_name"], r["expiry"]):
                    (r["wall_strike"], r["broken_level"])
                for r in cur.fetchall()
            }
    except psycopg.errors.UndefinedColumn:
        logger.warning("monitored_strikes.broken_level missing — apply "
                       "db/migrations/v4_wall_dynamics.sql; BROKEN badge dormant")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT side, index_name, expiry, wall_strike "
            "FROM monitored_strikes WHERE trading_date = %s",
            (trading_date,),
        )
        return {
            (r["side"], r["index_name"], r["expiry"]): (r["wall_strike"], None)
            for r in cur.fetchall()
        }


_UPSERT_WALL = """
INSERT INTO monitored_strikes
    (trading_date, side, index_name, option_type, expiry,
     wall_strike, monitored, wall_oi_at_lock, broken_level)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trading_date, side, index_name, expiry) DO UPDATE
    SET option_type     = EXCLUDED.option_type,
        wall_strike     = EXCLUDED.wall_strike,
        monitored       = EXCLUDED.monitored,
        wall_oi_at_lock = EXCLUDED.wall_oi_at_lock,
        broken_level    = EXCLUDED.broken_level,
        locked_at       = now()
    WHERE monitored_strikes.wall_strike IS DISTINCT FROM EXCLUDED.wall_strike
       OR monitored_strikes.broken_level IS DISTINCT FROM EXCLUDED.broken_level
"""

_UPSERT_WALL_BASE = """
INSERT INTO monitored_strikes
    (trading_date, side, index_name, option_type, expiry,
     wall_strike, monitored, wall_oi_at_lock)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trading_date, side, index_name, expiry) DO UPDATE
    SET option_type     = EXCLUDED.option_type,
        wall_strike     = EXCLUDED.wall_strike,
        monitored       = EXCLUDED.monitored,
        wall_oi_at_lock = EXCLUDED.wall_oi_at_lock,
        locked_at       = now()
    WHERE monitored_strikes.wall_strike IS DISTINCT FROM EXCLUDED.wall_strike
"""


def _upsert_wall(cur, trading_date: date, side: str, sel, broken_level, with_broken=True) -> int:
    if with_broken:
        cur.execute(_UPSERT_WALL, (
            trading_date, side, sel.index_name, sel.option_type, sel.expiry,
            sel.wall_strike, sel.monitored, sel.wall_oi, broken_level))
    else:
        cur.execute(_UPSERT_WALL_BASE, (
            trading_date, side, sel.index_name, sel.option_type, sel.expiry,
            sel.wall_strike, sel.monitored, sel.wall_oi))
    return cur.rowcount


def lock_walls(trading_date: date, chains: Dict[str, ChainSnapshot]) -> List[dict]:
    """Re-center display ladders + wide-scan re-pick CAP/FLOOR walls (sticky), this
    tick. Detection scans the full chain in a per-index spot window; the ladder is
    display-only. Returns the walls that CHANGED (new pick, move, or badge change)."""
    instruments = all_instruments()
    ladders = plan_ladders(chains, instruments)   # display only
    if not ladders:
        logger.warning("no ladders to refresh for %s (no live spot?)", trading_date)
        return []
    upsert_ladders(trading_date, list(ladders.values()))

    incumbents = read_incumbent_walls(trading_date)   # {key: (wall, broken)}
    nspot = chains["NIFTY"].spot if "NIFTY" in chains and chains["NIFTY"].spot else None

    # Build all picks first (pure), then persist (so a missing column degrades cleanly).
    picks: List[Tuple[str, object, Optional[int]]] = []
    for name, chain in chains.items():
        inst = instruments.get(name)
        if inst is None or chain.spot is None:
            logger.warning("no instrument/spot for %s — cannot pick walls", name)
            continue
        reach = (WALL_SCAN_REACH_POINTS * (chain.spot / nspot) if nspot
                 else WALL_SCAN_REACH_POINTS * float(inst.price_mult))
        inc = {s: incumbents.get((s, name, chain.expiry), (None, None)) for s in SIDES}
        inc_wall = {s: inc[s][0] for s in SIDES}
        sels = select_index_walls(chain, inst.strike_interval, reach, inc_wall,
                                  WALL_STICKY_MARGIN)
        for side in SIDES:
            selc = sels.get(side)
            if selc is None:
                logger.warning(
                    "could not pick %s %s on expiry %s — no %s OI in the %s-pt window",
                    name, side, chain.expiry, "CE" if side == "CAP" else "PE", round(reach))
                continue
            broken = compute_broken_level(side, inc_wall[side], inc[side][1], chain.spot)
            picks.append((side, selc, broken))

    return _persist_picks(trading_date, picks)


def _persist_picks(trading_date: date, picks) -> List[dict]:
    """UPSERT all picks; retry the whole batch without broken_level if the column
    isn't migrated (never refreeze walls on a missed migration)."""
    for with_broken in (True, False):
        changed: List[dict] = []
        try:
            with get_conn() as conn, conn.cursor() as cur:
                for side, selc, broken in picks:
                    if _upsert_wall(cur, trading_date, side, selc, broken, with_broken):
                        changed.append({
                            "side": side, "index": selc.index_name,
                            "option_type": selc.option_type, "wall": selc.wall_strike,
                            "monitored": selc.monitored, "expiry": str(selc.expiry),
                            "broken_level": broken if with_broken else None,
                        })
                        logger.info("refreshed %s %s wall=%s broken=%s (expiry %s)",
                                    selc.index_name, side, selc.wall_strike,
                                    broken if with_broken else "n/a", selc.expiry)
            return changed
        except psycopg.errors.UndefinedColumn:
            logger.warning("monitored_strikes.broken_level missing — persisting walls "
                           "without the BROKEN badge; apply db/migrations/v4_wall_dynamics.sql")
            continue
    return []


def lock_session(trading_date: date, strikecount: int = 10) -> List[dict]:
    """Fetch both chains and refresh ladders + walls for this session tick."""
    from auth import get_fyers_client

    try:
        client = get_fyers_client()
    except Exception as exc:
        logger.error("auth failed — cannot refresh session: %s", exc)
        return []
    chains = fetch_chains(client, strikecount=strikecount)
    return lock_walls(trading_date, chains)
