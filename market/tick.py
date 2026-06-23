"""Phase 5 — the unified session tick (fetch → store → lock → compute → persist).

This is the whole runtime cycle in one call, and it deliberately closes the
Phase-4 integration gap (FN-1): a SINGLE Fyers fetch feeds BOTH the stored
`snapshots` AND the wall lock, so the OI series we persist is guaranteed to cover
the monitored strikes the verdict reads — no chance of locking a wall we never
snapshotted.

  get client → fetch both chains (self-healing) → store snapshots
            → lock walls from the SAME chains (idempotent, once/day)
            → build_state (reads the snapshots) → persist verdicts (backtest log)

Pure orchestration; the Phase-6 `GET /tick` endpoint will just call run_tick().
Never raises — degrades to a summary dict so a scheduler ping is always answered.

CLI: uv run python -m market.tick [YYYY-MM-DD] [strikecount] [window]
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from compute.engine import build_state
from compute.metrics import index_metrics_from_chain
from compute.persist import persist_state
from config.thresholds import DEFAULT_WINDOW_MINUTES
from db.db import get_conn
from market.fetch import fetch_chains
from market.instruments import all_instruments
from market.metrics_store import insert_metrics
from market.selection import lock_walls
from market.store import snapshot_exists_since, store_chains

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# NSE/BSE continuous session (spec §6.2: cron runs 09:15–15:30 IST).
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# Advisory-lock key for single-flight ticks across workers (adjacent to the
# Fyers-login lock 728_051_193 in auth.startup; distinct so they never contend).
_TICK_LOCK_KEY = 728_051_194


def is_market_hours(now: Optional[datetime] = None) -> bool:
    """True during the trading session (09:15–15:30 IST, Mon–Fri). The cron
    already restricts the window; /tick re-checks defensively (force= overrides)."""
    ist = (now or datetime.now(IST)).astimezone(IST)
    return ist.weekday() < 5 and MARKET_OPEN <= ist.time() <= MARKET_CLOSE


def already_ticked_this_minute(now: Optional[datetime] = None) -> bool:
    """M5-2 de-dup: a snapshot already stored in the current clock minute means a
    tick ran this minute (e.g. a double-fired cron). Skip the rerun so we don't
    write duplicate snapshot + verdict rows that would skew the backtest counts."""
    n = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    minute_start = n.replace(second=0, microsecond=0)
    return snapshot_exists_since(minute_start)


def run_tick(
    trading_date: Optional[date] = None,
    strikecount: int = 10,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> dict:
    """Run one full cycle for `trading_date` (default: today IST). Returns a summary.

    This is a LIVE, same-day cycle: `build_state` windows the OI series off *now*,
    so run_tick is meant to run during the session. Passing a historical
    `trading_date` won't replay that day (and Fyers can't re-serve past intraday
    chains anyway) — it's there for same-day overrides / tests.

    Never raises: every stage degrades into the summary dict so a scheduler ping
    (the Phase-6 `GET /tick`) always gets an answer, and snapshots already stored
    are kept in the summary even if the later compute/persist step fails.
    """
    from auth import get_fyers_client  # lazy: avoid importing the Fyers SDK at import

    td = trading_date or datetime.now(IST).date()
    try:
        client = get_fyers_client()
    except Exception as exc:
        logger.error("auth failed — skipping tick: %s", exc)
        return {"trading_date": str(td), "error": f"auth: {exc}"}

    chains = fetch_chains(client, strikecount=strikecount)   # self-heals token expiry
    if not chains:
        logger.warning("no chains fetched — skipping tick for %s", td)
        return {"trading_date": str(td), "error": "no chains fetched"}

    # 1) Store the snapshots. The WHOLE chain is stored on purpose (not just the
    #    monitored strikes): the extra strikes give wall-migration (§3) and
    #    expiry re-lock the history they need. These chains are also reused to lock
    #    ladders/walls and to compute metrics, which guarantees snapshot↔wall
    #    coverage (FN-1) and that max-pain is over the same chain we snapshotted.
    summary: dict = {"trading_date": str(td), "snapshots": store_chains(chains)}

    # 2-5) From the SAME chains: store max-pain/PCR metrics (§6, no extra API),
    #     lock ladders + CAP/FLOOR walls (idempotent — re-attempts walls that only
    #     just came into range), compute the dual-index state, and persist it.
    #     Guarded so a DB hiccup never discards the already-stored snapshots.
    try:
        fetched_at = next(iter(chains.values())).fetched_at
        insts = all_instruments()
        metrics = [
            index_metrics_from_chain(ch, insts[name].strike_interval)
            for name, ch in chains.items() if name in insts
        ]
        summary["metrics_rows"] = insert_metrics(td, fetched_at, metrics)
        locked = lock_walls(td, chains)
        state = build_state(trading_date=td, window_minutes=window_minutes)
        written = persist_state(state)
        summary.update(
            locked=locked,
            verdicts_written=written,
            range_broken=state.range_broken,
            sides=[
                {"side": v.side, "verdict": v.verdict, "conviction": v.conviction,
                 "tag": v.tag}
                for v in state.sides
            ],
            note=state.note,
        )
    except Exception as exc:
        logger.exception("tick compute/persist failed after storing snapshots")
        summary["error"] = f"compute/persist: {exc}"
    return summary


def scheduled_tick(
    trading_date: Optional[date] = None,
    strikecount: int = 10,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> dict:
    """The internal-scheduler entry: gate, then run one tick (never raises).

    Wraps run_tick with the same guards the GET /tick endpoint applies, PLUS a
    Postgres advisory try-lock so that with multiple gunicorn workers each running
    a scheduler, only ONE actually ticks per firing:

      1. market-hours gate  — cheap, no DB/Fyers when the market is closed.
      2. advisory try-lock  — elects a single worker; the lock is held for the
                              whole tick and auto-releases when the txn commits.
      3. market-minute dedup — skip a second tick within the same clock minute.
    """
    if not is_market_hours():
        return {"skipped": True, "reason": "outside market hours (09:15–15:30 IST, Mon–Fri)"}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS got", (_TICK_LOCK_KEY,))
            if not cur.fetchone()["got"]:
                logger.info("scheduled tick: another worker holds the tick lock — skipping")
                return {"skipped": True, "reason": "tick lock held by another worker"}
            if already_ticked_this_minute():
                return {"skipped": True, "reason": "already ticked this minute"}
            return run_tick(
                trading_date=trading_date, strikecount=strikecount,
                window_minutes=window_minutes,
            )
    except Exception as exc:           # never let a scheduler job die on an error
        logger.exception("scheduled tick failed")
        return {"error": f"scheduled: {exc}"}


def main() -> None:
    import json
    import sys

    from db.db import close_pool

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    td = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    strikecount = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    window = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_WINDOW_MINUTES
    try:
        print(json.dumps(run_tick(td, strikecount, window), indent=2, default=str))
    finally:
        close_pool()


if __name__ == "__main__":
    main()
