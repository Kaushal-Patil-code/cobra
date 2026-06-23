# COBRA — Data Logic (Short)

> Core data pipeline only — fetch → snapshot → DTE → strikes → verdict → persist. Auth (Phase 0) skipped.

## Phase 1 — Fetcher + snapshots
1. One `optionchain()` call per index (`NIFTY50-INDEX`, `SENSEX-INDEX`) → 2 calls/cycle; empty timestamp = nearest expiry, response still lists all expiries.
2. Parse keeps only CE/PE rows; per strike stores `oi`, `ltp`, `volume`, `prev_oi`, `oichp`; chain-level `call_oi`/`put_oi` and India VIX kept separately.
3. Fyers' own `oich`/`oichp` are reference-only — the system computes its own intraday Δ later, never relying on them.
4. Strike intervals fixed: Nifty 50, Sensex 100; off-interval strikes warned but kept.
5. All rows of one cycle share a single UTC `ts`; append-inserted into `snapshots(ts, index, type, strike, expiry, oi, ltp, volume, prev_oi, oichp)`.
6. Per-index resilient: one index failing (or a dead token → one re-login retry) never aborts the other; partial data is never stored.

## Phase 2 — Expiry / DTE
7. `DTE = (nearest_expiry - today_IST).days` (calendar days; 0 on expiry day).
8. `is_expiry_day = DTE==0`, `near_expiry = DTE==1`.
9. Cross-check **suppressed** if Nifty 0-DTE OR Sensex 0-DTE OR Sensex missing → forces NIFTY-ONLY.
10. If not suppressed but either index is 1-DTE → `low_weight` flag ("near-expiry, low weight").
11. Label priority: Sensex-missing → suppressed → low-weight → active.

## Phase 3 — Strike auto-selection
12. Two Nifty bands: higher = RESISTANCE→CE, lower = SUPPORT→PE; single level `L`→`[L-25, L+25]`.
13. Candidates = interval-multiples in `[low-interval, high+interval]`; Sensex band first scaled ×`price_mult` (≈3.20).
14. WALL = highest-OI candidate of the zone's type; ties break toward the strike nearest band centre.
15. MONITORED = `{wall-interval, wall, wall+interval}` → 3 strikes × 2 indices × 2 zones = 12 strikes.
16. Walls **locked at session start** per `(role, index, expiry)`; idempotent re-runs; expiry roll = fresh lock on the new chain.
17. Migration is **flagged, never re-picked**: if a neighbour's OI strictly exceeds the wall's, emit "OI peak shifting up/down".

## Phase 4 — Verdict engine
18. Per strike Δ% = `(latest_oi - baseline_oi)/baseline_oi*100`, baseline = last snapshot at-or-before `now-window` (strict; window 15/30 min); no baseline or baseline 0 → insufficient.
19. Magnitude: <5% noise · 5–10% mild · ≥10% signal · ≥20% strong; direction flat if `|Δ%|<5%`, else up=building / down=unwinding.
20. Read = one snapshot move ≥3%; trend = streak ≥3 same-direction reads; a flat latest pair resets streak to 0.
21. Per zone, compare Nifty vs Sensex WALL — suppression applied FIRST (suppressed/missing Sensex → NIFTY-ONLY row):
22. building+building → CAP/FLOOR HOLDING (HIGH if both ≥signal or either trend, else MODERATE).
23. unwinding+unwinding → BREAKOUT/BREAKDOWN, **confirmed only if both streaks ≥2** else UNCONFIRMED (single unwind = fake).
24. opposite dirs → DIVERGENCE/LOW; one moving one quiet → PARTIAL/LOW; both quiet → NO SIGNAL.
25. Output = `/state`: zones with verdict, conviction, both signals, `dte_n`/`dte_s`, suppressed flag, expiry label. All thresholds in `config/thresholds.py`.

## Phase 5 — Persist (backtest)
26. Every tick writes one `verdicts` row **per zone** — including quiet & suppressed rows — so distorted days bucket out later.
27. `run_tick` = one cycle: fetch → store snapshots → lock walls *from the same chains* → build verdict → persist (never raises).
28. Stores the whole chain (not just monitored) for migration/re-lock history; backs `/state` and `/history` (records + per-weekday/verdict buckets).
29. De-dup guard: skip if a snapshot already exists this clock-minute; market-hours re-check 09:15–15:30 IST, Mon–Fri.
