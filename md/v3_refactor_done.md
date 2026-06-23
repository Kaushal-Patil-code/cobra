# COBRA v3 Refactor — Completion Summary

All 16 tasks (T1–T16) from [v3_refactor_plan.md](v3_refactor_plan.md) done, full-stack in one pass.
**Backend: 85 tests pass** (`~/.venvs/cobra/bin/python -m pytest`). **Frontend: `next build` compiles, 5 pages.**

## The three v3 changes + the new feature

1. **Spot-anchored ladder (replaced typed zones).** No human input. Each index reads its own live spot
   (`market/parse.py` now extracts it from the underlying row's `ltp`/`fp`), builds an 8-rung ladder
   (ATM +3 / −4, `compute/metrics.py:build_ladder`), and locks **CAP** (highest CE OI) + **FLOOR**
   (highest PE OI) per index (`compute/strikes.py`, `market/selection.py`). RANGE-BROKEN fires when spot
   leaves the ladder. The `zones` table / `set_zones` / `/set-zones` / `SetZonesForm` are gone.
2. **EXPIRY/PIN, no suppression (`compute/expiry.py` + `compute/verdict.py`).** Runs all 5 days. A 0-DTE
   index is tagged `EXPIRY/PIN`; the guard converts a 0-DTE **unwind** to HOLD so it can never produce a
   BREAKOUT/BREAKDOWN, and conviction is capped at MODERATE on a pin day. NIFTY-ONLY now means *Sensex data
   missing*, not expiry.
3. **Dropped the ×3.20 strike mapping** — each index anchors on its own spot (`map_band` removed).
4. **NEW: max-pain + PCR** (`compute/metrics.py`, `market/metrics_store.py`, `index_metrics` table),
   computed from the chain already pulled (no extra API), persisted per tick, shown in `MetricsPanel`.

`role` (RESISTANCE/SUPPORT) → `side` (CAP/FLOOR) everywhere: schemas, DB, verdict, persistence, API, UI, tests.

## ⚠️ Manual step before going live — run the DB migration

The DB change is **additive & data-preserving** but must be run by hand (renames columns in your Supabase
DB; not auto-run):

```
psql "$DATABASE_URL" -f db/migrations/v3_ladder.sql
```

It renames `role`→`side` (backfilling RESISTANCE→CAP, SUPPORT→FLOOR), adds `verdicts.wall_strike`/`tag`,
and creates the `ladders` + `index_metrics` tables. Idempotent. `db/schema.sql` is the fresh-install version.

## Notes
- **Test runner:** the repo lives on an exFAT drive so its `.venv` can't hold executables; tests run via a
  native venv at `~/.venvs/cobra` (Python 3.14). Use `~/.venvs/cobra/bin/python -m pytest`.
- **Still pending (unchanged from v2):** confirm OI refreshes intraday (M2); the `depth()` fallback is not
  implemented; the read-threshold (3%) is expected to drop to ~1.5–2% after live 3-min observation (tunable
  in `config/thresholds.py`). Telegram alerts (Phase 7) and deploy (Phase 8) remain.
