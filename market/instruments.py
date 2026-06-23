"""Instrument registry — loaded from the `instruments` table and cached.

Static config: read once and reuse (don't query per fetch). This is the single
source of truth for index symbol, strike interval, lot size, expiry weekday, and
the Sensex price multiplier — replacing the old hardcoded dicts.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from db.db import get_conn
from schemas.market import Instrument

logger = logging.getLogger(__name__)

_cache: Optional[Dict[str, Instrument]] = None

_SELECT = """
SELECT name, symbol, strike_interval, lot_size, expiry_weekday, price_mult, is_active
FROM instruments
WHERE is_active
ORDER BY name
"""


def _load() -> Dict[str, Instrument]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(_SELECT)
        rows = cur.fetchall()
    insts = {r["name"]: Instrument(**r) for r in rows}
    logger.info("loaded %d instruments: %s", len(insts), ", ".join(insts) or "(none)")
    return insts


def all_instruments(refresh: bool = False) -> Dict[str, Instrument]:
    """Active instruments by name (cached). Pass refresh=True to reload."""
    global _cache
    if _cache is None or refresh:
        _cache = _load()
    return _cache


def get_instrument(name: str) -> Instrument:
    insts = all_instruments()
    if name not in insts:
        raise KeyError(f"instrument {name!r} not in registry (run db/schema.sql seed)")
    return insts[name]
