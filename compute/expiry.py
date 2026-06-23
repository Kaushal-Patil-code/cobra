"""v3 — expiry / DTE awareness (§4). Runs ALL 5 days; no suppression.

Nifty (Tue) and Sensex (Thu) never share a days-to-expiry. v3 drops the old
0-DTE cross-check suppression: the tool runs every day and a 0-DTE index is
tagged EXPIRY/PIN instead. The verdict engine (compute.verdict) reads the pin
flags to apply the one guard — an OI *unwind* on a 0-DTE index is settlement,
read as PIN/HOLD, never a breakout. The only NIFTY-ONLY case left is a Sensex
*data* failure (sensex_missing), not expiry.

DTE is **calendar** days from the chain's nearest expiry to "today" (IST date),
which reproduces the spec's weekday table (e.g. Wed→next-Tue = ~6).
"""
from __future__ import annotations

from datetime import date

from schemas.market import ExpiryAssessment, IndexExpiry

# Banner labels (kept here so they're easy to tune / reference in tests).
LABEL_NIFTY_ONLY = "NIFTY-ONLY — Sensex unavailable"
LABEL_LOW_WEIGHT = "near-expiry, low weight"
LABEL_ACTIVE = "cross-check active"


def pin_label(nifty_pin: bool, sensex_pin: bool) -> str:
    """EXPIRY/PIN banner naming the 0-DTE index/indices (v3 §4)."""
    pinned = [n for n, on in (("Nifty", nifty_pin), ("Sensex", sensex_pin)) if on]
    return f"EXPIRY/PIN — {' & '.join(pinned)} 0-DTE"


def days_to_expiry(expiry: date, today: date) -> int:
    """Calendar days from `today` to `expiry` (0 on expiry day)."""
    return (expiry - today).days


def index_expiry(index_name: str, expiry: date, today: date) -> IndexExpiry:
    dte = days_to_expiry(expiry, today)
    return IndexExpiry(
        index_name=index_name,
        expiry=expiry,
        dte=dte,
        is_expiry_day=(dte == 0),
        near_expiry=(dte == 1),
    )


def assess(nifty: IndexExpiry, sensex: IndexExpiry | None) -> ExpiryAssessment:
    """Decide expiry handling from both indices' DTE (v3 §4 — no suppression)."""
    sensex_missing = sensex is None
    nifty_pin = nifty.is_expiry_day
    sensex_pin = sensex is not None and sensex.is_expiry_day
    low_weight = nifty.near_expiry or (sensex is not None and sensex.near_expiry)

    # Label priority: data-missing > pin > near-expiry > active.
    if sensex_missing:
        label = LABEL_NIFTY_ONLY
    elif nifty_pin or sensex_pin:
        label = pin_label(nifty_pin, sensex_pin)
    elif low_weight:
        label = LABEL_LOW_WEIGHT
    else:
        label = LABEL_ACTIVE

    return ExpiryAssessment(
        nifty=nifty,
        sensex=sensex,
        sensex_missing=sensex_missing,
        nifty_pin=nifty_pin,
        sensex_pin=sensex_pin,
        low_weight=low_weight,
        label=label,
    )


def assess_from_dates(
    nifty_expiry: date, sensex_expiry: date | None, today: date
) -> ExpiryAssessment:
    """Convenience: build both IndexExpiry objects from dates, then assess."""
    nifty = index_expiry("NIFTY", nifty_expiry, today)
    sensex = index_expiry("SENSEX", sensex_expiry, today) if sensex_expiry else None
    return assess(nifty, sensex)
