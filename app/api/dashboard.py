"""v3 — the dashboard HTTP API (§6/§7).

Thin HTTP shells over compute.* / market.* — the dashboard polls the reads and a
cron drives the tick. v3 has NO manual input: the ladder is anchored on live spot,
so there are no /zones or /set-zones endpoints.

  GET  /state                latest dual-index verdict + metrics (dashboard payload)
  GET  /history              logged verdicts + weekday buckets (backtest review)
  GET  /tick                 fetch → store → metrics → lock → compute → persist (cron)

All are DB-backed; with DATABASE_URL unset they return 503 (like /health/db). A
genuine DB outage maps to 503; anything else falls through to the 500 handler.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import psycopg
from flask import Blueprint, abort, jsonify, request

from compute.engine import build_state
from compute.persist import build_history
from config.settings import settings
from config.thresholds import DEFAULT_WINDOW_MINUTES, WINDOW_CHOICES

logger = logging.getLogger(__name__)

bp = Blueprint("dashboard", __name__)

IST = timezone(timedelta(hours=5, minutes=30))
STRIKECOUNT_DEFAULT = 10
HISTORY_DEFAULT_DAYS = 14


def _today_ist() -> date:
    return datetime.now(IST).date()


def _require_db() -> None:
    if not settings.effective_dsn:
        abort(503, "DATABASE_URL not set")


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        abort(400, f"{name} must be an integer")


def _date_arg(name: str):
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        abort(400, f"{name} must be an ISO date (YYYY-MM-DD)")


def _window_arg() -> int:
    window = _int_arg("window", DEFAULT_WINDOW_MINUTES)
    return window if window in WINDOW_CHOICES else DEFAULT_WINDOW_MINUTES


@bp.get("/state")
def state():
    """Latest dual-index verdict for the day (the live dashboard payload)."""
    _require_db()
    try:
        st = build_state(trading_date=_date_arg("date"), window_minutes=_window_arg())
    except psycopg.Error as exc:
        logger.error("/state DB error: %s", exc)
        abort(503, "database unavailable")
    return jsonify(st.model_dump(mode="json"))


@bp.get("/history")
def history():
    """Logged verdicts + per-weekday buckets over a date range (backtest review)."""
    _require_db()
    end = _date_arg("end") or _today_ist()
    start = _date_arg("start") or (end - timedelta(days=HISTORY_DEFAULT_DAYS))
    side = request.args.get("side")
    if side == "ALL":
        side = None
    if side is not None and side not in ("CAP", "FLOOR"):
        abort(400, "side must be CAP, FLOOR, or ALL")
    try:
        hist = build_history(start, end, side)
    except psycopg.Error as exc:
        logger.error("/history DB error: %s", exc)
        abort(503, "database unavailable")
    return jsonify(hist.model_dump(mode="json"))


@bp.get("/tick")
def tick():
    """Run one pipeline cycle (cron-driven). Skips outside market hours or if a
    tick already ran this minute; `?force=true` overrides both guards."""
    _require_db()
    # Deferred so /state and /history don't pull the Fyers SDK (via market.fetch)
    # at app boot — only /tick needs it.
    from market.tick import already_ticked_this_minute, is_market_hours, run_tick

    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    if not force and not is_market_hours():
        return jsonify(skipped=True, reason="outside market hours (09:15–15:30 IST, Mon–Fri)")
    if not force and already_ticked_this_minute():
        return jsonify(skipped=True, reason="already ticked this minute")

    result = run_tick(
        strikecount=_int_arg("strikecount", STRIKECOUNT_DEFAULT),
        window_minutes=_window_arg(),
    )
    return jsonify(result), (503 if result.get("error") else 200)
