"""Phase 4 read side — load the strikes to evaluate and their OI time-series.

The verdict orchestrator calls these to (1) discover which locked walls +
monitored neighbors exist for the day, and (2) pull each strike's (ts, oi)
series for the windowed Δ% and read-streak math (spec §5.1). Both functions are
plain reads — they open a pooled connection only when called, never at import.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from db.db import get_conn
from schemas.market import MonitoredStrike, SnapshotPoint

logger = logging.getLogger(__name__)


def read_monitored_strikes(trading_date: date) -> List[MonitoredStrike]:
    """All locked walls + monitored neighbors for the day (the strikes to evaluate)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT trading_date, side, index_name, option_type, expiry,
                   wall_strike, monitored, wall_oi_at_lock, broken_level
            FROM monitored_strikes
            WHERE trading_date = %s
            ORDER BY side, index_name
            """,
            (trading_date,),
        )
        rows = cur.fetchall()
    return [MonitoredStrike(**r) for r in rows]


def read_earliest_snapshot_ts(since: datetime) -> Optional[datetime]:
    """The earliest snapshot ts at-or-after `since` — the session's first read.

    Backs the §5.5 warm-up estimate ("first read in ~N min"): how long until a
    strike's OI series spans a full Δ% window. None when no snapshot exists yet.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT min(ts) AS first_ts FROM snapshots WHERE ts >= %s", (since,))
        row = cur.fetchone()
    return row["first_ts"] if row and row["first_ts"] is not None else None


def read_oi_series(
    index_name: str, option_type: str, strike: int, expiry: date, since: datetime
) -> List[SnapshotPoint]:
    """One strike's (ts, oi) series with ts >= since, ASCENDING by ts.

    Backed by snapshots_series_idx (index_name, option_type, strike, expiry,
    ts desc); the planner walks it forward to satisfy the ascending order.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts, oi
            FROM snapshots
            WHERE index_name = %s
              AND option_type = %s
              AND strike = %s
              AND expiry = %s
              AND ts >= %s
            ORDER BY ts ASC
            """,
            (index_name, option_type, strike, expiry, since),
        )
        rows = cur.fetchall()
    return [SnapshotPoint(ts=r["ts"], oi=r["oi"]) for r in rows]


def read_latest_oi(
    index_name: str, option_type: str, expiry: date,
    strikes: Sequence[int], since: datetime,
) -> Dict[int, int]:
    """Latest OI per strike for a set of strikes (one query) — for wall strength.

    Returns {strike: latest_oi} for the most recent snapshot of each strike with
    ts >= since. Used to size the wall against the whole ladder (dominance).
    """
    if not strikes:
        return {}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (strike) strike, oi
            FROM snapshots
            WHERE index_name = %s
              AND option_type = %s
              AND expiry = %s
              AND strike = ANY(%s)
              AND ts >= %s
            ORDER BY strike, ts DESC
            """,
            (index_name, option_type, expiry, list(strikes), since),
        )
        return {r["strike"]: r["oi"] for r in cur.fetchall()}
