"""Orchestrate fetch cycles: pull both chains (2 calls) → parse → store.

CLI: uv run python -m market.fetch [strikecount]

fetch_chains() returns parsed ChainSnapshots (no storage) — reused by the Phase 3
wall-locking. run_fetch() additionally persists each chain to `snapshots`. All
rows of a cycle share one `fetched_at`. Resilient per-index (spec §11): one index
failing never aborts the other. The index list + symbols come from the
`instruments` registry.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict

from auth import get_fyers_client
from db.db import close_pool
from market.chain import TokenRejectedError, fetch_raw_chain
from market.instruments import all_instruments
from market.parse import parse_chain
from market.store import store_snapshot
from schemas.market import ChainSnapshot

logger = logging.getLogger(__name__)


def _relogin():
    """Re-authenticate (single-flight across workers) and return a fresh client."""
    from auth.startup import relogin_single_flight

    logger.warning("Fyers token rejected — re-authenticating (single-flight)")
    return relogin_single_flight()


def fetch_chains(client, strikecount: int = 10) -> Dict[str, ChainSnapshot]:
    """Pull + parse the chain for every active instrument (no storage).

    One shared `fetched_at` for the cycle. A failure on one index is logged and
    skipped; the others still return. If Fyers rejects the token, re-authenticate
    once and retry (self-heals the daily token expiry). Never raises — a re-auth
    failure aborts the cycle gracefully with whatever was fetched so far.
    """
    fetched_at = datetime.now(timezone.utc)
    chains: Dict[str, ChainSnapshot] = {}
    reauthed = False
    for inst in all_instruments().values():
        for _ in range(2):  # second pass only after a re-auth
            try:
                raw = fetch_raw_chain(client, inst.symbol, strikecount=strikecount)
                chains[inst.name] = parse_chain(raw, inst.name, fetched_at)
                break
            except TokenRejectedError:
                if reauthed:
                    logger.error("token still rejected for %s after re-auth", inst.name)
                    break
                try:
                    client = _relogin()
                except Exception as exc:
                    logger.error("re-authentication failed — aborting fetch cycle: %s", exc)
                    return chains  # graceful: return whatever we have (maybe empty)
                reauthed = True
                continue  # retry this index with the fresh client
            except Exception as exc:
                logger.error("fetch failed for %s (%s): %s", inst.name, inst.symbol, exc)
                break
    return chains


def run_fetch(strikecount: int = 10) -> dict:
    """Fetch + parse + store all active indices; return a per-index summary."""
    try:
        client = get_fyers_client()  # cached token → refresh → full TOTP login
    except Exception as exc:
        logger.error("auth failed — skipping fetch cycle: %s", exc)
        return {"error": f"auth: {exc}"}

    chains = fetch_chains(client, strikecount)
    fetched_at = next(iter(chains.values())).fetched_at if chains else None
    summary: dict = {"fetched_at": fetched_at.isoformat() if fetched_at else None}

    for name, chain in chains.items():
        try:
            n = store_snapshot(chain)
            summary[name] = {
                "rows": n,
                "strikes": len({s.strike for s in chain.strikes}),
                "expiry": str(chain.expiry),
                "vix": chain.vix,
                "call_oi": chain.call_oi,
                "put_oi": chain.put_oi,
            }
        except Exception as exc:
            logger.error("store failed for %s: %s", name, exc)
            summary[name] = {"error": str(exc), "rows": 0}

    return summary


def main() -> None:
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    strikecount = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    try:
        print(json.dumps(run_fetch(strikecount), indent=2, default=str))
    finally:
        close_pool()


if __name__ == "__main__":
    main()
