"""Typed models for a parsed Fyers option chain (Phase 1)."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import Field

from .base import ApiModel

IndexName = Literal["NIFTY", "SENSEX"]
OptionType = Literal["CE", "PE"]
# v3: a wall is a CAP (highest CE OI) or a FLOOR (highest PE OI) of the ladder.
Side = Literal["CAP", "FLOOR"]


class StrikeOI(ApiModel):
    """One CE/PE strike from the chain — maps 1:1 to a `snapshots` row."""

    index_name: IndexName
    option_type: OptionType
    strike: int
    expiry: date
    oi: int
    ltp: Optional[float] = None
    volume: Optional[int] = None
    prev_oi: Optional[int] = None   # Fyers prev-day OI (reference only)
    oichp: Optional[float] = None   # Fyers OI %chg vs prev day (reference only)


class ExpiryInfo(ApiModel):
    """One entry from the chain's `expiryData` list.

    `epoch` is the Fyers expiry timestamp — feed it back as the `timestamp`
    param to fetch that specific expiry's chain in a later call.
    """

    date: date
    epoch: str
    flag: Optional[str] = None      # "W" weekly | "M" monthly


class ChainSnapshot(ApiModel):
    """A whole-chain pull for one index at one instant."""

    index_name: IndexName
    fetched_at: datetime
    expiry: date                    # nearest expiry of the returned chain
    expiry_epoch: Optional[str] = None   # epoch of `expiry` (for re-querying)
    available_expiries: List[ExpiryInfo] = Field(default_factory=list)
    spot: Optional[float] = None    # v3: live index spot (anchors the ladder, §3)
    vix: Optional[float] = None
    call_oi: Optional[int] = None
    put_oi: Optional[int] = None
    strikes: List[StrikeOI] = Field(default_factory=list)


# --- Phase 2: expiry / DTE awareness (spec §3) -----------------------------

class IndexExpiry(ApiModel):
    """An index's nearest expiry and how many calendar days away it is."""

    index_name: IndexName
    expiry: date
    dte: int                        # calendar days to expiry (0 = expiry day)
    is_expiry_day: bool             # dte == 0
    near_expiry: bool               # dte == 1


class ExpiryAssessment(ApiModel):
    """Expiry handling derived from both indices' DTE (v3 §4 — runs all 5 days).

    v3 drops 0-DTE suppression. Instead the tool runs every day and a 0-DTE index
    is tagged EXPIRY/PIN: its walls pin, so building/stable OI = HOLDING and an OI
    *unwind* is settlement (read as PIN/HOLD, never a breakout). The only NIFTY-ONLY
    case left is a *data* failure (Sensex missing), not expiry.

    - sensex_missing: Sensex fetch/token failure → NIFTY-ONLY row.
    - nifty_pin / sensex_pin: that index is 0-DTE → EXPIRY/PIN guard applies.

    Near-expiry (1-DTE) gets no special "low weight" downgrade — each index's DTE and
    expiry DATE are simply reported (see IndexExpiry.near_expiry / .dte).
    """

    nifty: IndexExpiry
    sensex: Optional[IndexExpiry] = None
    sensex_missing: bool
    nifty_pin: bool
    sensex_pin: bool
    label: str


# --- Phase 3: instrument registry (the `instruments` table) ----------------

class Instrument(ApiModel):
    """One index underlying + its fixed trading params (the `instruments` row)."""

    name: IndexName
    symbol: str                     # Fyers underlying, e.g. 'NSE:NIFTY50-INDEX'
    strike_interval: int            # 50 | 100
    lot_size: int                   # 75 | 20
    expiry_weekday: str             # 'TUE' | 'THU'
    price_mult: float               # × Nifty level (Sensex ≈ 3.20) — v3: sanity log only
    is_active: bool = True


# --- v3: spot-anchored ladder + per-index metrics (§3, §6) -----------------

class Ladder(ApiModel):
    """The locked spot-anchored strike ladder for one index (v3 §3).

    Built once per session from live spot: ATM + 3 rungs up + 4 down (8 strikes),
    both CE & PE tracked on every rung. Locked so each strike has a clean series;
    if live spot later leaves [min(strikes), max(strikes)] → RANGE BROKEN (§3).
    """

    index_name: IndexName
    expiry: date
    spot_at_lock: float
    atm: int
    interval: int
    strikes: List[int]              # 8 rungs, descending (ATM+3i … ATM-4i)


class IndexMetrics(ApiModel):
    """Per-index, per-tick chain metrics (v3 §6) — max-pain + PCR, no extra API."""

    index_name: IndexName
    expiry: date
    # Tick fetch time (when this index's data was actually pulled). Not written by
    # index_metrics_from_chain — the tick passes the shared fetched_at to insert;
    # only read_latest_metrics populates it (from the stored row) so /state can
    # report a real "last updated" instead of the request clock.
    ts: Optional[datetime] = None
    spot: Optional[float] = None
    atm: Optional[int] = None
    max_pain: Optional[int] = None  # argmin writer payout over the full chain
    pcr: Optional[float] = None     # putOi / callOi (chain totals)
    call_oi: Optional[int] = None
    put_oi: Optional[int] = None
    vix: Optional[float] = None     # India VIX at this tick (same for both indices, §5.3)


# --- Phase 4: read models for the verdict engine ---------------------------

class SnapshotPoint(ApiModel):
    """One (ts, oi) point of a single strike's OI time-series (from `snapshots`).

    Returned ascending by time so the brain can compute the windowed Δ% and the
    snapshot-to-snapshot read streak (spec §5.1).
    """

    ts: datetime
    oi: int


class MonitoredStrike(ApiModel):
    """A locked wall + its monitored neighbors for one index/side (v3 §3 row).

    The verdict engine reads these to know which strikes to evaluate and which
    one is the primary WALL — the CAP (highest CE OI) or FLOOR (highest PE OI)
    of the locked ladder; neighbors are context (§3/§4).
    """

    trading_date: date
    side: Side                      # CAP (CE) | FLOOR (PE)
    index_name: IndexName
    option_type: OptionType
    expiry: date
    wall_strike: int
    monitored: List[int]            # [wall-interval, wall, wall+interval]
    wall_oi_at_lock: Optional[int] = None
    broken_level: Optional[int] = None   # v4: former wall spot cleared (sticky badge)
