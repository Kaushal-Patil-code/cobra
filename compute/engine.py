"""v3 — verdict orchestrator (§5, ties §3/§4/§6 together).

build_state() is the read-only compute step the /tick and /state endpoints call:
read the locked monitored strikes (CAP/FLOOR walls) for the day → pull each
strike's OI series → score every strike (compute.verdict) → assemble each index's
WALL signal → assess expiry/pin (compute.expiry) → attach max-pain/PCR metrics and
the RANGE-BROKEN check (§3/§6) → emit one dual-index SideVerdict per side.

Pure-ish: the only I/O is read accessors (compute.series, market.ladders,
market.metrics_store). No writes, no Fyers, no LLM. Persisting is the tick's job.

CLI:  uv run python -m compute.engine [YYYY-MM-DD] [window]
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

from compute.expiry import assess_from_dates
from compute.metrics import dominance_strength, ladder_broken
from compute.pairing import build_wall_callout, pair_ladders_by_level
from compute.series import read_monitored_strikes, read_oi_series
from compute.verdict import build_wall_signal, side_verdict, strike_signal
from config.thresholds import (
    DEFAULT_WINDOW_MINUTES,
    SERIES_LOOKBACK_MINUTES,
    WINDOW_CHOICES,
)
from market.ladders import get_ladders
from market.metrics_store import read_latest_metrics
from schemas.market import MonitoredStrike
from schemas.verdict import SideVerdict, VerdictState, WallSignal

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
_SIDE_ORDER = {"CAP": 0, "FLOOR": 1}   # CAP (resistance) first


def _index_wall(ms: MonitoredStrike, now: datetime, window: int, ladder=None) -> WallSignal:
    """Score this index/side's strikes, build its WallSignal, and attach the full
    scored ladder (for the level-paired table) + wall STRENGTH (dominance)."""
    since = now - timedelta(minutes=SERIES_LOOKBACK_MINUTES)
    # Score the monitored set (wall + neighbors — what the verdict reads) PLUS every
    # ladder rung (what the level-paired table needs). Union → a shared strike is
    # read once; a monitored neighbor off the ladder edge is still scored.
    to_score = list(ms.monitored)
    if ladder is not None and ladder.strikes:
        to_score += [s for s in ladder.strikes if s not in to_score]
    signals = {
        strike: strike_signal(
            ms.index_name, ms.option_type, strike, ms.expiry,
            read_oi_series(ms.index_name, ms.option_type, strike, ms.expiry, since),
            now, window, is_wall=(strike == ms.wall_strike),
        )
        for strike in to_score
    }
    ws = build_wall_signal(ms, signals)
    ws.broken_level = ms.broken_level
    if ladder is not None and ladder.strikes:
        ladder_set = set(ladder.strikes)
        ws.wall_off_ladder = ms.wall_strike not in ladder_set
        ws.ladder = [signals[s] for s in sorted(ladder.strikes, reverse=True)]
        # Strength: size the wall against the 8 VISIBLE rungs. Take wall OI from the
        # scored wall signal so an OFF-LADDER wall still gets a strength.
        wall_oi = ws.wall.oi_latest
        others = [signals[s].oi_latest for s in ladder.strikes
                  if s != ms.wall_strike and signals[s].oi_latest is not None]
        ws.dominance, ws.strength = dominance_strength(wall_oi, others)
    else:
        ws.wall_off_ladder = False
    return ws


def build_state(
    trading_date: Optional[date] = None,
    now: Optional[datetime] = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> VerdictState:
    """Compute the full dashboard state for `trading_date` (the /state payload).

    `now` (tz-aware) and `trading_date` default to the current IST moment/date —
    pass them explicitly in tests. Unknown `window_minutes` falls back to default.
    """
    now = now or datetime.now(IST)
    if window_minutes not in WINDOW_CHOICES:
        window_minutes = DEFAULT_WINDOW_MINUTES
    ist_now = now.astimezone(IST)
    if trading_date is None:
        trading_date = ist_now.date()
    weekday = ist_now.strftime("%a")

    base = dict(ts=now, trading_date=trading_date, weekday=weekday,
                window_minutes=window_minutes)

    monitored = read_monitored_strikes(trading_date)
    if not monitored:
        return VerdictState(**base, note="no walls locked yet for this date")

    # Same nearest expiry per index across sides — take the first seen.
    nifty_exp = next((m.expiry for m in monitored if m.index_name == "NIFTY"), None)
    sensex_exp = next((m.expiry for m in monitored if m.index_name == "SENSEX"), None)
    if nifty_exp is None:
        # Nifty is the primary index (§5.2); nothing to anchor on.
        return VerdictState(**base, note="no NIFTY walls locked — cannot build verdict")

    assessment = assess_from_dates(nifty_exp, sensex_exp, ist_now.date())

    # v3 §6/§3 context: latest max-pain/PCR per index + RANGE-BROKEN (spot left ladder).
    metrics_by_index = read_latest_metrics(trading_date)
    # "Last updated" = the freshest tick that actually stored data (real data time),
    # not `now`. Derived from the metrics we just read — no extra query.
    data_ts = max(
        (m.ts for m in metrics_by_index.values() if m.ts is not None),
        default=None,
    )
    ladders = get_ladders(trading_date)
    range_broken = [
        name for name, lad in ladders.items()
        if (m := metrics_by_index.get(name)) and m.spot is not None
        and ladder_broken(m.spot, lad.strikes)
    ]

    # Live Sensex/Nifty ratio (≈3.20) from the two spots — used to pair strikes by
    # LEVEL (Nifty × ratio) and shown so the dashboard reflects the exact ratio used.
    n_m = metrics_by_index.get("NIFTY")
    s_m = metrics_by_index.get("SENSEX")
    nspot = n_m.spot if n_m else None
    sspot = s_m.spot if s_m else None
    live_ratio = round(sspot / nspot, 4) if (nspot and sspot) else None

    # side -> {index_name: MonitoredStrike}
    by_side: Dict[str, Dict[str, MonitoredStrike]] = defaultdict(dict)
    for m in monitored:
        by_side[m.side][m.index_name] = m

    sides: list[SideVerdict] = []
    for side in sorted(by_side, key=lambda s: _SIDE_ORDER.get(s, 9)):
        idx = by_side[side]
        nifty_ms = idx.get("NIFTY")
        if nifty_ms is None:
            logger.warning("side %s has no NIFTY wall — skipping", side)
            continue
        nifty_wall = _index_wall(nifty_ms, now, window_minutes, ladders.get("NIFTY"))
        sensex_ms = idx.get("SENSEX")
        sensex_wall = (
            _index_wall(sensex_ms, now, window_minutes, ladders.get("SENSEX"))
            if sensex_ms else None
        )
        sv = side_verdict(side, nifty_wall, sensex_wall, assessment)
        # Pair the two scored ladders by level (Nifty rung ↔ closest Sensex rung).
        sv.paired = pair_ladders_by_level(
            nifty_wall.ladder,
            sensex_wall.ladder if sensex_wall else [],
            live_ratio,
        )
        sv.wall_callout = build_wall_callout(nifty_wall, sensex_wall, live_ratio)
        sides.append(sv)

    return VerdictState(
        **base, data_ts=data_ts, live_ratio=live_ratio, expiry=assessment,
        metrics=list(metrics_by_index.values()),
        range_broken=range_broken, sides=sides,
    )


def main() -> None:
    import json
    import sys

    from db.db import close_pool

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    td = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    window = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_WINDOW_MINUTES
    try:
        state = build_state(trading_date=td, window_minutes=window)
        print(json.dumps(state.model_dump(mode="json"), indent=2, default=str))
    finally:
        close_pool()


if __name__ == "__main__":
    main()
