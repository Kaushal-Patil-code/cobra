"""v3 item 3 — dynamic ladder + CAP/FLOOR wall refresh (§3).

Per tick (NOT lock-once): re-center each index's ladder on live spot, then re-pick
its CAP (highest CE OI at/above spot) and FLOOR (highest PE OI at/below spot) walls
from current OI — with hysteresis (WALL_STICKY_MARGIN) so the wall doesn't flip on
tiny ties. The ladder and the walls are UPSERTed (one mutable row per
day/index/expiry), so a rolled expiry starts fresh on the new chain. No human zone
input. `lock_walls` keeps its name for callers but now *refreshes* every tick.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List, Optional, Tuple

from compute.strikes import plan_ladders, select_index_walls
from config.thresholds import WALL_STICKY_MARGIN
from db.db import get_conn
from market.fetch import fetch_chains
from market.instruments import all_instruments
from market.ladders import upsert_ladders
from schemas.market import ChainSnapshot

logger = logging.getLogger(__name__)

SIDES = ("CAP", "FLOOR")


def read_incumbent_walls(trading_date: date) -> Dict[Tuple[str, str, date], int]:
    """{(side, index_name, expiry): wall_strike} — last tick's walls (for stickiness)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT side, index_name, expiry, wall_strike "
            "FROM monitored_strikes WHERE trading_date = %s",
            (trading_date,),
        )
        return {
            (r["side"], r["index_name"], r["expiry"]): r["wall_strike"]
            for r in cur.fetchall()
        }


_UPSERT_WALL = """
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


def _upsert_wall(cur, trading_date: date, side: str, sel) -> int:
    cur.execute(
        _UPSERT_WALL,
        (trading_date, side, sel.index_name, sel.option_type, sel.expiry,
         sel.wall_strike, sel.monitored, sel.wall_oi),
    )
    return cur.rowcount  # 1 = newly created or moved, 0 = sticky hold (unchanged)


def lock_walls(trading_date: date, chains: Dict[str, ChainSnapshot]) -> List[dict]:
    """Re-center ladders + re-pick CAP/FLOOR walls (sticky) for each index, this tick.

    Returns only the walls that CHANGED this tick (a new pick or a move); a sticky
    hold is a no-op and is not returned. `build_state` reads the freshly-UPSERTed
    rows regardless, so an empty return just means nothing moved.
    """
    instruments = all_instruments()
    ladders = plan_ladders(chains, instruments)   # re-centered on current spot
    if not ladders:
        logger.warning("no ladders to refresh for %s (no live spot?)", trading_date)
        return []

    upsert_ladders(trading_date, list(ladders.values()))
    incumbents = read_incumbent_walls(trading_date)

    changed: List[dict] = []
    with get_conn() as conn, conn.cursor() as cur:
        for name, chain in chains.items():
            ladder = ladders.get(name)
            if ladder is None:
                logger.warning("no ladder for %s (no live spot?) — cannot pick walls", name)
                continue
            inc: Dict[str, Optional[int]] = {
                s: incumbents.get((s, name, chain.expiry)) for s in SIDES
            }
            sels = select_index_walls(chain, ladder, inc, WALL_STICKY_MARGIN)
            for side in SIDES:
                sel = sels.get(side)
                if sel is None:
                    logger.warning(
                        "could not pick %s %s on expiry %s — no %s OI at/%s spot on the ladder",
                        name, side, chain.expiry,
                        "CE" if side == "CAP" else "PE",
                        "above" if side == "CAP" else "below",
                    )
                    continue
                if _upsert_wall(cur, trading_date, side, sel):
                    changed.append({
                        "side": side, "index": sel.index_name,
                        "option_type": sel.option_type, "wall": sel.wall_strike,
                        "monitored": sel.monitored, "expiry": str(sel.expiry),
                    })
                    logger.info(
                        "refreshed %s %s wall=%s monitored=%s (expiry %s)",
                        sel.index_name, side, sel.wall_strike, sel.monitored, sel.expiry,
                    )
    return changed


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
