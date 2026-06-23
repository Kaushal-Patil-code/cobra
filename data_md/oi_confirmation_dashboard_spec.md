# COBRA — OI Confirmation Dashboard · Engineer Build Brief (v2)

> Transcribed from `data_md/Kaushal OI.pdf`. This is the product/logic
> specification for the COBRA service. Source-of-truth for what we're building.

- **Owner:** Hi (Nifty intraday options trader)
- **Builder:** AI engineer, using Claude Code (Max plan) + this repo
- **Companion file:** `oi_confirmation_dashboard.jsx` — a working React reference for the
  verdict logic. **Read it first; reuse the logic.** Ignore its paste / manual-entry UI —
  the real app has **NO manual input.**

**v2 changes:**
- Fyers confirmed (live) as the single data source for **both** Nifty + Sensex OI.
- Fully automatic, no manual data entry.
- Added a **strike auto-selection** rule.
- Added an **expiry-awareness layer** (Nifty and Sensex expire on different days — important).
- Fetch cadence set to **3 min**.

---

## Table of contents

0. [TL;DR — what you're building](#0-tldr--what-youre-building)
1. [Context](#1-context)
2. [Data source — Fyers](#2-data-source--fyers-verified-live-both-indices)
3. [Expiry awareness ⚠️](#3-️-expiry-awareness--read-this-the-non-obvious-gotcha)
4. [Strike auto-selection](#4-strike-auto-selection-given-the-two-zones)
5. [The brain — verdict logic](#5-the-brain--verdict-logic-implement-exactly)
6. [Architecture](#6-architecture-free-first-on-render-local-fallback)
7. [Fyers daily auth (headless)](#7-the-one-genuine-hard-part--fyers-daily-auth-headless)
8. [How to drive Claude Code](#8-how-to-drive-claude-code)
9. [Phased build plan](#9-phased-build-plan)
10. [Cost summary](#10-cost-summary)
11. [Do NOT](#11-do-not)
12. [Confirm with Hi (defaults chosen)](#12-confirm-with-hi-defaults-chosen)

---

## 0. TL;DR — what you're building

An **always-on, fully automatic** service that during market hours fetches **Nifty + Sensex
option OI** at auto-selected strikes, computes how each wall's OI is changing, derives a
**dual-index "fade / don't-fade" verdict**, shows it on a **live dashboard**, and **pushes a
Telegram alert when the verdict flips.**

The only human input is **two Nifty price zones, entered once per session** (Hi derives them
from his COBRA rules). **No pasting OI, no feeding snapshots — ever.**

**Hard rule for cost:** the *running* service uses **zero LLM calls**. All logic is plain
arithmetic. (As of 15 Jun 2026, programmatic Claude / Agent-SDK usage bills separately from
the Max subscription. Not needed here, so don't add it.)

---

## 1. Context

Hi trades a **range / fade** system on Nifty. He fades OI "walls":

- **Resistance / cap** (strike with heavy CALL OI) → he buys **PE** expecting the cap to hold.
- **Support / floor** (heavy PUT OI) → he buys **CE** expecting the floor to hold.

This dashboard is a **confirmation layer, not a signal generator.** It does **NOT** decide
entries (his trigger is a separate 5-min price-rejection candle). It tells him **whether to
trust the fade and how hard to hold / size**, by reading whether the wall's OI is *building*
(holds) or *unwinding* (breaking) — and whether **Nifty and Sensex agree.**

> ⚠️ **The dual-index rule is NOT yet validated** (paper-test, needs 8–10 clean setups). So v1
> must **log every snapshot + verdict to a DB** for backtesting. Treat verdicts as hypotheses
> to measure, not gospel.

**Level mapping:** Sensex ≈ Nifty × **3.20** (verified live 18 Jun: 77,571 / 24,209 = 3.204).

---

## 2. Data source — Fyers (VERIFIED LIVE, both indices)

Confirmed by a live pull on 18 Jun 2026 from the owner's account. **Fyers is the only API you
need** — it serves Nifty *and* Sensex OI. (No NSE scraping; NSE/NSEBSE has no Sensex option
chain anyway. Dropping NSE also removes all the rate-limit / blocking pain.)

**Endpoint:** Fyers v3 option chain — `optionchain` / SDK
`fyers.optionchain(data={"symbol":..., "strikecount":...})`.

| Underlying | Symbol             | Returns                                                                                                                                                |
|------------|--------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| Nifty      | `NSE:NIFTY50-INDEX` | per-strike `oi`, `oich` (OI chg vs prev day), `oichp` (%), `prev_oi`, `ltp`, `volume`; totals `callOi` / `putOi`; nearest expiries; India VIX |
| Sensex     | `BSE:SENSEX-INDEX`  | same structure                                                                                                                                         |

**Notes:**

- **One call per underlying returns the whole chain** → only **2 calls per refresh**.
- Rate limit is **1 lakh requests/day** — at 3-min cadence we use ~250/day. Non-issue.
- `oich` / `oichp` in the response are **vs previous-day** OI. For our intraday-window Δ% we
  compute **our own** deltas from successive snapshots (don't rely on `oich`).
- Nifty strike interval = **50**; Sensex strike interval = **100**.
- **Lot sizes (confirmed):** Nifty = **75** (resolves the old 65-vs-75 confusion), Sensex = **20**.
  (Not used by this tool, but correct for any ₹ math.)
- **M2 must validate** that `oi` actually refreshes intraday (not frozen at prev-day). If it's
  delayed, fall back to Fyers market-depth (`depth()`) per monitored strike — only ~12 strikes,
  still trivial vs the rate limit.

---

## 3. ⚠️ Expiry awareness — READ THIS (the non-obvious gotcha)

**Verified live:** Nifty weekly expiry = **TUESDAY** (next 23 Jun). Sensex weekly expiry =
**THURSDAY** (18 Jun was Sensex expiry). **They are never at the same days-to-expiry (DTE).**

On / near an index's expiry, its OI **collapses and whipsaws from settlement — NOT direction.**
Example from the 18 Jun pull (Sensex expiry day): Sensex 77400 PE showed `oichp` of
**+10,810%**, premium ₹0.05. A naive cross-check reads that as a breakdown. **It isn't.**

**DTE by weekday:**

| Day | Nifty DTE | Sensex DTE | Cross-check status                                          |
|-----|-----------|------------|-------------------------------------------------------------|
| Mon | 1         | 3          | Nifty near-expiry → caution                                 |
| Tue | **0**     | 2          | Nifty expiry → suppress (owner doesn't trade Tue, Rule 33)  |
| Wed | ~6        | 1          | Sensex near-expiry → caution                                |
| Thu | 5         | **0**      | Sensex expiry → Nifty-only                                  |
| Fri | 4         | 6          | both clean ✅                                                |

**Required handling in the verdict engine:**

1. Compute DTE for each index **every fetch** (from the chain's nearest expiry date).
2. If **either index is 0-DTE** → **suppress the cross-check** → fall back to single-index
   (Nifty-only) verdict, labelled `EXPIRY — Sensex cross-check paused`.
3. If either is **1-DTE** → still compute, but tag `near-expiry, low weight` on the banner.
4. **Validation:** tag every logged setup with weekday + both DTEs. Judge the dual-index rule
   **per weekday bucket** — the cleanest reads are Friday (and partly Mon/Wed). Do **NOT**
   conclude the rule works / fails on distorted days.

*(Defaults above are tunable — confirm with Hi.)*

---

## 4. Strike auto-selection (given the two zones)

**Input:** two Nifty price zones from COBRA, each a band `[low, high]` (if Hi gives a single
level `L`, use `[L-25, L+25]`).

- **Higher zone = RESISTANCE → track CALL (CE).**
- **Lower zone = SUPPORT → track PUT (PE).**

**Per zone, at session start (first fetch), then LOCKED for the day:**

```
NIFTY (interval 50):
  candidates = multiples of 50 in [low-50, high+50]
  WALL = candidate with highest OI in the zone's type (CE for resistance / PE for support) → lock
  MONITORED = { WALL-50, WALL, WALL+50 }        # 3 strikes

SENSEX (interval 100):
  mapped band = [low × 3.20, high × 3.20]
  candidates = multiples of 100 in [mapped.low-100, mapped.high+100]
  WALL = candidate with highest OI in the same type  → lock
  MONITORED = { WALL-100, WALL, WALL+100 }       # 3 strikes
```

- **Lock at session start** so each strike has a clean OI time-series. If the OI peak later
  migrates to a neighbor (e.g. WALL+50 overtakes WALL), **flag it** ("wall shifting up") — that
  migration is itself signal — **but don't silently re-pick the wall.**
- **Verdict uses the WALL (primary)** of each index; neighbors are context (writers rolling
  *out* = wall extending / strengthening; rolling *in* toward spot = weakening).
- Total tracked = 2 zones × (3 Nifty + 3 Sensex) = **12 strikes**, still just **2 API calls** per fetch.
- Use the **nearest weekly expiry** per index (Nifty Tue chain, Sensex Thu chain).

---

## 5. The brain — verdict logic (implement exactly)

**Reference implementation:** `oi_confirmation_dashboard.jsx` (`classify`, `computeStreak`,
`windowChange`, `zoneVerdict`). Port faithfully; **spec below wins on any ambiguity.**

### 5.1 Per-strike, each fetch

Store one timestamped `oi` per monitored strike. Then per strike:

| Quantity        | Definition                                                                                                          |
|-----------------|---------------------------------------------------------------------------------------------------------------------|
| **Δ% over window** | `(latest - value_at_or_before(now - window)) / value_at_or_before × 100`. **Window = 15 or 30 min (toggleable).** |
| **Direction**   | up = OI **building**; down = **unwinding**                                                                           |
| **Magnitude**   | `<5%` noise · `5–10%` mild · `≥10%` signal · `≥20%` strong                                                           |
| **Read**        | one snapshot-to-snapshot move **≥ 3%** = a directional read                                                          |
| **Trend**       | **3+** consecutive same-direction reads                                                                              |

> **Critical filter:** a **single one-snapshot unwind is a FAKE.** Never call a breakout off one
> read — needs **streak ≥ 2 AND cross-index confirmation.**

### 5.2 Dual-index verdict per zone (CE for resistance, PE for support)

Compare the **WALL** signal on Nifty vs Sensex:

| Nifty          | Sensex            | Verdict                                                                | Meaning                                                  |
|----------------|-------------------|-----------------------------------------------------------------------|----------------------------------------------------------|
| building       | building          | **CAP/FLOOR HOLDING** (HIGH if both ≥ signal/strong or either TREND, else MODERATE) | Fade OK. HIGH → hold full target / size up.              |
| unwinding      | unwinding         | **BREAKOUT/BREAKDOWN** (UNCONFIRMED if single read)                   | Don't fade; go with move. Unconfirmed → wait to sustain. |
| opposite dirs  |                   | ⚠️ **DIVERGENCE**                                                     | Fake-out risk. Stand down / tiny.                        |
| one flat       | other moving      | **PARTIAL — one index only**                                          | No cross-confirmation. Caution.                          |
| flat           | flat              | **NO SIGNAL**                                                         | Quiet.                                                   |
| any            | suppressed/no data | **NIFTY-ONLY (state it)**                                            | Sensex paused (expiry) or unavailable.                   |

**Apply §3 expiry suppression before this:** if a zone's cross-check is suppressed, **force the
NIFTY-ONLY row.**

Thresholds + DTE rules **live in one config block** so Hi can tune them.

---

## 6. Architecture (free-first on Render; local fallback)

### 6.1 Render free-tier reality

- Free **web services spin down after ~15 min idle** + cold-start slowly → an internal timer
  won't fire reliably when idle.
- Free Postgres has limits and **expires**; free FS is ephemeral.
- **Static sites (CDN) don't sleep** → good for the frontend.
- *(Render free limits change — verify current.)*

### 6.2 Recommended free design

External scheduler drives the fetch; external free DB stores; Render hosts API + frontend.

```
[cron-job.org]  --GET /tick every 3 min, 09:15-15:30 IST-->  [Render web service: API + fetcher]
  (free, precise)                                                          |
                                Fyers optionchain: NSE:NIFTY50-INDEX + BSE:SENSEX-INDEX  (2 calls)
                                                                           |
                                                  extract OI for the 12 monitored strikes
                                                                           |
                                                  upsert snapshot ----> [Supabase free Postgres]
                                                                           |
                                    compute Δ% + DTE-aware dual-index verdict (pure code)
                                                                           |
                            verdict flipped to actionable & not suppressed?  --> [Telegram] (alert)

[Render static site: React dashboard]  --polls /state ~30s-->  [/state]  -->  [Supabase]
```

- **Scheduler:** cron-job.org (free, sub-minute intervals, time-window restriction). Pings
  `/tick` → keeps Render awake **AND** triggers the fetch. (GH Actions cron is an alternative
  but timing is imprecise + eats free minutes — prefer cron-job.org.)
- **DB:** Supabase free Postgres. Tables:
  - `snapshots(ts, index, type, strike, oi)`
  - `verdicts(ts, zone, verdict, conviction, nifty_sig, sensex_sig, dte_n, dte_s, weekday)`

  The `verdicts` log **is the backtest dataset.**
- **API + fetcher:** small FastAPI (Python) or Express (Node). Endpoints: `GET /tick`
  (fetch + compute + store + alert), `GET /state` (latest for frontend), `GET /set-zones`
  (POST the two zones), `GET /health`.
- **Frontend:** adapt `oi_confirmation_dashboard.jsx` — **remove paste/manual UI**, add a small
  "set two zones" form, poll `/state`. Keep verdict banners, tables, window toggle, M-notation.
  Deploy as a Render static site.
- **Alerts:** Telegram (free, instant). Reuse the owner's existing bot (his NEWS service uses
  `send_news_to_telegram`) — reuse that bot token + chat ID, or make a fresh bot via @BotFather.
  Fire on **verdict transitions** to `HOLDING-HIGH` / confirmed `BREAKOUT/BREAKDOWN` /
  `DIVERGENCE`. **De-dupe, market hours only, and skip when expiry-suppressed.**

### 6.3 Local fallback

Same code on a laptop during market hours: internal scheduler (APScheduler / node-cron),
SQLite, dashboard served locally. **₹0, more reliable, only runs when the laptop is on.** Keep
code deployment-agnostic (env/config only) so switching is trivial.

---

## 7. The one genuine hard part — Fyers daily auth (headless)

Fyers **access tokens expire daily** and need an interactive OAuth login (redirect → auth code
→ access token). On a server this is the main ops task. Plan:

- Store `app_id` / `secret_key` as **env vars / secrets** (never in code/URLs).
- Implement the daily refresh: automate via Fyers' login flow (incl. TOTP) **or** a small
  once-a-day manual step where Hi pastes the fresh token each morning. Document whichever you pick.
- **Never log tokens. Never put OI / keys in query strings.**

> Repo status: this is already implemented — see `auth/` (TOTP auto-login, tokens persisted in
> the `fyers_tokens` table, single-flight startup warm-up + retries).

---

## 8. How to drive Claude Code

1. Put this `SPEC.md` + `oi_confirmation_dashboard.jsx` in the repo root.
2. Work **interactively in the terminal/IDE** — that draws from the Max subscription (no extra
   cost). **Don't** wire the running app to the Agent SDK / `claude -p` / GitHub-Actions-Claude
   (bills separately since 15 Jun 2026, and isn't needed).
3. **Usage heads-up:** Claude Code shares the same usage pool as Hi's claude.ai COBRA chats.
   Long build sprints on Hi's login eat his trading-chat budget → build outside market hours, or
   use a separate Pro account ($20/mo, also includes Claude Code).

---

## 9. Phased build plan

> **Strategy:** the Fyers auto-login (§7, the hard part) is **done**, so we build the main logic
> in small, independently-verifiable **phases**. Each phase has an explicit **Verify** gate —
> don't advance until it passes against live Fyers data and/or the JS reference. Building this
> way surfaces bugs early (per §1, verdicts are unvalidated hypotheses to *measure*) instead of
> letting them compound across the pipeline.
>
> Status legend: ✅ done · 🔜 next · ⬜ pending.

### Phase 0 — Fyers daily auth (headless) ✅ DONE · [§7]

- **Built:** TOTP auto-login, tokens persisted in the `fyers_tokens` table (not on disk),
  single-flight startup warm-up via a Postgres advisory lock, flow-level + per-request retries.
- **Verify:** ✅ headless login succeeds, token reused from DB while fresh, retries on failure.

### Phase 1 — Fetcher + raw snapshots ✅ DONE (intraday-refresh run pending market hours) · [§2]

- **Goal:** pull both option chains (`NSE:NIFTY50-INDEX` + `BSE:SENSEX-INDEX`, 2 calls), parse
  per-strike `oi`/`ltp`/`volume`, nearest expiries, India VIX; store timestamped `snapshots`.
- **Verify / catch bugs:** confirm OI **actually refreshes intraday** (not frozen at prev-day) —
  the spec's flagged unknown (§2). Check both symbols return data, strike intervals (50 / 100),
  and the response shape. If `oi` is delayed, fall back to `depth()` per monitored strike.

### Phase 2 — Expiry / DTE awareness ✅ DONE (compute/expiry.py + tests; wired into the verdict in Phase 4) · [§3]

- **Goal:** compute DTE per index each fetch from the chain's nearest expiry; weekday tagging;
  0-DTE → suppress cross-check (NIFTY-ONLY); 1-DTE → low-weight flag.
- **Verify:** DTE matches the Tue (Nifty) / Thu (Sensex) reality; suppression fires on expiry
  days; the `EXPIRY — Sensex cross-check paused` label appears.

### Phase 3 — Strike auto-selection ✅ DONE (selection + zones/monitored_strikes + expiry-roll-aware lock; verified live) · [§4]

- **Goal:** from the two zones, pick each index's WALL (highest OI of the zone's type) + the two
  neighbors, map the Sensex band ×3.20, **lock at session start**, flag migration (never re-pick).
- **Verify:** WALL = highest-OI strike of the correct type; 3 strikes/index locked; migration
  flag fires when a neighbor overtakes; 12 strikes total across both zones, still 2 API calls.

### Phase 4 — Verdict engine (the brain) ✅ DONE · [§5]

- **Goal:** per-strike Δ% over the 15/30-min window; direction / magnitude / read / trend; the
  "single one-snapshot unwind = FAKE" streak filter; the dual-index verdict table; apply §3
  suppression first. Thresholds + DTE rules in one config block.
- **Verify:** unit-test **every verdict row** against `oi_confirmation_dashboard.jsx`
  (`classify`, `computeStreak`, `windowChange`, `zoneVerdict`); confirm the streak ≥ 2 AND
  cross-index gating, plus the DIVERGENCE and NIFTY-ONLY paths.
- **Built:** `config/thresholds.py` (the one tunable config block), `compute/verdict.py` (pure
  brain), `compute/engine.py` (orchestrator: `build_state(trading_date, now, window)`),
  `compute/series.py` (read accessors), `schemas/verdict.py` (the `/state` contract). 25 tests
  cover every §5.2 row + streak/divergence/NIFTY-ONLY/suppression paths (62 total, all green).
- **Dependency RESOLVED:** the companion `oi_confirmation_dashboard.jsx` does **not** exist
  anywhere — the logic was ported from §5 directly (spec wins on ambiguity). Interpretations made:
  `flat ⟺ |Δ%| < 5%` (noise); a confirmed BREAKOUT/BREAKDOWN needs `streak ≥ 2` on **both**
  indices; `value_at_or_before(now−window)` is strict (no baseline before the cutoff →
  "insufficient", not an approximated short-window Δ%).
- **Pulled forward:** the `verdicts` backtest table DDL (Phase 5) is in `db/schema.sql` +
  `data_md/phase4_ddl.md` (run manually); a React dashboard (Phase 6) is scaffolded in
  `frontend/` against the `/state` contract with a sample-data fallback. Persisting verdicts and
  serving `/state` are still Phases 5/6.

### Phase 5 — Persist verdicts (backtest dataset) ✅ DONE · [§1, §11]

- **Goal:** log `verdicts(ts, zone, verdict, conviction, nifty_sig, sensex_sig, dte_n, dte_s,
  weekday)` alongside every snapshot; bucket by weekday/DTE.
- **Verify:** every fetch writes a verdict row; weekday/DTE buckets populate; data is queryable
  for the 8–10 clean-setup paper-test (judge the rule **per weekday bucket**, §3).
- **Built:** `market/verdict_store.py` (write `insert_verdicts` + reads `read_verdicts` /
  `read_verdicts_range` / `bucket_counts` by weekday), `compute/persist.py` (`persist_state`
  logs one row per zone — incl. quiet/suppressed rows so distorted days bucket out; plus
  `build_history`), and **`market/tick.py` `run_tick()` — the unified cycle**
  (fetch → store snapshots → lock walls *from the same chains* → compute → persist). The shared
  fetch closes the Phase-4 FN-1 gap: stored snapshots are guaranteed to cover the locked walls.
  Frontend gained a Live⇄History tab + backtest view (`HistoryView`) on the `/history` contract.
  7 persistence tests (62 → **69 total**, all green). No required DDL — the `verdicts` table
  exists from Phase 4; `data_md/phase5_ddl.md` has optional `outcome`/`notes` labeling columns.
- **Note:** `run_tick` is the body the Phase-6 `GET /tick` endpoint will call; `build_history`
  backs the Phase-6 `GET /history`. Both are built but not yet exposed over HTTP.

### Phase 6 — API + frontend ✅ DONE (live full-pipeline /tick run pending market hours) · [§6.2]

- **Goal:** `GET /tick` (fetch+compute+store+alert), `GET /state`, `POST /set-zones`,
  `GET /health`; adapt the React dashboard — remove paste UI, add a "set two zones" form, poll
  `/state`.
- **Verify:** `/tick` runs the whole pipeline; zones lock via the API; `/state` reflects the
  latest verdict.
- **Built:** `app/api/dashboard.py` blueprint — `GET /state` (build_state JSON), `GET /history`
  (build_history; default 2-week range), `GET /zones`, `POST /set-zones` (validates two bands via
  `schemas.zones.SetZonesRequest`, roles auto-assigned by `Zone.pair_from_bands`), `GET /tick`
  (runs `run_tick`; skips outside 09:15–15:30 IST Mon–Fri and on the **M5-2 market-minute de-dup**
  — `?force=true` overrides). DB-down → 503; bad args → 400; bad body → 422 (fixed the error
  handler to JSON-serialize pydantic `ctx`). The React app (Live + History, built Phases 4–5)
  already polls these; the vite proxy targets `:8000`. 13 API tests (70 → **83 total**, all green);
  all five endpoints curl-verified on a live boot.
- **Alerts:** `GET /tick` does fetch+store+compute+persist; the Telegram alert on verdict flips
  is **Phase 7** (not yet wired into the tick).
- **To go live for the frontend:** restart the gunicorn so it serves the new routes, then
  `cd frontend && npm run dev` (proxies to `:8000`). A real end-to-end `/tick` needs a valid Fyers
  token + market hours (Mon 09:15+ IST).

### Phase 7 — Telegram alerts ⬜ · [§6.2]

- **Goal:** fire on verdict **transitions** (HOLDING-HIGH / confirmed BREAKOUT/BREAKDOWN /
  DIVERGENCE); de-dupe; market-hours only; skip when expiry-suppressed.
- **Verify:** alerts fire **only on flips** (not every tick); de-dup holds; suppressed zones
  don't alert.

### Phase 8 — Deploy ⬜ · [§6]

- **Goal:** Render (static frontend + web service) + cron-job.org @ 3 min, market hours; OR the
  local fallback (APScheduler + SQLite).
- **Verify:** scheduler pings `/tick` on cadence within the window; service stays awake; or the
  local run works end-to-end.

---

## 10. Cost summary

| Item                              | Cost                                                  |
|-----------------------------------|-------------------------------------------------------|
| Building (interactive Claude Code) | Covered by existing Max sub (shared pool — §8)        |
| Running (LLM)                     | **₹0** — no Claude at runtime                          |
| Fyers API                         | Free with a Fyers account                             |
| Scheduler (cron-job.org)          | Free                                                  |
| DB (Supabase free)                | Free                                                  |
| Hosting (Render free)             | Free, with spin-down caveats (§6.1)                   |
| Telegram alerts                   | Free                                                  |
| If Render free is unreliable      | Local run = **₹0** (laptop on during market hours)    |

---

## 11. Do NOT

- ❌ Add any LLM call in the running service (breaks "free", not needed).
- ❌ Treat the dual-index verdict as proven — it's in paper-test. Build to **log + measure**, and
  **bucket by weekday/DTE** (§3).
- ❌ Compare Nifty vs Sensex OI blindly on expiry days (§3) — suppress.
- ❌ Hardcode/commit secrets; never put tokens or OI in URLs.
- ❌ Silently fail when Sensex is suppressed/missing — show "Nifty-only".
- ❌ Re-pick the wall mid-session — lock it; flag migration instead (§4).

---

## 12. Confirm with Hi (defaults chosen)

- **Zone input format** = band `[low, high]` (single level → ±25). *Band or single level?*
- **Strikes per zone** = wall ± 1 (3 per index). *Want wall ± 2?*
- **Fetch cadence** = 3 min, 09:15–15:30 IST. *(Confirmed fine on rate limits.)*
- **Expiry handling** = suppress cross-check at 0-DTE, flag at 1-DTE. *OK?*
- **Alert channel** = Telegram (reuse existing bot). *Change?*
- **Default window** = 15 min (toggle 15/30). *OK?*

---

> 🐍 COBRA · v2 build brief · Fyers serves both Nifty + Sensex OI (verified live) · runtime is
> pure code, no LLM · mind the Tue/Thu expiry mismatch · validate the rule before trusting it.
