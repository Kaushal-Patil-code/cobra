"""Phase 4 — the ONE tunable config block for the verdict engine (spec §5).

Every threshold the brain uses lives here so Hi can tune the rule without
touching logic (spec §5.2: "Thresholds + DTE rules live in one config block").
The DTE suppression rules themselves live in `compute/expiry.py` (Phase 2); this
block holds the per-strike Δ%/magnitude/streak knobs plus the window options.

Nothing here is read at module-import time by hot paths — `compute/verdict.py`
imports these names directly, so editing a value + restarting is all it takes.
"""
from __future__ import annotations

# --- Window over which Δ% is measured (spec §5.1) --------------------------
# "Window = 15 or 30 min (toggleable)." 15 is the default; the frontend toggle
# and /state?window= pass one of WINDOW_CHOICES.
DEFAULT_WINDOW_MINUTES = 15
WINDOW_CHOICES = (15, 30)

# How far back to pull each strike's OI series. Must cover the longest window
# AND enough prior snapshots to measure a streak of consecutive reads. At a
# 3-min cadence, 120 min ≈ 40 snapshots — ample for a 30-min window + streaks.
SERIES_LOOKBACK_MINUTES = 120

# --- Per-snapshot "read" + streak (spec §5.1) ------------------------------
# "one snapshot-to-snapshot move ≥ 3% = a directional read"
READ_THRESHOLD_PCT = 3.0
# "Trend = 3+ consecutive same-direction reads"
TREND_STREAK = 3
# Critical filter (spec §5.1/§5.2): "a single one-snapshot unwind is a FAKE …
# needs streak ≥ 2 AND cross-index confirmation." Below this a directional
# move is UNCONFIRMED.
MIN_CONFIRM_STREAK = 2

# --- v3 item 3: dynamic, spot-anchored wall selection (re-picked each tick) -
# The ladder re-centers on live spot every tick and the CAP/FLOOR walls are
# re-picked from current OI (cap = max CE OI at/above spot, floor = max PE OI
# at/below spot). To avoid flip-flopping on tiny ties, the incumbent wall is held
# unless a challenger strike's OI beats it by this margin (hysteresis). Ratio-based
# so it self-adjusts to OI size; tune on logged data (like the strength cutoffs).
WALL_STICKY_MARGIN = 0.15

# --- Magnitude buckets on |Δ% over window| (spec §5.1) ---------------------
#   <5%  noise · 5–10% mild · ≥10% signal · ≥20% strong
MAG_MILD_PCT = 5.0
MAG_SIGNAL_PCT = 10.0
MAG_STRONG_PCT = 20.0

# Direction is "flat" when the windowed move is mere noise (< mild). A flat
# wall is treated as quiet in the dual-index table (NO SIGNAL / PARTIAL rows).
# This is the spec-derived reading of the table's "flat" state (the JSX
# reference that would have pinned it exactly is not in the repo; spec wins).
FLAT_PCT = MAG_MILD_PCT

# Ordered magnitude labels, weakest → strongest (for comparisons).
MAGNITUDE_ORDER = ("noise", "mild", "signal", "strong")


def magnitude_at_least(mag: str, floor: str) -> bool:
    """True if magnitude label `mag` is >= `floor` in MAGNITUDE_ORDER."""
    return MAGNITUDE_ORDER.index(mag) >= MAGNITUDE_ORDER.index(floor)
