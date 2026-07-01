# Dynamic wall detection — wide-scan + hysteresis + sticky BROKEN badge

**Date:** 2026-07-01
**Status:** Approved (design) — pending implementation plan
**Area:** COBRA backend (`compute/strikes.py`, `market/selection.py`, `compute/engine.py`) + `frontend-next`

## Problem

Two production-manager corrections to how CAP/FLOOR walls are chosen and shown:

1. **Wall detection is limited to the 8 displayed ladder rungs.** `select_wall` uses
   `ladder_strikes` as its candidate set, so a dominant wall that sits beyond the
   visible ±3/±4 rungs is invisible to detection and to the verdict.
2. **The hysteresis needs to be stated as "hold-unless-beaten," not "re-pick each
   tick."** The switch threshold should be **≥5% more OI** (currently 15%), and when
   **spot crosses the wall** the box must **flag it broken and re-pick**.

Separately, a 29-Jun screenshot showed a stale wall (24200) while the biggest CE OI
at/above spot was 24100 — root-caused to an *old lock-once deploy*, not a logic bug.
The current per-tick re-pick logic is correct; this spec tunes and extends it.

## Goals

- Detect walls from a **wider scan of the full chain**, decoupled from the 8-rung display.
- Keep the **8-rung ladder display** unchanged; surface an off-ladder wall as a
  **highlighted callout row** and compute the verdict on it.
- Tune hysteresis to **5%** and phrase the rule as hold-unless-beaten.
- When spot crosses a wall: **re-pick immediately** and show a **sticky BROKEN badge**
  that persists until spot pulls back past the broken level.

## Non-goals

- No change to the verdict math, Δ%/streak scoring, or the level-pairing (Aligned/
  Divergent) logic.
- No change to max-pain/PCR/expiry.
- Strength/dominance stays measured against the **8 visible rungs** (not the scan
  window) — a deliberate simplification; revisit only if the PM asks.

## Decisions (resolved)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Broken-flag behavior | **Re-pick immediately + sticky BROKEN badge** until spot pulls back |
| D2 | Scan reach scaling | **Scaled per index**: NIFTY 400 pts; SENSEX `400 × live_ratio` (≈1280), fallback `price_mult` 3.20 |
| D3 | Fetch width | **Bump tick `strikecount` to ~15** so SENSEX's ~1280-pt window isn't truncated |
| D4 | Callout pairing | **Single paired callout row** (off-ladder NIFTY wall ↔ off-ladder SENSEX wall, with Aligned/Divergent) |
| D5 | Broken level tracked | Keep the **first** level that broke until spot pulls back past it (no flicker on multi-strike runs) |

## Design

### A. Wide-scan detection (`compute/strikes.py`)

`select_wall` candidates change from the 8 ladder rungs to **full-chain strikes of the
side's option type within a spot window**:

- CAP: `spot <= strike <= spot + reach` (CE)
- FLOOR: `spot - reach <= strike <= spot` (PE)

Signature change: drop `ladder_strikes`, add `scan_reach: float` (that index's own
points). Tie-break shifts from "toward ladder centre" to **toward spot** (nearest
actionable wall wins ties). Stickiness (D-below) and the spot-side bound are unchanged
in spirit — an incumbent that spot has crossed now falls **outside the window**, so it
is ineligible and forces a re-pick.

```
def select_wall(strikes, side, index_name, expiry, interval, spot, scan_reach,
                incumbent=None, sticky_margin=0.0):
    lo, hi = (spot, spot + scan_reach) if side == "CAP" else (spot - scan_reach, spot)
    oi = {s.strike: s.oi for s in strikes
          if s.option_type == SIDE_OPTION[side] and lo <= s.strike <= hi}
    if not oi: return None
    challenger = max(oi, key=lambda k: (oi[k], -abs(k - spot)))   # tie: nearest spot
    wall = challenger
    if (incumbent in oi and incumbent != challenger
            and oi[challenger] < oi[incumbent] * (1 + sticky_margin)):
        wall = incumbent
    return WallSelection(..., wall_strike=wall, wall_oi=oi[wall],
                         monitored=[wall-interval, wall, wall+interval], interval=interval)
```

`select_index_walls(chain, scan_reach, incumbents, sticky_margin)` — drops the `ladder`
param (ladders are display-only now); computes nothing about ladders.

### B. Per-index reach (`market/selection.py`)

`lock_walls` still builds + UPSERTs the 8-rung ladder for **display** (`plan_ladders` →
`upsert_ladders`), but computes the **scan reach** per index for detection:

- `nifty_reach = WALL_SCAN_REACH_POINTS` (400)
- `ratio = sensex_spot / nifty_spot` if both live, else `instruments['SENSEX'].price_mult`
- `sensex_reach = WALL_SCAN_REACH_POINTS * ratio`

The full fetched chain (`chain.strikes`) is the candidate pool. Requires the fetch to be
wide enough → **D3**.

### C. Fetch width (`market/tick.py`)

Raise the tick fetch `strikecount` (currently 10) to **15** so `[spot ± reach]` is fully
covered for both indices (SENSEX ~1280 pts ≈ 13 strikes). ~50% more snapshot rows/tick —
acceptable. Detection remains write-path; `build_state` reads the persisted wall.

### D. Hysteresis (`config/thresholds.py`)

`WALL_STICKY_MARGIN`: **0.15 → 0.05**. Rule: *hold the current wall; switch only if a
challenger has ≥5% more OI* (`challenger_oi >= incumbent_oi × 1.05`), *or if spot crosses
the wall* (incumbent leaves the window → re-pick). Update the doc-comment to the
hold-unless-beaten phrasing.

### E. Sticky BROKEN badge

**Column:** `monitored_strikes.broken_level integer` (nullable). NULL = wall intact; a
value = "spot cleared this former wall and hasn't pulled back."

**Pure helper** `compute_broken_level(side, prev_wall, prev_broken, spot) -> Optional[int]`
(in `compute/strikes.py`):

```
# CAP (resistance; broken = spot above). FLOOR mirrors (spot below / clear when spot >= broken).
if prev_broken is None:
    return prev_wall if (prev_wall is not None and spot > prev_wall) else None   # new break
return None if spot <= prev_broken else prev_broken                              # clear on pullback
```

Keeps the **first** broken level (D5); a fast run that clears several strikes still points
at the resistance that flipped, and the badge doesn't flicker.

**Write path** (`lock_walls`): read incumbents as `{key: (wall, broken_level)}`; re-pick
wall (A/B); compute `broken_level` via the helper; pass it to `_upsert_wall` as a separate
argument. `broken_level` is **not** a field on `WallSelection` — it is orthogonal to the
wall pick and only exists at persist time. UPSERT `WHERE` gains
`OR monitored_strikes.broken_level IS DISTINCT FROM EXCLUDED.broken_level` so a pull-back
that only clears the badge still persists when the wall strike itself didn't move.

### F. Off-ladder wall → callout row (`compute/engine.py`, schemas)

- `WallSignal` gains `wall_off_ladder: bool` and `broken_level: Optional[int]`.
- `_index_wall`: set `wall_off_ladder = ms.wall_strike not in ladder.strikes`. Fix
  dominance to take `wall_oi = ws.wall.oi_latest` (not the ladder dict) so an off-ladder
  wall **still gets a strength 1–5** vs the 8 visible rungs.
- `SideVerdict` gains `wall_callout: Optional[PairedRung]`. `build_state` builds it when
  **either** index's wall is off its ladder — pairs the off-ladder NIFTY/SENSEX wall legs
  via the existing agree/level_gap logic (a leg is None when that index's wall is on the
  visible ladder). Frontend renders it as a highlighted row pinned to the paired table.

### G. Schema / read wiring

- `schemas/market.py::MonitoredStrike` + `compute/series.py::read_monitored_strikes`:
  add `broken_level`.
- `market/selection.py::read_incumbent_walls`: return `broken_level` alongside `wall_strike`.

### H. Migration & safety

`db/migrations/v4_wall_dynamics.sql`:

```sql
begin;
alter table monitored_strikes add column if not exists broken_level integer;
commit;
```

Mirror the column into `db/schema.sql`. `lock_walls` graceful-degrades on
`psycopg.errors.UndefinedColumn` (read + upsert) so a forgotten migration **never
refreezes walls** — walls still re-pick and wide-scan; only the BROKEN badge is dormant
until the column exists.

### I. Frontend (`frontend-next`)

- `SideCard.jsx` header: when `nifty.broken_level` is set, render `⚠ BROKEN — cleared
  <level>` and flip the distance line to `spot <n> pts ABOVE cap (breakout)` (mirror for
  FLOOR: below floor).
- `PairedWalls.jsx`: render `side.wall_callout` as a highlighted row (distinct styling,
  e.g. a left accent + "off-ladder wall" label) pinned above/below the 8-rung table, with
  the same Aligned/Divergent chip.

## Testing

- `select_wall` wide-scan: winner beyond `spot+reach` excluded; off-ladder winner inside
  window chosen; tie broken toward spot; 5% hold/switch boundary (challenger ×1.04 holds,
  ×1.06 switches); incumbent crossed by spot → forced re-pick.
- Per-index reach in `lock_walls`: SENSEX reach = 400 × ratio; fallback to `price_mult`
  when a spot is missing.
- `compute_broken_level`: CAP cross-up sets level; sticky through further clears; clears
  on pull-back; FLOOR mirror; no-incumbent → None.
- `_index_wall`: `wall_off_ladder` flag; off-ladder wall still yields a strength.
- `build_state`: `wall_callout` present iff a wall is off-ladder; legs + agree correct.
- `lock_walls`/`_upsert_wall`: persists `broken_level`; degrades on `UndefinedColumn`.

## Deployment ordering

1. Apply `db/migrations/v4_wall_dynamics.sql` on Supabase **before** deploying the code
   (graceful-degrade covers a slip, but the badge stays dormant until applied).
2. Deploy backend; confirm tick cron runs and header ATM == ladder centre (re-center
   working). Off-ladder walls appear as callout rows; BROKEN badge fires on a cross.
3. Deploy `frontend-next`.

## Files touched

- `config/thresholds.py` — `WALL_STICKY_MARGIN = 0.05`, new `WALL_SCAN_REACH_POINTS = 400`.
- `compute/strikes.py` — `select_wall` wide-scan rework; `compute_broken_level`; `select_index_walls` signature.
- `schemas/strikes.py` — no change (`broken_level` is not a `WallSelection` field; computed at persist time).
- `market/selection.py` — reach per index; incumbents carry `broken_level`; `compute_broken_level` call; `_upsert_wall` gains a `broken_level` arg; UPSERT + WHERE; graceful degrade.
- `market/tick.py` — fetch `strikecount` → 15.
- `market/ladders.py` — unchanged (display ladder still upserted).
- `schemas/market.py` — `MonitoredStrike.broken_level`.
- `compute/series.py` — `read_monitored_strikes` selects `broken_level`.
- `schemas/verdict.py` — `WallSignal.wall_off_ladder`, `WallSignal.broken_level`, `SideVerdict.wall_callout`.
- `compute/engine.py` — `_index_wall` flags + dominance fix; `build_state` callout.
- `db/schema.sql` + `db/migrations/v4_wall_dynamics.sql` — `broken_level` column.
- `frontend-next/app/components/SideCard.jsx`, `PairedWalls.jsx` — badge + callout row.
- Tests across `tests/test_strikes.py`, `test_selection.py`, `test_dbwrite.py`, `test_verdict.py`.
