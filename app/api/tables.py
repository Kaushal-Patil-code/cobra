"""v3 — history/all-tables read API (keyset-paginated).

Read-only browse of the data tables for the History view. One route per table (an
implicit allowlist — there is deliberately NO generic `/table/<name>`, so
`fyers_tokens` and `zones` can never be reached). All DB-backed: 503 without a DSN,
400 on bad args (incl. a malformed cursor), 503 on a genuine DB outage.

  GET /snapshots     raw per-strike OI series           (filters: index, option_type, strike, expiry)
  GET /metrics       index_metrics (max-pain / PCR)      (filter: index)
  GET /ladders       locked ladders                      (filter: index)
  GET /walls         locked CAP/FLOOR walls              (filters: index, side)
  GET /instruments   static registry (no pagination)

Shared list args: start, end (ISO dates), limit (<=1000), cursor (opaque), order (desc|asc).
"""
from __future__ import annotations

import logging
from datetime import date

import psycopg
from flask import Blueprint, abort, jsonify, request

from config.settings import settings
from market.pagination import clamp_limit
from market.history_reads import (
    read_instruments,
    read_ladders,
    read_metrics,
    read_snapshots,
    read_walls,
)

logger = logging.getLogger(__name__)

bp = Blueprint("tables", __name__)


def _require_db() -> None:
    if not settings.effective_dsn:
        abort(503, "DATABASE_URL not set")


def _date_arg(name: str):
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        abort(400, f"{name} must be an ISO date (YYYY-MM-DD)")


def _int_arg(name: str):
    raw = request.args.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        abort(400, f"{name} must be an integer")


def _order_arg() -> str:
    order = (request.args.get("order") or "desc").lower()
    if order not in ("asc", "desc"):
        abort(400, "order must be 'asc' or 'desc'")
    return order


def _side_arg():
    side = request.args.get("side")
    if side and side not in ("CAP", "FLOOR"):
        abort(400, "side must be CAP or FLOOR")
    return side


def _index_arg():
    idx = request.args.get("index")
    if idx and idx not in ("NIFTY", "SENSEX"):
        abort(400, "index must be NIFTY or SENSEX")
    return idx


def _common():
    """(start, end, limit, cursor, order) shared by every paginated route."""
    return (
        _date_arg("start"),
        _date_arg("end"),
        clamp_limit(request.args.get("limit")),
        request.args.get("cursor"),
        _order_arg(),
    )


def _serve(reader, **kwargs):
    """Run a reader, mapping a bad cursor → 400 and a DB outage → 503."""
    _require_db()
    try:
        return jsonify(reader(**kwargs))
    except ValueError as exc:  # malformed cursor
        abort(400, str(exc))
    except psycopg.Error as exc:
        logger.error("tables read DB error: %s", exc)
        abort(503, "database unavailable")


@bp.get("/snapshots")
def snapshots():
    start, end, limit, cursor, order = _common()
    return _serve(
        read_snapshots, start=start, end=end, index=_index_arg(),
        option_type=(request.args.get("option_type") or None),
        strike=_int_arg("strike"), expiry=_date_arg("expiry"),
        limit=limit, cursor=cursor, order=order,
    )


@bp.get("/metrics")
def metrics():
    start, end, limit, cursor, order = _common()
    return _serve(read_metrics, start=start, end=end, index=_index_arg(),
                  limit=limit, cursor=cursor, order=order)


@bp.get("/ladders")
def ladders():
    start, end, limit, cursor, order = _common()
    return _serve(read_ladders, start=start, end=end, index=_index_arg(),
                  limit=limit, cursor=cursor, order=order)


@bp.get("/walls")
def walls():
    start, end, limit, cursor, order = _common()
    return _serve(read_walls, start=start, end=end, index=_index_arg(),
                  side=_side_arg(), limit=limit, cursor=cursor, order=order)


@bp.get("/instruments")
def instruments():
    _require_db()
    try:
        return jsonify(read_instruments())
    except psycopg.Error as exc:
        logger.error("/instruments DB error: %s", exc)
        abort(503, "database unavailable")
