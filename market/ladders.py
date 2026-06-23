"""v3 — persist & read the locked spot-anchored ladders (the `ladders` table, §3).

One ladder per index per expiry, locked at session start so each rung has a clean
OI series. Keyed by `(trading_date, index_name, expiry)`: a rolled expiry has no
row → it locks fresh on the new chain (same expiry-roll behaviour as the walls).
INSERT … ON CONFLICT DO NOTHING makes concurrent lockers safe — first one wins.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List

from db.db import get_conn
from schemas.market import Ladder

logger = logging.getLogger(__name__)

_INSERT = """
INSERT INTO ladders
    (trading_date, index_name, expiry, spot_at_lock, atm, interval, strikes)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trading_date, index_name, expiry) DO NOTHING
"""


def get_locked_ladder_keys(trading_date: date) -> set:
    """Set of (index_name, expiry) ladders already locked for the day."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT index_name, expiry FROM ladders WHERE trading_date = %s",
            (trading_date,),
        )
        return {(r["index_name"], r["expiry"]) for r in cur.fetchall()}


def insert_ladders(trading_date: date, ladders: List[Ladder]) -> int:
    """Persist newly built ladders; returns the number actually inserted.

    Existing (trading_date, index_name, expiry) rows are left untouched (locked),
    so this is idempotent and safe to call every tick.
    """
    if not ladders:
        return 0
    inserted = 0
    with get_conn() as conn, conn.cursor() as cur:
        for lad in ladders:
            cur.execute(
                _INSERT,
                (trading_date, lad.index_name, lad.expiry, lad.spot_at_lock,
                 lad.atm, lad.interval, lad.strikes),
            )
            if cur.rowcount:
                inserted += 1
                logger.info(
                    "locked %s ladder atm=%s strikes=%s (expiry %s)",
                    lad.index_name, lad.atm, lad.strikes, lad.expiry,
                )
    return inserted


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
