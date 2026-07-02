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

# --- v3 item 3 / v4: dynamic wall detection (wide-scan + hysteresis) --------
# Detection scans the FULL chain, not just the 8 displayed rungs: CAP = max CE OI
# in [spot, spot+reach], FLOOR = max PE OI in [spot-reach, spot]. `reach` is in the
# index's own points, scaled by the live Sensex/Nifty ratio so both indices reach
# the same relative level (NIFTY 400 ≈ SENSEX 1280). Tune on logged data.
WALL_SCAN_REACH_POINTS = 400

# Hysteresis (hold-unless-beaten): keep the incumbent wall; switch only if a
# challenger's OI beats it by this margin, OR spot crosses the wall (→ re-pick).
# 5% kills flicker without lagging a real migration.
WALL_STICKY_MARGIN = 0.05

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

# --- Proximity bands (spec §5.4) -------------------------------------------
# A fade only exists NEAR the wall — |dist| (points from spot to the wall) buckets
# into AT WALL / APPROACHING / FAR, so the dashboard says when to actually watch.
# Per-index, in each index's OWN points (Sensex ≈ ×3.2 the Nifty bands, same
# relative level). {index: (at_max, approaching_max)}; |dist| ≤ at_max → AT WALL,
# ≤ approaching_max → APPROACHING, else FAR. Tunable after live days.
PROX_BANDS = {
    "NIFTY": (25.0, 60.0),
    "SENSEX": (75.0, 190.0),
}

# --- VIX regime (spec §5.3) ------------------------------------------------
# Stops fading into a trend day at one glance. Level bands on India VIX (already in
# the Fyers response), plus an intraday-jump override. Tunable — ties to R11
# (VIX > 22 = no-trade) and R22 (IV crush):
#   vix < 14           → calm, fade-friendly
#   14 ≤ vix ≤ 20      → normal
#   vix > 20  OR  intraday jump > 5% off the session-open VIX → spiking, don't fade
VIX_CALM_MAX = 14.0
VIX_NORMAL_MAX = 20.0
VIX_INTRADAY_JUMP_PCT = 5.0

# Ordered magnitude labels, weakest → strongest (for comparisons).
MAGNITUDE_ORDER = ("noise", "mild", "signal", "strong")


def magnitude_at_least(mag: str, floor: str) -> bool:
    """True if magnitude label `mag` is >= `floor` in MAGNITUDE_ORDER."""
    return MAGNITUDE_ORDER.index(mag) >= MAGNITUDE_ORDER.index(floor)
