"""v3 — the brain (§5). PURE functions, zero I/O, zero LLM.

Per-strike Δ%/magnitude/streak math, then a dual-index verdict per SIDE (CAP for
the call wall, FLOOR for the put wall). v3 changes vs the old port:

  - role (RESISTANCE/SUPPORT) → side (CAP/FLOOR).
  - No expiry suppression (§4): NIFTY-ONLY only when Sensex *data* is missing.
  - EXPIRY/PIN guard (§4): an OI *unwind* on a 0-DTE index is settlement, read as
    PIN/HOLD — it must never drive a BREAKOUT/BREAKDOWN.
  - Near/at-expiry trust (§1): near (1-DTE) or at (0-DTE) expiry, the wall has
    MATURED, so a HOLDING read reaches HIGH conviction MORE easily (the pin makes it
    a stronger barrier). Trust is no longer downgraded on expiry; only the sizing
    lever (₹2,000 cap / max-2-trades) bounds risk, and that lives outside this engine.

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
    """v3 §4 PIN guard: a 0-DTE OI unwind on the NEAREST (read) chain is settlement —
    its OI drains as it settles, and it isn't even the chain being traded (read-near /
    trade-next, see compute.expiry) — so read it as HOLD (building), never a breakout.
    Building/flat on a pinning index are left untouched."""
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
    # §2: a fade only exists for a wall that is BUILDING or STABLE. NEVER fade a wall
    # that is actually unwinding — a str-5 unwind is a real break, not a fade. Today
    # this is already structural (HOLDING ⟹ building, NO SIGNAL ⟹ flat), but the guard
    # documents the invariant so a future change can't accidentally fade a break.
    fadeable = strong and state != "unwinding"
    if "BREAKOUT" in v or "BREAKDOWN" in v:
        return "DON'T FADE" if conviction != "UNCONFIRMED" else "WAIT"
    if "DIVERGENCE" in v or v.startswith("PARTIAL"):
        return "WAIT"
    if "HOLDING" in v:
        return "FADE OK" if fadeable else "WAIT"
    if "NO SIGNAL" in v:                      # quiet: fade only a stable + dominant wall
        return "FADE OK" if (fadeable and state == "flat") else "WAIT"
    return "WAIT"


def side_verdict(
    side: str,
    nifty: WallSignal,
    sensex: Optional[WallSignal],
    assessment: ExpiryAssessment,
) -> SideVerdict:
    """The §5.2 table for one side, with the §4 EXPIRY/PIN guard applied.

    NIFTY-ONLY only when Sensex DATA is missing (not expiry). A 0-DTE index can't
    produce a breakout (its unwind reads as HOLD). Per §1, a matured (near/at-expiry)
    wall is trusted MORE — a HOLDING read reaches HIGH conviction more easily and is
    never downgraded; sizing stays bounded by the ₹2,000 cap / max-2-trades, a lever
    outside this engine (so there is no MODERATE conviction cap on expiry any more).
    """
    hold, brk = _labels(side)
    option_type = "CE" if side == "CAP" else "PE"
    dte_n = assessment.nifty.dte
    dte_s = assessment.sensex.dte if assessment.sensex else None
    nifty_only = assessment.sensex_missing or sensex is None
    pin_involved = assessment.nifty_pin or assessment.sensex_pin
    near_expiry_involved = assessment.nifty.near_expiry or (
        assessment.sensex is not None and assessment.sensex.near_expiry
    )
    # §1 LOGIC CHANGE (near-expiry now = MORE trust, not less): near (1-DTE) OR at
    # (0-DTE) expiry means the wall has MATURED — positions are committed and the
    # strike pins price, so a heavy wall is a STRONGER barrier and a FADE deserves
    # HIGHER conviction. This lowers the bar to HIGH for a HOLDING wall; it does NOT
    # size the trade up (the ₹2,000 cap / max-2-trades handle sizing — §1 risk note),
    # and it does NOT relax the 0-DTE guard below (an unwind is still settlement).
    mature = pin_involved or near_expiry_involved
    # Canonical tag marker; the frontend maps it to plain wording. A 0-DTE pin takes
    # priority over a 1-DTE near-expiry.
    tag = "EXPIRY/PIN" if pin_involved else ("NEAR-EXPIRY" if near_expiry_involved else None)

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
        # NIFTY-ONLY never reaches HIGH (no cross-confirm), and §1 does not lift that:
        # a matured wall can't substitute for the missing Sensex check. Conviction stays.
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
            # §1: a matured (near/at-expiry) wall reaches HIGH on its own — the pin
            # makes the barrier stronger, so a HOLDING read is trusted more, not less.
            conv = "HIGH" if (both_signal or either_trend or mature) else "MODERATE"
            verdict = hold
            if conv == "HIGH":
                meaning = ("Fade OK — wall matured near expiry; strong hold (size by the "
                           "risk cap, not conviction)." if mature
                           else "Fade OK. HIGH — hold full target.")
            else:
                meaning = "Fade OK (moderate) — hold, normal size."
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

    # §1: no conviction cap on expiry any more. A 0-DTE unwind is already neutralised
    # to HOLD by _effective_state above (settlement never becomes a breakout), and a
    # matured wall is trusted MORE, not less. Position sizing stays bounded by the
    # ₹2,000 cap / max-2-trades (risk note) — that lever is outside the verdict engine.

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


# ---------------------------------------------------------------------------
# The ACTION line (§5.1) — one plain instruction = one decision (the #1 add)
# ---------------------------------------------------------------------------

def compose_action_line(
    sv: SideVerdict,
    vix_regime: Optional[str] = None,
    expiry_max_pain: Optional[int] = None,
) -> str:
    """Compose the single plain-English instruction from state + prox + side (§5.1).

    Reads the already-computed SideVerdict (verdict / conviction / prox / dist / side
    / suppressed) plus two state-level overlays: `vix_regime` (append a trend-risk
    note when spiking) and `expiry_max_pain` (append the pin target on a 0-DTE day —
    pass None off expiry). It ends by pointing to the trader's own trigger ("wait for
    your 5-min candle"), never a bare "just buy".
    """
    side_word = "Top" if sv.side == "CAP" else "Bottom"      # CAP = the top wall
    fade_word = "buy PE" if sv.side == "CAP" else "buy CE"    # fade the cap with PE, the floor with CE
    v = sv.verdict.upper()
    dist = abs(sv.dist_pts) if sv.dist_pts is not None else None
    dist_txt = f"{dist}pts" if dist is not None else "some way"

    if "BREAKOUT" in v or "BREAKDOWN" in v:
        updown = "up" if "BREAKOUT" in v else "down"
        base = f"{side_word} breaking {updown} — DON'T FADE, stand aside."
        if sv.conviction == "UNCONFIRMED":
            base += " (1 read only — wait to confirm.)"
    elif "HOLDING" in v:
        # HOLDING ⟹ both indices building (aligned) → "both agree"; NIFTY-ONLY → "Nifty alone".
        agree = "Nifty alone" if sv.suppressed else "both agree"
        # Only invite a fade when the wall is actually fade-able (thick enough AND close
        # enough — the engine has already resolved the verb). A held-but-unfade-able wall
        # gets a plain "just watch", never a "FADE-NOW" the verb would contradict.
        if sv.action != "FADE OK":
            if sv.prox == "FAR":
                base = f"{side_word} holding but price {dist_txt} away → too far, just watch."
            elif sv.prox in ("AT", "APPROACHING"):
                base = f"{side_word} holding, {agree}, but the wall's too thin to fade — just watch."
            else:                                # no spot yet → can't place the fade
                base = f"{side_word} holding, {agree} — waiting on a live price."
        elif sv.prox == "AT":
            base = (f"{side_word} holding, {agree}, price {dist_txt} away → "
                    f"FADE-NOW zone ({fade_word}). Wait for your 5-min candle.")
        elif sv.prox == "APPROACHING":
            base = (f"{side_word} holding, {agree}, price {dist_txt} away → "
                    f"get ready to fade ({fade_word}).")
        else:                                    # fade-able but no prox — shouldn't occur
            base = f"{side_word} holding, {agree}."
    elif "DIVERGENCE" in v:
        base = "Nifty & Sensex disagree — skip or tiny only."
    elif v.startswith("PARTIAL"):
        moving = "Nifty" if sv.nifty.state in ("building", "unwinding") else "Sensex"
        base = f"Only {moving} moving, other quiet — wait for both."
    elif sv.suppressed:                          # NIFTY-ONLY, quiet/unclear
        base = "No Sensex check — Nifty alone, be careful."
    else:                                        # NO SIGNAL
        base = "Both quiet — nothing to do."

    if vix_regime == "spiking":
        base += " · VIX spiking, trend risk."
    if expiry_max_pain is not None:
        base += f" · Expiry pin {expiry_max_pain}."
    return base


def compose_wait_reason(sv: SideVerdict) -> Optional[str]:
    """The specific unmet condition behind a WAIT (§5.2) — never a blank WAIT.

    None unless the action is WAIT. Priority follows the spec's list: an unconfirmed
    break, then a disagreement, a one-sided (partial) read, price too far from the
    wall, and finally a wall too thin to fade. Distance-based reason names the gap.
    """
    if sv.action != "WAIT":
        return None
    v = sv.verdict.upper()
    if sv.conviction == "UNCONFIRMED":
        return "(1 read only)"
    if "DIVERGENCE" in v:
        return "(they disagree)"
    if v.startswith("PARTIAL"):
        # PARTIAL = exactly one index moving; name the quiet (flat) one.
        flat = "Nifty" if sv.nifty.state not in ("building", "unwinding") else "Sensex"
        return f"({flat} quiet)"
    if sv.prox == "FAR":
        dist = abs(sv.dist_pts) if sv.dist_pts is not None else None
        return f"(too far — {dist}pts)" if dist is not None else "(too far)"
    if sv.prox is None:
        return "(waiting on live price)"     # no spot yet — can't place the fade
    if "HOLDING" in v or "NO SIGNAL" in v:
        return "(thin wall — hold off)"      # a fade-able state but strength < 3
    return None
