"""Persist a ChainSnapshot's strikes into the `snapshots` table (append-only).

All rows of one fetch share a single `ts` (chain.fetched_at) so each strike has
a clean, aligned time-series for the Δ% math in Phase 4.
"""
from __future__ import annotations

import logging
from typing import Dict

from db.db import get_conn
from schemas.market import ChainSnapshot

logger = logging.getLogger(__name__)

_INSERT = """
INSERT INTO snapshots
    (ts, index_name, option_type, strike, expiry, oi, ltp, volume, prev_oi, oichp)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def store_snapshot(chain: ChainSnapshot) -> int:
    """Insert every strike of `chain`; returns the number of rows written."""
    rows = [
        (
            chain.fetched_at, s.index_name, s.option_type, s.strike, s.expiry,
            s.oi, s.ltp, s.volume, s.prev_oi, s.oichp,
        )
        for s in chain.strikes
    ]
    if not rows:
        logger.warning("no strikes to store for %s", chain.index_name)
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(_INSERT, rows)
    logger.info(
        "stored %d snapshot rows for %s (expiry %s) @ %s",
        len(rows), chain.index_name, chain.expiry, chain.fetched_at,
    )
    return len(rows)


def store_chains(chains: Dict[str, ChainSnapshot]) -> Dict[str, int]:
    """Store every chain's strikes; per-index resilient (spec §11).

    One index failing to store never aborts the others. Returns
    {index_name: rows_written} (0 for an index whose store raised).
    """
    out: Dict[str, int] = {}
    for name, chain in chains.items():
        try:
            out[name] = store_snapshot(chain)
        except Exception as exc:
            logger.error("store failed for %s: %s", name, exc)
            out[name] = 0
    return out


def snapshot_exists_since(ts) -> bool:
    """True if any snapshot row has ts >= `ts` — backs the /tick market-minute
    de-dup (a double-fired cron within the same minute is skipped)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM snapshots WHERE ts >= %s LIMIT 1", (ts,))
        return cur.fetchone() is not None
