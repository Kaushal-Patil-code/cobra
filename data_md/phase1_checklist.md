# Phase 1 — Fetcher + Raw Snapshots · Granular Checklist

**Goal:** during market hours, pull both option chains from Fyers (2 calls), parse per-strike OI
+ nearest expiry + India VIX, and write timestamped rows to `snapshots`. **Primary bug-finding
gate:** prove `oi` actually refreshes *intraday* (not frozen at the previous day's value).

**Spec refs:** §2 (data source), §6.2 (fetcher), §9 Phase 1.
**Depends on:** ✅ Fyers auth (`auth.get_fyers_client`), ✅ `snapshots` table, `FYERS_*` in `.env`,
and **live market hours** for the refresh validation (09:15–15:30 IST).

**STATUS (18 Jun 2026):** pipeline built & verified end-to-end (84 rows persisted from a live
pull). Only the **intraday-refresh proof (task 8)** remains — it needs the market open. Built
files: `schemas/market.py`, `market/{chain,parse,store,fetch}.py`, `scripts/inspect_chain.py`,
`scripts/validate_refresh.py`, `tests/test_parse_chain.py`, fixtures in `tests/fixtures/`.

### Findings — live pull 18 Jun 2026 (~22:00 IST, market closed → EOD-frozen data)

- **Auth works end-to-end:** `get_fyers_client()` reused a DB-cached token (no re-login).
- **Response shape:** `data.{callOi, putOi, expiryData[], indiavixData, optionsChain[]}`;
  `optionsChain[0]` is the underlying (`strike_price -1`, blank `option_type`) → skipped.
- **Per-strike fields:** `strike_price`, `option_type`, `oi`, `oich`, `oichp`, `prev_oi`, `ltp`, `volume`.
- **Nearest expiry = `expiryData[0].date`** ("DD-MM-YYYY"): Nifty **23-06** (Tue), Sensex **18-06** (Thu).
- **VIX** = `data.indiavixData.ltp` (12.67; it's NSE India VIX in both responses).
- **`strikecount=10` → 21 strikes/index** (10 each side + ATM) × 2 = 42 option rows + 1 underlying = 43.
- **Expiry-day distortion confirmed:** Sensex (expiry today) shows ₹0.05 premiums — the §3 reason to suppress.
- ⚠️ `oich`/`oichp` are vs **prev-day** (don't use for intraday Δ% — compute our own across `ts`).

**Proposed module layout (new `market/` package, Analyzer-style):**
- `market/chain.py` — Fyers option-chain client (fetch both symbols)
- `market/parse.py` — response → typed per-strike rows
- `market/store.py` — batch insert into `snapshots`
- `market/fetch.py` — `run_fetch()` orchestration + CLI entry
- `schemas/market.py` — pydantic models for parsed chain/strike
- `scripts/validate_refresh.py` (or a CLI flag) — the intraday-refresh check

---

## 1. DB schema ✅ DONE
- [x] `snapshots` table (`id uuid`, `ts`, `index_name`, `option_type`, `strike`, `expiry`, `oi`, `ltp`, `volume`, `prev_oi`, `oichp`)
- [x] Indexes: `snapshots_series_idx`, `snapshots_ts_idx`
- [x] DDL applied to Supabase

## 2. Inspect the live Fyers `optionchain` response ✅ DONE
- [ ] One-off script: authenticate via `get_fyers_client()`, call `optionchain` for `NSE:NIFTY50-INDEX`
- [ ] Capture the raw JSON shape: locate `data.optionsChain[]`, key names (`strike_price`, `option_type`, `oi`, `oich`, `oichp`, `prev_oi`, `ltp`, `volume`), `expiryData[]`, `indiavixData`, `callOi`/`putOi`
- [ ] Note the quirk: first `optionsChain` row is the underlying (blank `option_type`) — must be skipped
- [ ] Repeat for `BSE:SENSEX-INDEX`; confirm identical structure
- [ ] Save a trimmed sample response as a fixture for offline parser tests (mask nothing sensitive — it's market data)

## 3. Pydantic models (`schemas/market.py`) ✅ DONE
- [ ] `StrikeOI`: `index_name`, `option_type` (CE/PE), `strike:int`, `expiry:date`, `oi:int`, `ltp`, `volume`, `prev_oi`, `oichp`
- [ ] `ChainSnapshot`: `index_name`, `fetched_at`, `expiry`, `vix`, `call_oi`, `put_oi`, `strikes: list[StrikeOI]`
- [ ] Subclass `ApiModel`; validate `option_type ∈ {CE,PE}`, `strike > 0`

## 4. Option-chain client (`market/chain.py`) ✅ DONE
- [ ] `fetch_chain(index_name, strikecount=...)` → calls `client.optionchain(data={"symbol":..., "strikecount":...})`
- [ ] Symbol map: `NIFTY → NSE:NIFTY50-INDEX`, `SENSEX → BSE:SENSEX-INDEX`
- [ ] Reuse `get_fyers_client()`; build the client once per fetch cycle (not per call)
- [ ] Error handling: `s != "ok"` → log + raise a typed error (don't write partial data)
- [ ] Transient-network retry (small, reuse the auth retry style) — distinct from login retry

## 5. Parser (`market/parse.py`) ✅ DONE
- [ ] `parse_chain(raw, index_name, fetched_at) -> ChainSnapshot`
- [ ] Pick the **nearest weekly expiry** from `expiryData` (Nifty→Tue, Sensex→Thu); attach to each row
- [ ] Iterate `optionsChain`, skip the underlying/blank-type row, build `StrikeOI` per CE/PE
- [ ] Coerce types: `strike → int`, validate interval (Nifty %50==0, Sensex %100==0) and log violations
- [ ] Extract `vix`, `call_oi`, `put_oi`

## 6. Persistence (`market/store.py`) ✅ DONE
- [ ] `store_snapshot(chain: ChainSnapshot)` — batch INSERT all strikes with **one shared `ts`** (`fetched_at`)
- [ ] Parameterized multi-row insert via `get_conn()` (`cur.executemany` or a single VALUES batch)
- [ ] Append-only (no upsert); return rows-written count
- [ ] Phase-1 scope: store a modest ATM window (e.g. strikecount ~20). Full 12-strike monitored set is Phase 3.

## 7. Orchestration + entry point (`market/fetch.py`) ✅ DONE
- [ ] `run_fetch()`: fetch NIFTY + SENSEX (2 calls) → parse → store both; one `fetched_at` for the cycle
- [ ] Log a summary per index: rows, expiry, VIX, sample OI
- [ ] CLI: `uv run python -m market.fetch` (manual trigger; the cron `/tick` wiring is Phase 6)
- [ ] Graceful behavior when DB/creds missing (clear error, no crash)

## 8. ⭐ Intraday-refresh validation (the Phase-1 verify gate) ⏳ TOOLING DONE — RUN NEEDS MARKET HOURS
- [ ] `scripts/validate_refresh.py`: run `run_fetch()` twice ~3–5 min apart (market open)
- [ ] For matching `(index, type, strike, expiry)`, compare `oi` across the two `ts` → confirm it **changes**
- [ ] Cross-check: is `oi` ≠ `prev_oi` intraday? is the chain's data live (LTP moving, VIX present)?
- [ ] **Decision recorded:** does Fyers `oi` refresh intraday? (yes → proceed · no/delayed → §depth fallback)
- [ ] Fallback path (only if frozen): Fyers `depth()` per monitored strike (~12) for live OI
- [ ] Note: today is Thu (Sensex expiry) — expect Sensex OI whipsaw; validate mainly on Nifty

## 9. Tests (`tests/`) ✅ DONE (parser; persistence verified via live fetch)
- [ ] `test_parse.py` — parse the saved sample fixture; assert strike count, CE/PE split, expiry, VIX (no network)
- [ ] Assert the underlying/blank-type row is skipped; strike intervals correct
- [ ] `test_store.py` — insert parsed rows, read back; assert shared `ts`, types (DB-dependent → mark/skip if no DB)
- [ ] `uv run pytest` green

## 10. Verify gate → advance to Phase 2 only when ALL true ⏳ (only the intraday-refresh run remains)
- [ ] Both chains fetched in **exactly 2 API calls**
- [ ] `snapshots` rows written: shared `ts`, correct `expiry`, CE/PE, strikes at 50/100 intervals
- [ ] **Intraday OI refresh proven** (or `depth()` fallback wired) — finding written down
- [ ] Parser unit tests pass against the fixture

---

## Out of scope (deferred — don't build here)
- DTE/expiry suppression logic → **Phase 2**
- Wall + neighbor strike selection / zone locking → **Phase 3**
- Δ%-over-window, streak, dual-index verdict → **Phase 4**
- `verdicts` table + backtest bucketing → **Phase 5**
- `/tick` `/state` `/set-zones` endpoints, cron, frontend → **Phase 6+**
