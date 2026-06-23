"""Parse a raw Fyers optionchain response into a typed ChainSnapshot.

Response shape (verified live 18 Jun 2026):
  data.optionsChain[0]      → the underlying (strike_price -1, option_type ""):
                              carries the live index SPOT in `ltp`/`fp` (v3 §3)
  data.optionsChain[1:]     → CE/PE rows: strike_price, option_type, oi, ltp,
                              volume, prev_oi, oichp, oich
  data.expiryData[0].date   → nearest expiry, "DD-MM-YYYY"
  data.indiavixData.ltp     → India VIX
  data.callOi / data.putOi  → chain totals
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from schemas.market import ChainSnapshot, ExpiryInfo, StrikeOI

logger = logging.getLogger(__name__)

STRIKE_INTERVAL = {"NIFTY": 50, "SENSEX": 100}
IST = ZoneInfo("Asia/Kolkata")  # expiry epochs resolve to IST trading dates


def _expiry_date(entry: dict) -> date:
    """Date of an expiryData entry.

    Prefer the `expiry` epoch (format-proof); fall back to the `date` string,
    accepting both 'DD-MM-YYYY' and 'DD-Mon-YYYY' since the format varies by API
    version.
    """
    epoch = entry.get("expiry")
    if epoch:
        return datetime.fromtimestamp(int(epoch), tz=IST).date()
    ds = entry.get("date", "")
    for fmt in ("%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(ds, fmt).date()
        except (ValueError, TypeError):
            continue
    raise ValueError(f"unparseable expiry entry: {entry!r}")


def _parse_expiries(expiry_data: list) -> list[ExpiryInfo]:
    if not expiry_data:
        raise ValueError("expiryData missing/empty in optionchain response")
    return [
        ExpiryInfo(
            date=_expiry_date(e),
            epoch=str(e.get("expiry") or ""),
            flag=e.get("expiry_flag"),
        )
        for e in expiry_data
    ]


def _as_int(v):
    return int(v) if v is not None else None


def parse_chain(raw: dict, index_name: str, fetched_at: datetime) -> ChainSnapshot:
    data = raw.get("data") or {}
    available = _parse_expiries(data.get("expiryData") or [])
    nearest = available[0]                 # expiryData is ascending → [0] is nearest
    expiry = nearest.date
    # indiavixData is always NSE India VIX — Fyers returns it for the Sensex
    # chain too; there is no Sensex-specific vol index here.
    vix = (data.get("indiavixData") or {}).get("ltp")
    interval = STRIKE_INTERVAL[index_name]
    logger.debug("%s nearest expiry %s (epoch %s), vix %s", index_name, expiry, nearest.epoch, vix)

    strikes: list[StrikeOI] = []
    spot: float | None = None
    for row in data.get("optionsChain") or []:
        ot = row.get("option_type")
        if ot not in ("CE", "PE"):
            # underlying row (strike_price -1): live index spot anchors the ladder.
            if spot is None:
                spot = row.get("ltp") or row.get("fp")
            continue  # not a strike row
        strike = int(row["strike_price"])
        if strike % interval != 0:
            logger.warning(
                "%s strike %s not a multiple of %s — keeping but flagged",
                index_name, strike, interval,
            )
        strikes.append(
            StrikeOI(
                index_name=index_name,
                option_type=ot,
                strike=strike,
                expiry=expiry,
                oi=int(row.get("oi") or 0),
                ltp=row.get("ltp"),
                volume=_as_int(row.get("volume")),
                prev_oi=_as_int(row.get("prev_oi")),
                oichp=row.get("oichp"),
            )
        )

    return ChainSnapshot(
        index_name=index_name,
        fetched_at=fetched_at,
        expiry=expiry,
        expiry_epoch=nearest.epoch or None,
        available_expiries=available,
        spot=spot,
        vix=vix,
        call_oi=_as_int(data.get("callOi")),
        put_oi=_as_int(data.get("putOi")),
        strikes=strikes,
    )
