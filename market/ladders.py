"""v3 item 3 — persist & read the spot-anchored ladders (the `ladders` table, §3).

One mutable ladder per index per expiry, RE-CENTERED on live spot each tick. Keyed
by `(trading_date, index_name, expiry)`: a rolled expiry has no row → it's created
fresh on the new chain. UPSERT … DO UPDATE … WHERE strikes changed: the row is
rewritten only when spot has drifted enough to shift the ladder, so a quiet tick is a
no-op. `snapshots` stores the whole chain, so a shifted rung still has a clean OI
series. (`spot_at_lock`/`locked_at` keep their names but now hold the spot and time at
the last re-center.)
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List

from db.db import get_conn
from schemas.market import Ladder

logger = logging.getLogger(__name__)

_UPSERT = """
INSERT INTO ladders
    (trading_date, index_name, expiry, spot_at_lock, atm, interval, strikes)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trading_date, index_name, expiry) DO UPDATE
    SET spot_at_lock = EXCLUDED.spot_at_lock,
        atm          = EXCLUDED.atm,
        interval     = EXCLUDED.interval,
        strikes      = EXCLUDED.strikes,
        locked_at    = now()
    WHERE ladders.strikes IS DISTINCT FROM EXCLUDED.strikes
"""


def get_locked_ladder_keys(trading_date: date) -> set:
    """Set of (index_name, expiry) ladders already locked for the day."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT index_name, expiry FROM ladders WHERE trading_date = %s",
            (trading_date,),
        )
        return {(r["index_name"], r["expiry"]) for r in cur.fetchall()}


def upsert_ladders(trading_date: date, ladders: List[Ladder]) -> int:
    """Re-center the day's ladders (UPSERT). Returns how many were created or shifted
    this tick; an unchanged ladder (spot hasn't crossed to a new ATM) is a no-op."""
    if not ladders:
        return 0
    changed = 0
    with get_conn() as conn, conn.cursor() as cur:
        for lad in ladders:
            cur.execute(
                _UPSERT,
                (trading_date, lad.index_name, lad.expiry, lad.spot_at_lock,
                 lad.atm, lad.interval, lad.strikes),
            )
            if cur.rowcount:
                changed += 1
                logger.info(
                    "re-centered %s ladder atm=%s strikes=%s (expiry %s)",
                    lad.index_name, lad.atm, lad.strikes, lad.expiry,
                )
    return changed


def get_ladders(trading_date: date) -> Dict[str, Ladder]:
    """The day's locked ladders, by index_name."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT index_name, expiry, spot_at_lock, atm, interval, strikes
            FROM ladders
            WHERE trading_date = %s
            """,
            (trading_date,),
        )
        rows = cur.fetchall()
    return {
        r["index_name"]: Ladder(
            index_name=r["index_name"], expiry=r["expiry"],
            spot_at_lock=float(r["spot_at_lock"]), atm=r["atm"],
            interval=r["interval"], strikes=list(r["strikes"]),
        )
        for r in rows
    }
