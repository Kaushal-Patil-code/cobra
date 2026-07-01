"""v3 — the verdicts-table store (the backtest dataset; §1, §11).

WRITE + read layer for the `verdicts` table. The verdict orchestrator persists one
row per SIDE (CAP/FLOOR) per tick (append-only, mirroring snapshots), and the
backtest tooling reads them back day-by-day, over a range, and bucketed per weekday
for the §4 per-weekday rule judging. Plain reads/writes — a pooled connection is
opened only when called, never at import.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

import psycopg

from db.db import get_conn
from schemas.verdict import VerdictRecord

logger = logging.getLogger(__name__)

# Writable columns, in fixed order — the INSERT and the value tuple track this.
_INSERT = """
INSERT INTO verdicts
    (ts, trading_date, weekday, window_minutes, side, option_type, wall_strike,
     verdict, conviction, meaning, tag, nifty_sig, sensex_sig, dte_n, dte_s,
     suppressed, expiry_label,
     nifty_strength, nifty_dominance, sensex_strength, sensex_dominance)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s)
"""

# Fallback for a pre-migration deploy: the 17 core columns only (the first 17 of the
# value tuple). If db/migrations/v3_wall_strength.sql hasn't been applied yet, the
# full INSERT raises UndefinedColumn — we degrade to this so core verdict logging
# survives instead of losing every row until the migration runs.
_BASE_COLS = 17
_INSERT_BASE = """
INSERT INTO verdicts
    (ts, trading_date, weekday, window_minutes, side, option_type, wall_strike,
     verdict, conviction, meaning, tag, nifty_sig, sensex_sig, dte_n, dte_s,
     suppressed, expiry_label)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# Mon..Sun fixed order for per-weekday bucketing (the column holds these strings).
_WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def insert_verdicts(rows: List[dict]) -> int:
    """Bulk-insert verdict rows (one per side per tick); returns rows written.

    Each dict carries the writable keys; values are extracted in the fixed column
    order above (required keys read directly, nullable ones via .get so a missing
    optional key becomes NULL). Append-only — no ON CONFLICT; tick-layer de-dup
    is handled by the /tick market-minute guard.
    """
    if not rows:
        logger.warning("no verdict rows to insert")
        return 0
    values = [
        (
            r["ts"], r["trading_date"], r["weekday"], r["window_minutes"],
            r["side"], r["option_type"], r.get("wall_strike"),
            r["verdict"], r["conviction"], r.get("meaning"), r.get("tag"),
            r.get("nifty_sig"), r.get("sensex_sig"), r.get("dte_n"), r.get("dte_s"),
            r.get("suppressed", False), r.get("expiry_label"),
            r.get("nifty_strength"), r.get("nifty_dominance"),
            r.get("sensex_strength"), r.get("sensex_dominance"),
        )
        for r in rows
    ]
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.executemany(_INSERT, values)
    except psycopg.errors.UndefinedColumn:
        logger.warning(
            "verdicts strength columns missing — apply db/migrations/v3_wall_strength.sql; "
            "logging %d base rows without strength/dominance for now", len(values),
        )
        base = [v[:_BASE_COLS] for v in values]
        with get_conn() as conn, conn.cursor() as cur:
            cur.executemany(_INSERT_BASE, base)
    logger.info("inserted %d verdict rows", len(values))
    return len(values)


def read_verdicts(trading_date: date) -> List[VerdictRecord]:
    """All verdict rows for the day, newest first then by side."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM verdicts
            WHERE trading_date = %s
            ORDER BY ts DESC, side
            """,
            (trading_date,),
        )
        rows = cur.fetchall()
    return [VerdictRecord(**r) for r in rows]


def read_verdicts_range(
    start: date, end: date, side: Optional[str] = None
) -> List[VerdictRecord]:
    """Verdicts with trading_date BETWEEN start AND end (inclusive), optional
    side filter, newest day/tick first then by side."""
    with get_conn() as conn, conn.cursor() as cur:
        if side is None:
            cur.execute(
                """
                SELECT *
                FROM verdicts
                WHERE trading_date BETWEEN %s AND %s
                ORDER BY trading_date DESC, ts DESC, side
                """,
                (start, end),
            )
        else:
            cur.execute(
                """
                SELECT *
                FROM verdicts
                WHERE trading_date BETWEEN %s AND %s
                  AND side = %s
                ORDER BY trading_date DESC, ts DESC, side
                """,
                (start, end, side),
            )
        rows = cur.fetchall()
    return [VerdictRecord(**r) for r in rows]


def bucket_counts(start: date, end: date, side: Optional[str] = None) -> List[dict]:
    """Per-weekday summary over the range for the §4 per-weekday judging.

    GROUP BY weekday, verdict in SQL, then assemble one dict per weekday shaped
    like BucketStat, ordered Mon..Sun. `side` (optional) scopes the buckets to one
    side so they line up with a side-filtered record list.
    """
    with get_conn() as conn, conn.cursor() as cur:
        if side is None:
            cur.execute(
                """
                SELECT weekday, verdict, count(*) AS n
                FROM verdicts
                WHERE trading_date BETWEEN %s AND %s
                GROUP BY weekday, verdict
                """,
                (start, end),
            )
        else:
            cur.execute(
                """
                SELECT weekday, verdict, count(*) AS n
                FROM verdicts
                WHERE trading_date BETWEEN %s AND %s
                  AND side = %s
                GROUP BY weekday, verdict
                """,
                (start, end, side),
            )
        rows = cur.fetchall()

    # weekday -> {"count": total, "by_verdict": {verdict: n}}
    by_weekday: dict = {}
    for r in rows:
        wd = r["weekday"]
        n = r["n"]
        bucket = by_weekday.setdefault(wd, {"count": 0, "by_verdict": {}})
        bucket["count"] += n
        bucket["by_verdict"][r["verdict"]] = n

    return [
        {
            "bucket": wd,
            "count": by_weekday[wd]["count"],
            "by_verdict": by_weekday[wd]["by_verdict"],
        }
        for wd in _WEEKDAY_ORDER
        if wd in by_weekday
    ]
