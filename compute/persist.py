"""v3 — persist computed verdicts to the `verdicts` backtest log (§1, §11).

The running service treats every verdict as a hypothesis to MEASURE, not gospel,
so each tick logs one row per side (CAP/FLOOR). `build_history` reads them back for
the per-weekday/DTE paper-test (§4). All DB I/O is delegated to
market.verdict_store; this module only maps VerdictState → rows and shapes reads.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from market.verdict_store import (
    bucket_counts,
    insert_verdicts,
    read_verdicts_range,
)
from schemas.verdict import BucketStat, SideVerdict, VerdictHistory, VerdictState

logger = logging.getLogger(__name__)


def _row(state: VerdictState, v: SideVerdict) -> dict:
    """One SideVerdict + its envelope → a verdicts-table row (the writable cols)."""
    return {
        "ts": state.ts,
        "trading_date": state.trading_date,
        "weekday": state.weekday,
        "window_minutes": state.window_minutes,
        "side": v.side,
        "option_type": v.option_type,
        "wall_strike": v.wall_strike,
        "verdict": v.verdict,
        "conviction": v.conviction,
        "meaning": v.meaning,
        "tag": v.tag,
        "nifty_sig": v.nifty_sig,
        "sensex_sig": v.sensex_sig,
        "dte_n": v.dte_n,
        "dte_s": v.dte_s,
        "suppressed": v.suppressed,
        "expiry_label": v.expiry_label,
        # Wall strength (size axis) per index — logged to tune the cutoffs on data.
        "nifty_strength": v.nifty.strength,
        "nifty_dominance": v.nifty.dominance,
        "sensex_strength": v.sensex.strength if v.sensex else None,
        "sensex_dominance": v.sensex.dominance if v.sensex else None,
    }


def persist_state(state: VerdictState) -> int:
    """Log one verdict row per side. No sides (e.g. walls not locked yet) → 0.

    Every side is logged — including quiet NO-SIGNAL and NIFTY-ONLY rows — because
    the backtest needs the full series, and the tag / weekday let us bucket
    distorted (expiry/pin) days out later (§4, §11).
    """
    if not state.sides:
        logger.info("no sides in state for %s — nothing to persist", state.trading_date)
        return 0
    n = insert_verdicts([_row(state, v) for v in state.sides])
    logger.info("persisted %d verdict rows for %s @ %s", n, state.trading_date, state.ts)
    return n


def build_history(start: date, end: date, side: Optional[str] = None) -> VerdictHistory:
    """Assemble the /history payload: in-range records + per-weekday buckets.

    `side` scopes BOTH the records and the buckets, so a CAP-only view shows
    CAP-only weekday counts (no all-side/filtered-records mismatch).
    """
    records = read_verdicts_range(start, end, side)
    buckets = [BucketStat(**b) for b in bucket_counts(start, end, side)]
    return VerdictHistory(start=start, end=end, records=records, weekday_buckets=buckets)
