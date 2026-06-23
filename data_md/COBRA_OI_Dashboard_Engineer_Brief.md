# COBRA — OI Confirmation Dashboard · Engineer Build Brief (v3 · FINAL)

**Owner:** Hi (Nifty intraday options trader)
**Builder:** AI engineer, using Claude Code (Max plan) + this repo
**Companion file:** `oi_confirmation_dashboard.jsx` — reference implementation of the **verdict math** (`classify`, `computeStreak`, `windowChange`, `zoneVerdict`). **Reuse that math. Ignore its paste UI and its typed-strike/zone model — both are superseded by this doc.**

> **Status:** Data source **verified live** (Fyers serves both Nifty + Sensex OI). Most of the existing port is correct. This v3 changes two things: the **strike model** (now a spot-anchored ladder, no typed strikes) and the **expiry model** (run all 5 days with an EXPIRY/PIN tag instead of suppressing). Details in §A.

---

## 0. TL;DR

An **always-on, fully automatic** service that, during market hours, fetches **Nifty + Sensex option OI** across a spot-anchored strike ladder, finds each index's **cap** (highest CALL OI) and **floor** (highest PUT OI), tracks how that OI changes intraday, and produces a **dual-index "fade / don't-fade" verdict** shown on a live dashboard, with a **Telegram alert when the verdict flips**.

- **No manual data entry, ever.** The app reads live spot and builds everything itself.
- It's a **confirmation layer, not a signal generator** — it tells Hi whether to trust a fade and how hard to hold/size; his entry trigger (a 5-min rejection candle) is separate.
- **Runtime uses zero LLM calls** — pure arithmetic. (Programmatic Claude/Agent-SDK usage bills separately since 15 Jun 2026; not needed here, don't add it.)

---

## A. CHANGES FROM YOUR CURRENT BUILD (read first)

Your current port (the "COBRA DATA LOGIC" P0–P5 doc) is mostly correct — fetch, snapshots, DTE, the verdict matrix, persistence, per-index resilience all stay. **Change these:**

1. **Strike selection → spot-anchored ladder (replaces P3 entirely).**
   Delete the band / `L±25` / "two typed zones" logic. Instead: read **live spot** per index, build a fixed **8-strike ladder** around it (§3), and track **both CE and PE** on every rung. Cap = highest CE OI in the ladder; floor = highest PE OI. Your "lock at session start + flag migration, never re-pick" behaviour stays — only the candidate set changes.

2. **Expiry handling → EXPIRY/PIN tag, no suppression (replaces pts 9–11, 21).**
   Delete the "0-DTE → force NIFTY-ONLY suppression" rule. The tool runs **all 5 days**. On expiry, a wall **pins** (stable/building OI = HOLDING = fade OK — that's correct and useful). The **only** special rule: an OI **unwind on the 0-DTE index is settlement, not a breakout** — it must NOT produce a BREAKOUT/BREAKDOWN verdict; read it as PIN/HOLD. Tag the 0-DTE index "EXPIRY/PIN". (Nifty 0-DTE = Tue, Sensex 0-DTE = Thu.) `Sensex data missing` still → NIFTY-ONLY (that's a data case, not expiry).

3. **Drop the hardcoded ×3.20 ratio for strike placement.**
   Because each index now anchors on **its own** live spot, you no longer map Nifty→Sensex strikes via the ratio. This removes the ratio-drift bug. Keep the ratio only as a sanity log (today 3.204).

Everything else below is the full, self-contained spec.

---

## 1. Context

Hi trades a **range/fade** system on Nifty. He fades OI walls:
- **Cap / resistance** (strike with heavy CALL OI) → buys PE expecting the cap to hold.
- **Floor / support** (heavy PUT OI) → buys CE expecting the floor to hold.

The dashboard reads whether the cap/floor OI is **building** (holds → fade) or **unwinding** (breaking → don't fade), and whether **Nifty and Sensex agree**.

> ⚠️ The dual-index cross-check is **not yet validated** (paper-test, 8–10 setups needed). Log everything; bucket by weekday/DTE; treat verdicts as hypotheses to measure.

---

## 2. Data source — Fyers (verified live, both indices)

**Fyers is the only API needed** — it serves Nifty *and* Sensex OI. (NSE/NSEBSE has no Sensex option chain; dropping NSE also removes scraping/rate-limit pain.)

Endpoint: Fyers v3 `optionchain` (SDK `fyers.optionchain(data={"symbol":..., "strikecount":...})`).

| Underlying | Symbol | Returns (per strike) |
|---|---|---|
| Nifty | `NSE:NIFTY50-INDEX` | `oi`, `oich`, `oichp`, `prev_oi`, `ltp`, `volume`; chain totals `callOi`/`putOi`; nearest-expiry list; India VIX; index spot in the `-INDEX` row (`fp`/`ltp`) |
| Sensex | `BSE:SENSEX-INDEX` | same structure |

- **One call per index = 2 calls/cycle** (whole chain returned).
- Rate limit **1 lakh/day**; at 3-min cadence ≈ 250 calls/day → non-issue.
- Fyers' `oich`/`oichp` are **vs previous day** — reference only. Compute your **own** intraday Δ% from successive snapshots.
- Strike intervals: **Nifty 50, Sensex 100.**
- Live spot is in the response (the index row, `fp`/`ltp`) — use it to anchor the ladder; or a separate quote call.
- Lot sizes (correct, though unused here): Nifty **75**, Sensex 20.
- **M2 must confirm `oi` refreshes intraday** (not frozen at prev-day). If delayed, fall back to market-depth (`depth()`) per monitored strike — only 32 series, still trivial vs the limit. *(This is the one thing not yet tested live.)*

---

## 3. Strike grid — spot-anchored ladder

**At session start, per index, read live spot → build a fixed 8-strike ladder. Track BOTH CE and PE on every rung.**

```
ATM        = round(spot / interval) * interval      # Nifty interval 50, Sensex 100
ladder     = [ ATM+3i, ATM+2i, ATM+1i, ATM, ATM-1i, ATM-2i, ATM-3i, ATM-4i ]   # i = interval
             # 8 strikes: ATM + 3 up + 4 down (intentionally skewed downward)
```

**Nifty example** (spot 24,000, i=50): `24150 24100 24050 24000(ATM) 23950 23900 23850 23800`
**Sensex** (own spot ~77,400, i=100): `77700 77600 77500 77400(ATM) 77300 77200 77100 77000`

- Both CE & PE on each rung → **8 × 2 types × 2 indices = 32 OI series**, still **2 API calls**.
- Each index anchors on **its own** live spot — **no ×3.20 mapping** for placement.
- **Walls (per index), locked at session start from the locked ladder:**
  - **CAP** = ladder strike with the highest **CE** OI.
  - **FLOOR** = ladder strike with the highest **PE** OI.
- Keep your existing **lock + migration-flag** behaviour: don't re-pick the wall mid-session; if a neighbour's OI strictly exceeds the locked wall's, emit `cap/floor shifting up/down` (a context signal). Verdict Δ% tracks the **locked** wall strike's own series (consistent).
- Store all 8 rungs (CE+PE) per index for context/history, not just the walls.
- **Ladder-exit signal:** if live spot leaves the locked ladder's range, don't silently re-centre — emit `RANGE BROKEN → likely trend day, stop fading`. Re-centre only on a manual reset / next session. (That exit is itself useful: range→trend.)

*(Reach is Hi's exact spec: ATM + 3 up + 4 down. Easily widened if it exits too often — see §13.)*

---

## 4. Expiry awareness — all 5 days, EXPIRY/PIN tag

**Verified live:** Nifty weekly expiry = **Tuesday**; Sensex weekly expiry = **Thursday**. They're never at the same DTE.

`DTE = (nearest_expiry_date − today_IST).days` (calendar days; 0 on expiry day), computed **per index** every tick.

**Run the tool every day. No DTE suppression.** Handling:
- On the **0-DTE index**, walls **pin** → stable/building OI reads as **HOLDING** (fade OK). Tag that index's signal `EXPIRY/PIN`.
- **The one guard:** an OI **unwind on the 0-DTE index = settlement, not a breakout.** It must **not** drive a BREAKOUT/BREAKDOWN verdict — read as PIN/HOLD instead. (Building OI on 0-DTE = HOLDING as normal.)
- `near_expiry = DTE==1` → still compute, tag `near-expiry, low weight`.
- `Sensex data missing` (fetch/token failure) → NIFTY-ONLY row (data case, not expiry).
- **Validation:** every persisted verdict row carries `weekday`, `dte_n`, `dte_s`. The dual-index cross-check is cleanest on **Friday** (both mid-cycle); Mon/Wed one index is 1-DTE; Tue/Thu one is 0-DTE/pinning. Judge the rule **per weekday bucket** — never on pooled days.

---

## 5. Verdict engine

Reference math: `oi_confirmation_dashboard.jsx`. Spec wins on ambiguity. **Thresholds all live in `config/thresholds.py`.**

### 5.1 Per strike, each tick
| Quantity | Definition |
|---|---|
| **Δ% over window** | `(latest_oi − baseline_oi)/baseline_oi × 100`, baseline = last snapshot **at-or-before** `now − window` (strict; **window 15/30 min toggle**). No baseline or baseline 0 → `insufficient`. |
| **Direction** | `flat` if `\|Δ%\|<5`; else `up`=building / `down`=unwinding |
| **Magnitude** | `<5` noise · `5–10` mild · `≥10` signal · `≥20` strong |
| **Read** | one snapshot-to-snapshot move ≥ **3%** (⚠ see note) |
| **Trend** | streak **≥3** same-direction reads; a flat latest pair resets streak to 0 |

> ⚠️ **Read threshold (3%) is likely too stiff for 3-min ticks** — OI usually builds gradually, so single-step ≥3% moves are rare and "trend"/breakout-confirm will seldom fire. The window-Δ% verdict still works. **Expect to drop this to ~1.5–2% after observing live 3-min OI.** Keep it in config.

### 5.2 Dual-index verdict (computed for CAP-side and FLOOR-side separately)
Compare the **locked wall** signal, Nifty vs Sensex (CAP uses CE; FLOOR uses PE). Apply §4 (EXPIRY/PIN guard; Sensex-missing → NIFTY-ONLY) **first**.

| Nifty | Sensex | Verdict |
|---|---|---|
| building | building | **CAP/FLOOR HOLDING** — HIGH if both ≥signal or either TREND, else MODERATE → fade OK (HIGH = hold full target / size up) |
| unwinding | unwinding | **BREAKOUT/BREAKDOWN** — confirmed only if **both streaks ≥2**, else **UNCONFIRMED** (single unwind = fake) → don't fade / go with move |
| opposite dirs | | **DIVERGENCE / LOW** → stand down / tiny |
| one moving, one quiet | | **PARTIAL / LOW** → caution |
| both quiet | | **NO SIGNAL** |
| any | suppressed/missing | **NIFTY-ONLY** (state it) |

---

## 6. Max-pain + PCR (confirmed — include)

Both compute from the chain you already pull — **no extra API calls.** Hi's Rule 33: **max-pain dominates on expiry**, so this is the pin magnet, most useful Tue/Thu.

- **Max-pain strike** = the strike `S` minimising total writer payout:
  `loss(S) = Σ_k CE_OI_k · max(0, S − K_k) + Σ_k PE_OI_k · max(0, K_k − S)`, evaluated over all chain strikes `K_k`; max-pain = `argmin_S loss(S)`. Compute **per index** (Nifty on its Tue chain, Sensex on its Thu chain) over the full chain, not just the 8-rung ladder.
- **PCR** = `putOi / callOi` (chain totals are in the response). Per index.
- **Display:** max-pain strike + PCR next to each index's cap/floor; surface max-pain prominently on the 0-DTE index (it's the pin target). Persist both per tick.

---

## 7. Architecture (free-first on Render; local fallback)

### 7.1 Render free-tier reality
Free **web services sleep after ~15 min idle** + cold-start slowly (internal timers won't fire when idle); free Postgres is limited/expiring; FS ephemeral; **static sites don't sleep**. *(Verify current Render limits.)*

### 7.2 Recommended free design
```
[cron-job.org] --GET /tick every 3 min, 09:15–15:30 IST Mon–Fri--> [Render web svc: API + fetcher]
  (free, precise)                                                       |
                       Fyers optionchain: NSE:NIFTY50-INDEX + BSE:SENSEX-INDEX  (2 calls)
                                                                          |
                     anchor ladders on live spot → extract 32 OI series → snapshot
                                                                          |
                                       upsert ----> [Supabase free Postgres]
                                                                          |
                           compute Δ% + DTE-aware dual-index verdict (pure code)
                                                                          |
                   verdict flipped to actionable? --> [Telegram] (alert, de-duped)

[Render static site: React dashboard] --polls /state ~30s--> [/state] --> [Supabase]
```
- **Scheduler:** cron-job.org (free, sub-minute, time-window) pings `/tick` → keeps Render awake **and** triggers the fetch. (Preferred over GH Actions cron: better timing, no minute budget.)
- **DB:** Supabase free Postgres. `snapshots(ts, index, type, strike, expiry, oi, ltp, volume, prev_oi)` + `verdicts(ts, side, wall_strike, verdict, conviction, nifty_sig, sensex_sig, dte_n, dte_s, weekday, tag)`. The whole chain is stored (not just walls) for migration/re-lock history. The `verdicts` log **is** the backtest dataset.
- **API + fetcher:** FastAPI (Python) or Express (Node). `GET /tick`, `GET /state`, `GET /history`, `GET /health`. `run_tick` = one cycle (fetch → store snapshots → lock/read walls from the **same** chains → build verdict → persist), and **never raises** (partial data never stored; one index failing never aborts the other; one re-login retry on dead token).
- **Frontend:** adapt the JSX — remove paste UI, show the live ladder + cap/floor + dual-index verdict banners + window toggle + log; poll `/state`. Deploy as a Render static site.
- **Alerts:** **Telegram** (reuse Hi's existing bot — his NEWS service uses it — or fresh via @BotFather). Fire on verdict **transitions** to `HOLDING-HIGH` / confirmed `BREAKOUT/BREAKDOWN` / `DIVERGENCE`. **De-dup** (per clock-minute guard), **market hours only**.

### 7.3 Local fallback
Same code on a laptop during market hours: APScheduler/node-cron, SQLite, dashboard served locally. **₹0, more reliable, only runs when the laptop is on.** Keep it deployment-agnostic (env/config only).

---

## 8. The real hard part — Fyers daily auth (headless)

Fyers **access tokens expire daily** and need an interactive OAuth login (redirect → auth code → token). On a server this is the main task — **not a footnote.**
- Store `app_id` / `secret_key` as **env/secrets** (never in code/URLs).
- Automate the daily refresh (Fyers login incl. TOTP) **or** a small once-a-day manual token paste. The "one re-login retry on dead token" (§7.2) only works if this is automated — otherwise a mid-session token death needs Hi to re-auth manually.
- Never log tokens; never put OI/keys in query strings.

---

## 9. Driving Claude Code

1. Put this `SPEC.md` + `oi_confirmation_dashboard.jsx` in the repo root.
2. Work **interactively in the terminal/IDE** — draws from the Max subscription (no extra cost). **Don't** wire the running app to the Agent SDK / `claude -p` / GH-Actions-Claude (bills separately since 15 Jun 2026; not needed).
3. **Usage note:** Claude Code shares Hi's claude.ai usage pool — long sprints on his login eat his trading-chat budget. Build outside market hours, or use a separate Pro account ($20/mo, includes Claude Code).

---

## 10. Milestones

1. **Fyers daily-auth** working headless (§8) — the hard part. (Data availability already proven.)
2. **Fetcher + storage** (local): both chains (2 calls), live spot, snapshot all 32 series. **Validate intraday OI refresh** (§2).
3. **Spot-anchored ladder + wall lock** (§3) + migration flag + ladder-exit signal.
4. **Verdict engine** (§5) + **expiry/pin handling** (§4). Unit-test against the JS reference.
5. **API + frontend** — live ladder + verdicts, no paste UI.
6. **Deploy** — Render static + web svc + cron-job.org @ 3 min, market hours; or local.
7. **Telegram alerts** on flips (de-duped, market hours).
8. **Max-pain + PCR panel** (§6).
9. Persistent log from M2 = backtest dataset; bucket by weekday/DTE.

---

## 11. Cost

| Item | Cost |
|---|---|
| Building (interactive Claude Code) | Existing **Max** sub (shared pool — §9) |
| Running (LLM) | **₹0** — no Claude at runtime |
| Fyers API · cron-job.org · Supabase free · Telegram | Free |
| Hosting (Render free) | Free, with spin-down caveats (§7.1) |
| If Render free is unreliable | Local = **₹0** |

---

## 12. Do NOT

- ❌ Add any LLM call in the running service.
- ❌ Treat the dual-index verdict as proven — log + measure, bucket by weekday/DTE.
- ❌ Fire BREAKOUT off a 0-DTE OI unwind — that's settlement, read as PIN/HOLD (§4).
- ❌ Re-pick the wall mid-session — lock it, flag migration (§3).
- ❌ Silently re-centre the ladder if spot exits — emit RANGE BROKEN (§3).
- ❌ Hardcode the Sensex ratio for placement, or use it to map strikes (§A.3).
- ❌ Commit secrets; never put tokens/OI in URLs.

---

## 13. Confirm / tunable (defaults chosen)

- **Spot-anchor:** ladder built from **live spot** each session, no typed strikes. ✅ (per Hi's latest)
- **Ladder reach:** ATM + 3 up + 4 down (8 strikes, skewed down). *Widen if it exits too often.*
- **Fetch cadence:** **3 min**, 09:15–15:30 IST, Mon–Fri. (Confirmed fine on rate limits.)
- **Window:** 15 min default (toggle 15/30).
- **Read threshold:** 3% default — **expect to lower to ~1.5–2%** after live 3-min observation (§5.1).
- **Expiry:** run all 5 days; EXPIRY/PIN tag + no-false-breakout on 0-DTE index; no suppression.
- **Max-pain + PCR:** included (§6).
- **Alerts:** Telegram, reuse existing bot.

---
*🐍 COBRA · v3 FINAL · Fyers serves both Nifty + Sensex OI (verified live) · spot-anchored ladder · runs all 5 days with pin-aware expiry handling · runtime is pure code, no LLM · validate the rule before trusting it.*
