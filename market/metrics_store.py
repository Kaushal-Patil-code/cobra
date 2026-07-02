"""v3 — persist & read per-index chain metrics (the `index_metrics` table, §6).

Max-pain + PCR per index, appended every tick (append-only, like snapshots). The
dashboard reads the latest row per index for /state; the per-tick history is kept
for the backtest (e.g. did the pin magnet predict the close on expiry days?).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Dict, List

from db.db import get_conn
from schemas.market import IndexMetrics

logger = logging.getLogger(__name__)

_INSERT = """
INSERT INTO index_metrics
    (ts, trading_date, index_name, expiry, spot, atm, max_pain, pcr, call_oi, put_oi)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def insert_metrics(trading_date: date, ts: datetime, metrics: List[IndexMetrics]) -> int:
    """Append one metrics row per index for this tick; returns rows written."""
    if not metrics:
        return 0
    values = [
        (ts, trading_date, m.index_name, m.expiry, m.spot, m.atm,
         m.max_pain, m.pcr, m.call_oi, m.put_oi)
        for m in metrics
    ]
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(_INSERT, values)
    logger.info("stored %d index_metrics rows for %s @ %s", len(values), trading_date, ts)
    return len(values)


def read_latest_metrics(trading_date: date) -> Dict[str, IndexMetrics]:
    """The most-recent metrics row per index for the day (for /state), by index_name."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (index_name)
                   index_name, expiry, ts, spot, atm, max_pain, pcr, call_oi, put_oi
            FROM index_metrics
            WHERE trading_date = %s
            ORDER BY index_name, ts DESC
            """,
            (trading_date,),
        )
        rows = cur.fetchall()
    return {
        r["index_name"]: IndexMetrics(
            index_name=r["index_name"], expiry=r["expiry"], ts=r["ts"],
            spot=float(r["spot"]) if r["spot"] is not None else None,
            atm=r["atm"], max_pain=r["max_pain"],
            pcr=float(r["pcr"]) if r["pcr"] is not None else None,
            call_oi=r["call_oi"], put_oi=r["put_oi"],
        )
        for r in rows
    }
