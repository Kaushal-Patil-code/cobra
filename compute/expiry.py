"""v3 — expiry / DTE awareness (§4). Runs ALL 5 days; no suppression.

Nifty (Tue) and Sensex (Thu) never share a days-to-expiry. v3 drops the old
0-DTE cross-check suppression: the tool runs every day and a 0-DTE index is
tagged EXPIRY/PIN instead. The verdict engine (compute.verdict) reads the pin
flags to apply the one guard — an OI *unwind* on a 0-DTE index is settlement,
read as PIN/HOLD, never a breakout. The only NIFTY-ONLY case left is a Sensex
*data* failure (sensex_missing), not expiry.

DTE is **calendar** days from the chain's nearest expiry to "today" (IST date),
which reproduces the spec's weekday table (e.g. Wed→next-Tue = ~6).

READ-NEAR / TRADE-NEXT (§1, per Hi) — an invariant, do not "optimise" it away:
  - Walls, max-pain and the OI/verdict are ALWAYS read off the NEAREST expiry chain.
    That is where the pin and the real OI mass sit, especially on expiry day; the
    next-week chain's OI is thin and would not show the true wall. NEVER switch
    wall-reading to next-week.
  - The trader FADES with the NEXT week's expiry, so 0-DTE theta / IV-crush never
    applies to what is actually bought. Therefore near/at expiry = a MATURED wall =
    FULL trust: HIGH is allowed on BOTH 1-DTE and 0-DTE HOLDING — there is NO
    MODERATE conviction cap (see compute.verdict.side_verdict; the old cap guarded a
    risk this workflow does not take).
  - The one guard that STAYS: a 0-DTE OI *unwind* on the nearest chain is settlement,
    not a breakout (the OI drains as it settles — and it isn't even the chain being
    traded), so it reads as PIN/HOLD. See compute.verdict._effective_state.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from schemas.market import ExpiryAssessment, IndexExpiry

# Banner labels (kept here so they're easy to tune / reference in tests).
LABEL_NIFTY_ONLY = "NIFTY-ONLY — Sensex unavailable"
LABEL_ACTIVE = "cross-check active"


def pin_label(nifty_pin: bool, sensex_pin: bool) -> str:
    """EXPIRY/PIN banner naming the 0-DTE index/indices (v3 §4)."""
    pinned = [n for n, on in (("Nifty", nifty_pin), ("Sensex", sensex_pin)) if on]
    return f"EXPIRY/PIN — {' & '.join(pinned)} 0-DTE"


def expiry_pin_note(max_pain: Optional[int]) -> Optional[str]:
    """§5.6: the max-pain pin-target note, shown only on a 0-DTE (expiry) day.

    Max-pain dominates on expiry (R33), so naming the pin target makes it actionable
    on Tue/Thu. None when the pinning index has no max-pain (empty/degraded chain).
    """
    if max_pain is None:
        return None
    return f"Pin target {max_pain} (max-pain) — price likely drawn here today."


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

    # Label priority: data-missing > pin > active. Near-expiry (1-DTE) no longer gets
    # a "low weight" downgrade — the banner simply reports each index's expiry DATE.
    if sensex_missing:
        label = LABEL_NIFTY_ONLY
    elif nifty_pin or sensex_pin:
        label = pin_label(nifty_pin, sensex_pin)
    else:
        label = LABEL_ACTIVE

    return ExpiryAssessment(
        nifty=nifty,
        sensex=sensex,
        sensex_missing=sensex_missing,
        nifty_pin=nifty_pin,
        sensex_pin=sensex_pin,
        label=label,
    )


def assess_from_dates(
    nifty_expiry: date, sensex_expiry: date | None, today: date
) -> ExpiryAssessment:
    """Convenience: build both IndexExpiry objects from dates, then assess."""
    nifty = index_expiry("NIFTY", nifty_expiry, today)
    sensex = index_expiry("SENSEX", sensex_expiry, today) if sensex_expiry else None
    return assess(nifty, sensex)
