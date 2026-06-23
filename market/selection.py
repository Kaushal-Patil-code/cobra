"""v3 — lock orchestration: spot-anchored ladders + CAP/FLOOR walls (§3).

Per tick (idempotent): build each index's ladder from live spot, persist it, then
lock the CAP (highest CE OI) and FLOOR (highest PE OI) walls of that ladder. There
is no human zone input. Reuses an existing lock for the same
`(side, index, expiry)`; a rolled expiry has no such row, so it locks fresh on the
new chain. INSERT … ON CONFLICT DO NOTHING makes concurrent lockers safe.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List

from compute.strikes import plan_ladders, plan_locks
from db.db import get_conn
from market.fetch import fetch_chains
from market.instruments import all_instruments
from market.ladders import insert_ladders
from schemas.market import ChainSnapshot, Instrument, Ladder
from schemas.strikes import WallSelection

logger = logging.getLogger(__name__)

SIDES = ("CAP", "FLOOR")


def get_locked_keys(trading_date: date) -> set:
    """Set of (side, index_name, expiry) walls already locked for the day."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT side, index_name, expiry FROM monitored_strikes WHERE trading_date = %s",
            (trading_date,),
        )
        return {(r["side"], r["index_name"], r["expiry"]) for r in cur.fetchall()}


def _insert_lock(cur, trading_date: date, side: str, sel: WallSelection) -> int:
    cur.execute(
        """
        INSERT INTO monitored_strikes
            (trading_date, side, index_name, option_type, expiry,
             wall_strike, monitored, wall_oi_at_lock)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (trading_date, side, index_name, expiry) DO NOTHING
        """,
        (trading_date, side, sel.index_name, sel.option_type, sel.expiry,
         sel.wall_strike, sel.monitored, sel.wall_oi),
    )
    return cur.rowcount  # 1 = inserted, 0 = already locked (ON CONFLICT)


def _warn_unlockable(
    chains: Dict[str, ChainSnapshot],
    instruments: Dict[str, Instrument],
    ladders: Dict[str, Ladder],
    coverable: set,
) -> None:
    """Surface any (index, side) we have a chain for but couldn't lock (spec §11)."""
    for name, chain in chains.items():
        if name not in instruments:
            continue
        if name not in ladders:
            logger.warning(
                "no ladder for %s (no live spot in the chain?) — cannot lock walls", name
            )
            continue
        for side in SIDES:
            if (side, name, chain.expiry) not in coverable:
                logger.warning(
                    "could not lock %s %s on expiry %s — wall strike fell outside the "
                    "fetched chain; widen strikecount or use the depth() fallback",
                    name, side, chain.expiry,
                )


def lock_walls(trading_date: date, chains: Dict[str, ChainSnapshot]) -> List[dict]:
    """Lock ladders + CAP/FLOOR walls for each index against the given chains."""
    instruments = all_instruments()
    ladders = plan_ladders(chains, instruments)
    if not ladders:
        logger.warning("no ladders to lock for %s (no live spot?)", trading_date)
        return []

    insert_ladders(trading_date, list(ladders.values()))   # idempotent (ON CONFLICT)

    already = get_locked_keys(trading_date)
    planned = plan_locks(chains, ladders, already)

    coverable = already | {(side, sel.index_name, sel.expiry) for side, sel in planned}
    _warn_unlockable(chains, instruments, ladders, coverable)

    if not planned:
        logger.info("walls already locked for %s (nothing new)", trading_date)
        return []

    locked = []  # only rows this call actually inserted (accurate under races)
    with get_conn() as conn, conn.cursor() as cur:
        for side, sel in planned:
            if _insert_lock(cur, trading_date, side, sel):
                locked.append((side, sel))
                logger.info(
                    "locked %s %s wall=%s monitored=%s (expiry %s)",
                    sel.index_name, side, sel.wall_strike, sel.monitored, sel.expiry,
                )

    return [
        {
            "side": side, "index": sel.index_name, "option_type": sel.option_type,
            "wall": sel.wall_strike, "monitored": sel.monitored, "expiry": str(sel.expiry),
        }
        for side, sel in locked
    ]


def lock_session(trading_date: date, strikecount: int = 10) -> List[dict]:
    """Fetch both chains and lock ladders + walls for the session."""
    from auth import get_fyers_client

    try:
        client = get_fyers_client()
    except Exception as exc:
        logger.error("auth failed — cannot lock session: %s", exc)
        return []
    chains = fetch_chains(client, strikecount=strikecount)
    return lock_walls(trading_date, chains)
