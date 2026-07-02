"""Read-only, keyset-paginated readers for the history/all-tables view.

One reader per browsable table. Each builds a parameterised query (filters are
always bound params; table/column names are hardcoded — never from the request,
so no SQL injection) and returns the paginated response envelope.

Browsable: snapshots, index_metrics, ladders, monitored_strikes, instruments.
NEVER exposed: fyers_tokens (live bearer tokens), zones (deprecated/empty in v3).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from market.pagination import build_envelope, decode_cursor, jsonable
from db.db import get_conn

logger = logging.getLogger(__name__)


def _keyset(
    table: str,
    cols: str,
    key_col: str,
    *,
    where: Optional[list] = None,
    params: Optional[list] = None,
    cursor: Optional[str] = None,
    limit: int = 100,
    order: str = "desc",
) -> dict:
    """Run a keyset-paginated SELECT and return the response envelope.

    `table`/`cols`/`key_col` are trusted constants from the callers below; only the
    `where`/`params` values come from the request (bound, never interpolated).
    Orders by `(key_col, id)` and pages with a `(key_col, id) </> (cursor)` guard.
    """
    where = list(where or [])
    params = list(params or [])
    direction = "ASC" if order == "asc" else "DESC"
    cmp = ">" if order == "asc" else "<"

    cur = decode_cursor(cursor)
    if cur:
        where.append(f"({key_col}, id) {cmp} (%s::timestamptz, %s::uuid)")
        params += [cur[0], cur[1]]

    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT {cols} FROM {table} {wsql} "
        f"ORDER BY {key_col} {direction}, id {direction} LIMIT %s"
    )
    params.append(limit + 1)  # +1 to detect a next page

    with get_conn() as conn, conn.cursor() as c:
        c.execute(sql, params)
        rows = [jsonable(r) for r in c.fetchall()]
    return build_envelope(rows, limit, key_col)


def _ts_range(where: list, params: list, start: Optional[date], end: Optional[date], col: str = "ts") -> None:
    """Append an inclusive date-range filter on a timestamptz column (by its date)."""
    if start is not None:
        where.append(f"{col}::date >= %s")
        params.append(start)
    if end is not None:
        where.append(f"{col}::date <= %s")
        params.append(end)


def _date_range(where: list, params: list, start: Optional[date], end: Optional[date], col: str) -> None:
    """Append an inclusive range filter on a plain date column."""
    if start is not None:
        where.append(f"{col} >= %s")
        params.append(start)
    if end is not None:
        where.append(f"{col} <= %s")
        params.append(end)


def read_snapshots(*, start=None, end=None, index=None, option_type=None, strike=None,
                   expiry=None, limit=100, cursor=None, order="desc") -> dict:
    """`snapshots` — raw per-strike OI series. Keyed on (ts, id)."""
    where, params = [], []
    _ts_range(where, params, start, end)
    if index:
        where.append("index_name = %s"); params.append(index)
    if option_type:
        where.append("option_type = %s"); params.append(option_type)
    if strike is not None:
        where.append("strike = %s"); params.append(strike)
    if expiry is not None:
        where.append("expiry = %s"); params.append(expiry)
    return _keyset(
        "snapshots",
        "id, ts, index_name, option_type, strike, expiry, oi, ltp, volume, prev_oi, oichp",
        "ts", where=where, params=params, cursor=cursor, limit=limit, order=order,
    )


def read_metrics(*, start=None, end=None, index=None, limit=100, cursor=None, order="desc") -> dict:
    """`index_metrics` — max-pain / PCR / spot per index per tick. Keyed on (ts, id)."""
    where, params = [], []
    _ts_range(where, params, start, end)
    if index:
        where.append("index_name = %s"); params.append(index)
    return _keyset(
        "index_metrics",
        "id, ts, trading_date, index_name, expiry, spot, atm, max_pain, pcr, call_oi, put_oi, vix",
        "ts", where=where, params=params, cursor=cursor, limit=limit, order=order,
    )


def read_ladders(*, start=None, end=None, index=None, limit=100, cursor=None, order="desc") -> dict:
    """`ladders` — the locked spot-anchored ladder per day/index. Keyed on (locked_at, id)."""
    where, params = [], []
    _date_range(where, params, start, end, "trading_date")
    if index:
        where.append("index_name = %s"); params.append(index)
    return _keyset(
        "ladders",
        "id, locked_at, trading_date, index_name, expiry, spot_at_lock, atm, interval, strikes",
        "locked_at", where=where, params=params, cursor=cursor, limit=limit, order=order,
    )


def read_walls(*, start=None, end=None, index=None, side=None, limit=100, cursor=None, order="desc") -> dict:
    """`monitored_strikes` — locked CAP/FLOOR walls. Keyed on (locked_at, id)."""
    where, params = [], []
    _date_range(where, params, start, end, "trading_date")
    if index:
        where.append("index_name = %s"); params.append(index)
    if side:
        where.append("side = %s"); params.append(side)
    return _keyset(
        "monitored_strikes",
        "id, locked_at, trading_date, side, index_name, option_type, expiry, "
        "wall_strike, monitored, wall_oi_at_lock",
        "locked_at", where=where, params=params, cursor=cursor, limit=limit, order=order,
    )


def read_instruments() -> dict:
    """`instruments` — static registry; tiny, returned whole (no pagination)."""
    with get_conn() as conn, conn.cursor() as c:
        c.execute(
            "SELECT name, symbol, strike_interval, lot_size, expiry_weekday, "
            "price_mult, is_active FROM instruments ORDER BY name"
        )
        rows = [jsonable(r) for r in c.fetchall()]
    return {"items": rows, "page": {"limit": len(rows), "count": len(rows),
                                    "has_more": False, "next_cursor": None}}
