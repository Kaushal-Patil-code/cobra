"""Phase 4 — verdict-engine output models (spec §5).

These types ARE the `/state` contract the React dashboard renders and the shape
the Phase-5 `verdicts` log is built from. Keep them stable; the frontend keys
off these field names.

Shape (v3):
    VerdictState
      ├─ expiry:  ExpiryAssessment       (v3 §4 — EXPIRY/PIN, no suppression)
      ├─ metrics: list[IndexMetrics]     (v3 §6 — max-pain + PCR per index)
      └─ sides: list[SideVerdict]        (one per CAP / FLOOR)
           ├─ nifty:  WallSignal         (the primary index)
           └─ sensex: WallSignal | None  (None only when Sensex data is missing)
                └─ wall + neighbors: StrikeSignal
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional

from .base import ApiModel
from .market import ExpiryAssessment, IndexMetrics, IndexName, OptionType, Side

Direction = Literal["up", "down", "flat"]
Magnitude = Literal["noise", "mild", "signal", "strong"]
# Index-level read of a wall: building OI / unwinding OI / quiet / no history.
IndexState = Literal["building", "unwinding", "flat", "nodata"]
Conviction = Literal["HIGH", "MODERATE", "LOW", "UNCONFIRMED", "NONE"]


class StrikeSignal(ApiModel):
    """Per-strike signal over the window (spec §5.1)."""

    index_name: IndexName
    option_type: OptionType
    strike: int
    expiry: date
    is_wall: bool

    oi_latest: Optional[int] = None
    oi_baseline: Optional[int] = None       # OI at-or-before (now - window)
    change_pct: Optional[float] = None       # windowed Δ% (None = insufficient)
    direction: Direction = "flat"
    magnitude: Magnitude = "noise"
    reads: int = 0                           # # of ≥3% snapshot-to-snapshot moves
    streak: int = 0                          # trailing run of same-direction reads
    trend: bool = False                      # streak ≥ TREND_STREAK
    insufficient: bool = False               # not enough history to judge


class WallSignal(ApiModel):
    """One index's wall (primary) + neighbor context for a side (v3 §3/§5)."""

    index_name: IndexName
    state: IndexState                        # derived from the WALL strike
    wall: StrikeSignal
    neighbors: List[StrikeSignal] = []
    # Compact human/log string, e.g. "building/signal/streak3".
    summary: str = ""
    # Neighbor-overtakes-wall flag (v3 §3 migration); detail only, never re-picks.
    migration: Optional[str] = None
    pin: bool = False                        # this index is 0-DTE → EXPIRY/PIN (§4)
    # Wall SIZE (not change): dominance = wall OI ÷ median of the other ladder
    # strikes; strength 1 (thin) – 5 (dominant). A wall can be "building but tiny"
    # (skip) or "flat but huge" (very fade-able) — this is that axis.
    dominance: Optional[float] = None
    strength: Optional[int] = None
    # All 8 spot-anchored ladder rungs of this side's type, scored over the window
    # (descending by strike, wall flagged). Backs the level-paired table (v3 item 1);
    # `wall` + `neighbors` remain the subset the verdict math reads.
    ladder: List[StrikeSignal] = []


class PairedRung(ApiModel):
    """One row of the level-paired ladder table (v3 item 1).

    A NIFTY rung beside the SENSEX rung closest to its level-equivalent
    (nifty_strike × the LIVE ratio = sensex_spot / nifty_spot). One row per Nifty
    rung; `sensex` is None when no Sensex rung sits within tolerance of that level or
    Sensex data is missing. Because the Nifty strike grid is coarser than Sensex's in
    level-terms (≈160 vs 100 at ratio ~3.20), some Sensex rungs are nearest to no
    Nifty rung — each is added as a Sensex-only row (`nifty` None) so both full
    ladders stay visible. `agree` is the at-a-glance cross-check.
    """

    nifty: Optional[StrikeSignal] = None
    sensex: Optional[StrikeSignal] = None
    # both legs moving the same way → ALIGNED; opposite → DIVERGENT; either flat or a
    # leg absent → None.
    agree: Optional[Literal["ALIGNED", "DIVERGENT"]] = None
    is_wall: bool = False                    # either leg is its index's wall
    level_gap: Optional[int] = None          # |sensex_strike − nifty_strike×ratio|, rounded


class SideVerdict(ApiModel):
    """Dual-index verdict for one side — CAP or FLOOR (v3 §5.2)."""

    side: Side
    option_type: OptionType                  # CE for CAP, PE for FLOOR
    wall_strike: Optional[int] = None        # the locked NIFTY wall (primary index)
    verdict: str                             # e.g. "CAP HOLDING", "BREAKOUT", "DIVERGENCE"
    conviction: Conviction
    action: str = "WAIT"                     # the one-line verb: FADE OK / DON'T FADE / WAIT
    meaning: str                             # trader-facing line from the §5.2 table
    tag: Optional[str] = None                # "EXPIRY/PIN" when an index is 0-DTE (§4)

    nifty_sig: str                           # compact state string (for the log)
    sensex_sig: Optional[str] = None
    dte_n: int
    dte_s: Optional[int] = None
    suppressed: bool = False                 # NIFTY-ONLY (Sensex data missing, §4)
    expiry_label: str = ""

    nifty: WallSignal
    sensex: Optional[WallSignal] = None
    # Level-paired ladder rows for this side (Nifty rung ↔ closest Sensex rung by
    # level), built from the two `WallSignal.ladder`s. Display only (v3 item 1).
    paired: List[PairedRung] = []


class VerdictState(ApiModel):
    """The whole dashboard state for one instant — the `/state` payload."""

    ts: datetime
    trading_date: date
    weekday: str                             # 'Mon'..'Fri' (DTE bucketing, v3 §4)
    window_minutes: int
    # Live Sensex/Nifty spot ratio used for level-pairing (~3.20); None if a spot
    # is missing. Surfaced so the dashboard shows the exact ratio used (v3 item 1).
    live_ratio: Optional[float] = None
    expiry: Optional[ExpiryAssessment] = None  # None until walls are locked
    metrics: List[IndexMetrics] = []         # max-pain + PCR per index (v3 §6)
    range_broken: List[IndexName] = []       # indices whose spot left the ladder (§3)
    sides: List[SideVerdict] = []
    note: Optional[str] = None               # e.g. "no walls locked yet"


# --- Phase 5: persisted backtest dataset (the `verdicts` table) ------------

class VerdictRecord(ApiModel):
    """One persisted verdict row — mirrors the `verdicts` table (spec §1, §6.2).

    Built from a SideVerdict + its VerdictState envelope on each tick; read back
    for the weekday/DTE-bucketed paper-test (spec §3, §11). `outcome`/`notes` are
    optional manual backtest-labeling columns (nullable; populated later, not by
    the running service).
    """

    ts: datetime
    trading_date: date
    weekday: str
    window_minutes: int
    side: Side
    option_type: OptionType
    wall_strike: Optional[int] = None
    verdict: str
    conviction: Conviction
    meaning: Optional[str] = None
    tag: Optional[str] = None
    nifty_sig: Optional[str] = None
    sensex_sig: Optional[str] = None
    dte_n: Optional[int] = None
    dte_s: Optional[int] = None
    suppressed: bool = False
    expiry_label: Optional[str] = None
    outcome: Optional[str] = None            # manual label, e.g. 'correct'|'wrong'
    notes: Optional[str] = None


class BucketStat(ApiModel):
    """Counts for one weekday bucket (spec §3/§11 per-weekday judging)."""

    bucket: str                              # 'Mon'..'Fri'
    count: int
    by_verdict: dict = {}                    # {verdict_label: count}


class VerdictHistory(ApiModel):
    """The `/history` payload — logged verdicts + weekday buckets for review."""

    start: Optional[date] = None
    end: Optional[date] = None
    records: List[VerdictRecord] = []
    weekday_buckets: List[BucketStat] = []
