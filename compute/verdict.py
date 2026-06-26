"""v3 — the brain (§5). PURE functions, zero I/O, zero LLM.

Per-strike Δ%/magnitude/streak math, then a dual-index verdict per SIDE (CAP for
the call wall, FLOOR for the put wall). v3 changes vs the old port:

  - role (RESISTANCE/SUPPORT) → side (CAP/FLOOR).
  - No expiry suppression (§4): NIFTY-ONLY only when Sensex *data* is missing.
  - EXPIRY/PIN guard (§4): an OI *unwind* on a 0-DTE index is settlement, read as
    PIN/HOLD — it must never drive a BREAKOUT/BREAKDOWN. Conviction is capped at
    MODERATE whenever a pinning index is involved (never size up on settlement).

Pipeline per fetch:
  series (per strike)
    → window_change      Δ% over the 15/30-min window           (§5.1)
    → reads / streak     ≥3% snapshot-to-snapshot moves; trend  (§5.1)
    → StrikeSignal       direction / magnitude / streak / trend
    → WallSignal         the WALL (primary) + neighbor context   (§3)
    → side_verdict       dual-index table + pin guard            (§5.2/§4)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from config.thresholds import (
    FLAT_PCT,
    MAG_MILD_PCT,
    MAG_SIGNAL_PCT,
    MAG_STRONG_PCT,
    MIN_CONFIRM_STREAK,
    READ_THRESHOLD_PCT,
    TREND_STREAK,
    magnitude_at_least,
)
from schemas.market import ExpiryAssessment, MonitoredStrike, SnapshotPoint
from schemas.verdict import IndexState, SideVerdict, StrikeSignal, WallSignal

# ---------------------------------------------------------------------------
# Per-strike primitives (§5.1)
# ---------------------------------------------------------------------------

def window_change(
    series: Sequence[SnapshotPoint], now: datetime, window_minutes: int
) -> Tuple[Optional[float], Optional[int], Optional[int]]:
    """Δ% over the window: (latest − value_at_or_before(now−window)) / baseline × 100.

    Returns (change_pct, baseline_oi, latest_oi). change_pct is None when there's
    no snapshot at-or-before the cutoff (series doesn't span a full window yet) or
    the baseline OI is 0 — i.e. we can't honestly measure the windowed move. We
    are strict about *value_at_or_before* (now − window) rather than approximating
    with a shorter window, so an early-session strike reads as "insufficient".
    """
    if not series:
        return None, None, None
    latest = series[-1].oi
    cutoff = now - timedelta(minutes=window_minutes)
    baseline: Optional[int] = None
    for p in series:                      # ascending; take the last point <= cutoff
        if p.ts <= cutoff:
            baseline = p.oi
        else:
            break
    if baseline is None or baseline == 0:
        return None, baseline, latest
    return (latest - baseline) / baseline * 100.0, baseline, latest


def classify_magnitude(change_pct: Optional[float]) -> str:
    """|Δ%| → noise (<5) · mild (5–10) · signal (10–20) · strong (≥20) (§5.1)."""
    if change_pct is None:
        return "noise"
    a = abs(change_pct)
    if a >= MAG_STRONG_PCT:
        return "strong"
    if a >= MAG_SIGNAL_PCT:
        return "signal"
    if a >= MAG_MILD_PCT:
        return "mild"
    return "noise"


def direction_of(change_pct: Optional[float]) -> str:
    """up = OI building · down = unwinding · flat = noise / insufficient.

    flat below FLAT_PCT (= mild threshold): a move under 5% is noise, not a
    direction (so "noise magnitude" ⟺ "flat direction", consistently).
    """
    if change_pct is None or abs(change_pct) < FLAT_PCT:
        return "flat"
    return "up" if change_pct > 0 else "down"


def compute_reads_streak(series: Sequence[SnapshotPoint]) -> Tuple[int, int, str]:
    """Count directional reads and the trailing same-direction streak (§5.1).

    A *read* is one snapshot-to-snapshot move ≥ 3%. The *streak* is the run of
    consecutive same-direction reads ending at the most recent pair; a sub-3%
    (non-read) pair or a direction flip breaks it, and a flat latest pair → 0.
    Returns (reads_total, streak_len, streak_direction).
    """
    moves: List[str] = []                 # "up" / "down" / "flat" per adjacent pair
    for a, b in zip(series, series[1:]):
        if a.oi == 0:
            moves.append("flat")
            continue
        pct = (b.oi - a.oi) / a.oi * 100.0
        if abs(pct) >= READ_THRESHOLD_PCT:
            moves.append("up" if pct > 0 else "down")
        else:
            moves.append("flat")

    reads_total = sum(1 for m in moves if m != "flat")
    if not moves or moves[-1] == "flat":
        return reads_total, 0, "flat"
    direction = moves[-1]
    streak = 0
    for m in reversed(moves):
        if m == direction:
            streak += 1
        else:
            break
    return reads_total, streak, direction


def strike_signal(
    index_name: str,
    option_type: str,
    strike: int,
    expiry,
    series: Sequence[SnapshotPoint],
    now: datetime,
    window_minutes: int,
    is_wall: bool,
) -> StrikeSignal:
    """Build the full per-strike signal from its OI series (§5.1)."""
    change_pct, baseline, latest = window_change(series, now, window_minutes)
    reads, streak, _ = compute_reads_streak(series)
    return StrikeSignal(
        index_name=index_name,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        is_wall=is_wall,
        oi_latest=latest,
        oi_baseline=baseline,
        change_pct=None if change_pct is None else round(change_pct, 2),
        direction=direction_of(change_pct),
        magnitude=classify_magnitude(change_pct),
        reads=reads,
        streak=streak,
        trend=streak >= TREND_STREAK,
        insufficient=change_pct is None,
    )


# ---------------------------------------------------------------------------
# Index-level (the WALL drives the verdict; neighbors are context — §3)
# ---------------------------------------------------------------------------

def index_state(sig: StrikeSignal) -> IndexState:
    """Map the WALL's windowed read to building / unwinding / flat / nodata."""
    if sig.insufficient:
        return "nodata"
    if sig.direction == "flat":
        return "flat"
    return "building" if sig.direction == "up" else "unwinding"


def build_wall_signal(
    ms: MonitoredStrike, signals_by_strike: Dict[int, StrikeSignal]
) -> WallSignal:
    """Assemble an index's WallSignal: the locked wall + its neighbors.

    Flags neighbor migration (§3) when a neighbor's *latest* OI now exceeds the
    wall's — context only, the wall is never silently re-picked.
    """
    wall = signals_by_strike[ms.wall_strike]
    neighbors = [
        signals_by_strike[s]
        for s in ms.monitored
        if s != ms.wall_strike and s in signals_by_strike
    ]
    state = index_state(wall)
    summary = f"{state}/{wall.magnitude}/streak{wall.streak}"

    migration: Optional[str] = None
    wall_oi = wall.oi_latest or 0
    overtaker = max(
        (n for n in neighbors if (n.oi_latest or 0) > wall_oi),
        key=lambda n: n.oi_latest or 0,
        default=None,
    )
    if overtaker is not None:
        d = "up" if overtaker.strike > ms.wall_strike else "down"
        migration = f"OI peak shifting {d}: {ms.wall_strike} → {overtaker.strike}"

    return WallSignal(
        index_name=ms.index_name,
        state=state,
        wall=wall,
        neighbors=neighbors,
        summary=summary,
        migration=migration,
    )


# ---------------------------------------------------------------------------
# Dual-index verdict per side (§5.2) + EXPIRY/PIN guard (§4)
# ---------------------------------------------------------------------------

def _labels(side: str) -> Tuple[str, str]:
    """(hold_label, break_label) for the side."""
    if side == "CAP":
        return "CAP HOLDING", "BREAKOUT"
    return "FLOOR HOLDING", "BREAKDOWN"


def _moving(state: IndexState) -> bool:
    return state in ("building", "unwinding")


def _effective_state(state: IndexState, is_pin: bool) -> IndexState:
    """v3 §4 PIN guard: a 0-DTE OI unwind is settlement → read as HOLD (building),
    never a breakout. Building/flat on a pinning index are left untouched."""
    if is_pin and state == "unwinding":
        return "building"
    return state


# Minimum wall strength to actually fade a HOLDING wall (size axis, not change).
MIN_FADE_STRENGTH = 3


def _action(verdict: str, conviction: str, strength: Optional[int], state: IndexState) -> str:
    """The one-line decision verb (the trader wants the verb mid-trade, not the
    diagnosis). FADE OK / DON'T FADE / WAIT.

      confirmed BREAKOUT/DOWN  → DON'T FADE  (respect the break — go with the move)
      DIVERGENCE / PARTIAL / unconfirmed → WAIT (no cross-confirmation)
      HOLDING (building) + strength ≥3   → FADE OK
      quiet but the wall is STABLE (flat) + strength ≥3 → FADE OK
          (a "flat but huge" wall is very fade-able; a "building but tiny" one isn't)
      otherwise → WAIT (thin wall, or no read yet)
    """
    v = verdict.upper()
    strong = (strength or 0) >= MIN_FADE_STRENGTH
    if "BREAKOUT" in v or "BREAKDOWN" in v:
        return "DON'T FADE" if conviction != "UNCONFIRMED" else "WAIT"
    if "DIVERGENCE" in v or v.startswith("PARTIAL"):
        return "WAIT"
    if "HOLDING" in v:
        return "FADE OK" if strong else "WAIT"
    if "NO SIGNAL" in v:                      # quiet: fade only a stable + dominant wall
        return "FADE OK" if (strong and state == "flat") else "WAIT"
    return "WAIT"


def side_verdict(
    side: str,
    nifty: WallSignal,
    sensex: Optional[WallSignal],
    assessment: ExpiryAssessment,
) -> SideVerdict:
    """The §5.2 table for one side, with the §4 EXPIRY/PIN guard applied.

    NIFTY-ONLY only when Sensex DATA is missing (not expiry). A 0-DTE index can't
    produce a breakout (its unwind reads as HOLD), and any pin caps conviction at
    MODERATE so the trader never sizes up on settlement.
    """
    hold, brk = _labels(side)
    option_type = "CE" if side == "CAP" else "PE"
    dte_n = assessment.nifty.dte
    dte_s = assessment.sensex.dte if assessment.sensex else None
    nifty_only = assessment.sensex_missing or sensex is None
    pin_involved = assessment.nifty_pin or assessment.sensex_pin
    tag = "EXPIRY/PIN" if pin_involved else None

    # Surface the pin state on the wall signals (display only).
    nifty.pin = assessment.nifty_pin
    if sensex is not None:
        sensex.pin = assessment.sensex_pin

    n = _effective_state(nifty.state, assessment.nifty_pin)

    if nifty_only:
        # NIFTY-ONLY (Sensex data missing). The pin guard still neutralises a
        # 0-DTE Nifty unwind into HOLD above, so no false breakdown here either.
        if n == "building":
            verdict, conv = f"{hold} (NIFTY-ONLY)", "MODERATE"
            meaning = "Sensex unavailable — Nifty wall building; fade with caution, no cross-confirm."
        elif n == "unwinding":
            if nifty.wall.streak >= MIN_CONFIRM_STREAK:
                verdict, conv = f"{brk} (NIFTY-ONLY)", "LOW"
                meaning = "Sensex unavailable — Nifty wall unwinding; possible break, no cross-confirm."
            else:
                verdict, conv = f"{brk} (NIFTY-ONLY, UNCONFIRMED)", "UNCONFIRMED"
                meaning = "Single Nifty read, no Sensex — wait to sustain (one unwind is a fake)."
        elif n == "flat":
            verdict, conv, meaning = "NO SIGNAL (NIFTY-ONLY)", "NONE", "Quiet; Sensex unavailable."
        else:  # nodata
            verdict, conv, meaning = "NO SIGNAL (NIFTY-ONLY)", "NONE", "Insufficient history yet; Sensex unavailable."
        if pin_involved and conv == "HIGH":
            conv = "MODERATE"
        action = _action(verdict, conv, nifty.strength, n)
        if action == "WAIT" and ("HOLDING" in verdict.upper() or "NO SIGNAL" in verdict.upper()):
            if (nifty.strength or 0) < MIN_FADE_STRENGTH:
                meaning += " (thin wall — strength < 3; hold off.)"
        return SideVerdict(
            side=side, option_type=option_type, wall_strike=nifty.wall.strike,
            verdict=verdict, conviction=conv, action=action, meaning=meaning, tag=tag,
            nifty_sig=nifty.summary, sensex_sig=None,
            dte_n=dte_n, dte_s=dte_s, suppressed=True, expiry_label=assessment.label,
            nifty=nifty, sensex=None,
        )

    s = _effective_state(sensex.state, assessment.sensex_pin)
    if _moving(n) and _moving(s):
        if n == "building" and s == "building":
            both_signal = magnitude_at_least(nifty.wall.magnitude, "signal") and \
                magnitude_at_least(sensex.wall.magnitude, "signal")
            either_trend = nifty.wall.trend or sensex.wall.trend
            conv = "HIGH" if (both_signal or either_trend) else "MODERATE"
            verdict = hold
            meaning = ("Fade OK. HIGH → hold full target / size up." if conv == "HIGH"
                       else "Fade OK (moderate) — hold, normal size.")
        elif n == "unwinding" and s == "unwinding":
            confirmed = (nifty.wall.streak >= MIN_CONFIRM_STREAK
                         and sensex.wall.streak >= MIN_CONFIRM_STREAK)
            verdict = brk
            if confirmed:
                conv = "HIGH" if (nifty.wall.trend or sensex.wall.trend) else "MODERATE"
                meaning = "Don't fade; go with the move — confirmed on both indices."
            else:
                conv = "UNCONFIRMED"
                meaning = "Don't fade yet — single read; wait to sustain (one unwind is a fake)."
        else:  # opposite directions
            verdict, conv = "DIVERGENCE", "LOW"
            meaning = "Fake-out risk. Stand down / tiny."
    elif _moving(n) != _moving(s):       # exactly one index moving
        verdict, conv = "PARTIAL — one index only", "LOW"
        meaning = "No cross-confirmation. Caution."
    else:                                # both quiet (flat / nodata)
        verdict, conv, meaning = "NO SIGNAL", "NONE", "Quiet."

    # PIN guard: never size up on a settlement day — cap conviction at MODERATE.
    if pin_involved and conv == "HIGH":
        conv = "MODERATE"
        meaning += " (pin day — capped, don't size up on settlement.)"

    action = _action(verdict, conv, nifty.strength, n)
    if action == "WAIT" and ("HOLDING" in verdict.upper() or "NO SIGNAL" in verdict.upper()):
        if (nifty.strength or 0) < MIN_FADE_STRENGTH:
            meaning += " (thin wall — strength < 3; hold off.)"

    return SideVerdict(
        side=side, option_type=option_type, wall_strike=nifty.wall.strike,
        verdict=verdict, conviction=conv, action=action, meaning=meaning, tag=tag,
        nifty_sig=nifty.summary, sensex_sig=sensex.summary,
        dte_n=dte_n, dte_s=dte_s, suppressed=False, expiry_label=assessment.label,
        nifty=nifty, sensex=sensex,
    )
