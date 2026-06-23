# COBRA v3 Refactor — Granular Execution Plan

Source: `data_md/COBRA_OI_Dashboard_Engineer_Brief.md` (v3 FINAL).
Decisions: **full stack in one pass**; **additive ALTER migration** (preserve `snapshots`/`verdicts` rows; rename via ALTER+backfill, never drop).
Process: after **every** code change → (a) functional review (run the relevant pytest + targeted manual run), (b) manual code review of the diff.

Core model shift: typed two-zone bands → **spot-anchored 8-strike ladder per index**; `role` (RESISTANCE/SUPPORT) → `side` (CAP/FLOOR); expiry suppression → **EXPIRY/PIN tag, no suppression**; add **max-pain + PCR**; drop the ×3.20 strike mapping.

---

## T1 — Schemas (foundation)
- 1.1 `schemas/market.py`: add `spot` to `ChainSnapshot`; add `Ladder`, `IndexMetrics` models.
- 1.2 `schemas/market.py`: rework `ExpiryAssessment` → `sensex_missing`, `low_weight`, `nifty_pin`, `sensex_pin`, `label` (drop `suppress_cross_check`).
- 1.3 `schemas/market.py`: `MonitoredStrike.role` → `side` (Literal CAP/FLOOR).
- 1.4 New `schemas/strikes.py`: `WallSelection` (side-based), `MigrationFlag`; delete `schemas/zones.py` (Zone/ZoneBand/SetZonesRequest gone).
- 1.5 `schemas/verdict.py`: `ZoneVerdict`→`SideVerdict` (`side`, add `tag`, `wall_strike`); `VerdictRecord` role→side + tag + wall_strike; `VerdictState` add `metrics`, `range_broken`; `BucketStat`/`VerdictHistory` keep.

## T2 — compute/metrics.py (new)
- 2.1 `compute_atm(spot, interval)`, `build_ladder(spot, interval)` (ATM +3 / −4), `ladder_broken(spot, ladder)`.
- 2.2 `max_pain(strikes)` (argmin writer payout over full chain), `pcr(call_oi, put_oi)`.
- 2.3 `index_metrics_from_chain(chain)` → `IndexMetrics`.

## T3 — compute/strikes.py (rewrite)
- 3.1 Remove `candidate_strikes`/`map_band`/`select_zone_walls` (band logic).
- 3.2 `select_wall(strikes, option_type, ladder, ...)` = highest-OI rung of that type.
- 3.3 `select_index_walls(chain, ladder, interval)` → (CAP via CE, FLOOR via PE).
- 3.4 Keep/adapt `check_migration` (uses sel.interval).
- 3.5 `plan_locks(chains, instruments, already_locked)` — per index: ladder from spot, lock CAP+FLOOR.

## T4 — compute/expiry.py (rewrite)
- 4.1 Drop suppression; `assess` → `sensex_missing` (data→NIFTY-ONLY), `nifty_pin`/`sensex_pin` (0-DTE), `low_weight` (1-DTE).
- 4.2 Labels: NIFTY-ONLY / EXPIRY-PIN / near-expiry / active.

## T5 — compute/verdict.py (rewrite)
- 5.1 `_labels(side)` CAP/FLOOR; `zone_verdict`→`side_verdict`.
- 5.2 Remove expiry suppression; keep NIFTY-ONLY only for `sensex_missing`/sensex None.
- 5.3 **PIN guard**: pin index `unwinding`→`building` (read as HOLD); no breakout off a 0-DTE unwind; cap conviction ≤ MODERATE on pin; set `tag="EXPIRY/PIN"`.
- 5.4 Populate `wall_strike` (nifty primary) on the verdict.

## T6 — market/parse.py
- 6.1 Extract live spot from the underlying row (`strike_price -1`, `fp`/`ltp`) → `ChainSnapshot.spot`.

## T7 — persistence: ladders + metrics (new)
- 7.1 `market/ladders.py`: insert/read locked ladders (spot/atm/strikes).
- 7.2 `market/metrics_store.py`: insert/read `index_metrics` (max-pain/pcr per tick per index).

## T8 — market/selection.py (rewrite)
- 8.1 `lock_walls` → lock ladders + CAP/FLOOR walls per index (no zones); idempotent; expiry-roll aware.
- 8.2 `_warn_unlockable`/`get_locked_keys` keyed on side; `lock_session` adapt.

## T9 — compute/series.py + engine.py
- 9.1 `series.py`: `read_monitored_strikes` role→side; add ladder/metrics reads if needed.
- 9.2 `engine.py`: group by side; build CAP/FLOOR verdicts; attach metrics + range-broken; drop role ordering → side ordering.

## T10 — verdict_store.py + persist.py
- 10.1 `verdict_store.py`: INSERT columns side/tag/wall_strike; reads role→side; bucket by side filter.
- 10.2 `persist.py`: `_row` side/tag/wall_strike; `persist_state` also persists metrics; `build_history` side filter.

## T11 — market/tick.py
- 11.1 Cycle: fetch → store snapshots → compute+store metrics → lock ladders+walls (same chains) → build_state → persist verdicts. Keep never-raises + market-minute de-dup.

## T12 — DB
- 12.1 `db/schema.sql`: final-state schema (side, tag, wall_strike, `ladders`, `index_metrics`; mark `zones` deprecated).
- 12.2 `db/migrations/v3_ladder.sql`: ALTER+backfill monitored_strikes/verdicts (role→side, RESISTANCE→CAP/SUPPORT→FLOOR), add columns, create new tables — idempotent, data-preserving.

## T13 — app/api/dashboard.py
- 13.1 Remove `/zones` + `/set-zones` (no manual input); drop zones imports.
- 13.2 `/history` role→side filter (CAP/FLOOR/ALL); `/state`,`/tick` adapt.

## T14 — Tests
- 14.1 Rewrite: test_strikes, test_selection, test_verdict, test_expiry, test_persist, test_api, test_parse_chain.
- 14.2 Delete test_zones; add test_metrics, test_ladders.
- 14.3 Full suite green.

## T15 — frontend-next
- 15.1 `lib/api.js`: drop set-zones; add metrics/ladder fields.
- 15.2 Remove `SetZonesForm`; add `LadderView` + `MetricsPanel` (max-pain/PCR); `DualIndexTable` role→side + tag; `ExpiryBanner` pin; `page.js` wiring.
- 15.3 `sampleState.js`/`sampleHistory.js` updated to the new shape.

## T16 — Final
- 16.1 Full pytest green; `next build` (or lint) on frontend; summary of changes + the manual DB migration step.
