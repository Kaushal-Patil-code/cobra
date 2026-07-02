# COBRA — Comment / Logic Map (developer spec)

**Purpose:** every on-screen comment the dashboard shows, the **exact condition that fires it**, the **current wording → new plainer wording**, plus **new comments to add** with their logic. Dev: apply the NEW wording to existing comments, and build the ADD rows (§5).

---

## 0. Variables (so triggers are unambiguous)

Computed **per side** and **per index** each tick:
- **CAP** side reads **CE OI at the cap strike**; **FLOOR** side reads **PE OI at the floor strike**.
- `dir` = **building** (Δ% > +5), **unwinding** (Δ% < −5), **flat** (|Δ%| ≤ 5). Δ% = OI change over the window (15/30m).
- `tier` = noise (<5%) · mild (5–10) · signal (≥10) · strong (≥20), on |Δ%|.
- `streak` = consecutive same-direction reads (read = one tick move ≥ ~2%).
- `str` = wall strength 1–5 (see §2).
- `N` = Nifty side, `S` = Sensex side. `aligned` = N.dir==S.dir and both non-flat. `divergent` = opposite non-flat.
- `dte_n`, `dte_s` = days to expiry per index. `near_expiry` = DTE==1. `expiry` = DTE==0.
- `dist` = wall_strike − spot (signed; pts **and** % of spot). `prox` = AT / APPROACHING / FAR (bands in §5).
- `vix`, `vix_regime`. `sensex_ok` = Sensex data present & fresh.

---

## 1. ⚠️ LOGIC CHANGE — near-expiry now = MORE trust (not less)

**Old:** `DTE==1` → tag "near-expiry, **low weight**" → conviction downgraded.
**New (per Hi):** closer to expiry, the wall has **matured** (positions committed, strike pins price) → a heavy wall is a **stronger** barrier → for a FADE, confidence goes **UP**.

Implement:
- **`near_expiry` (DTE==1):** tag **"wall matured — higher trust"**. For HOLDING states, reach **HIGH** conviction more easily (lower the bar). **Do NOT downgrade.**
- **`expiry` (DTE==0):** tag **"EXPIRY — wall pinning"**, strongest hold-trust. **KEEP the guard:** an OI **unwind** on the 0-DTE index = settlement → must **NOT** produce BREAKOUT/BREAKDOWN; read as **HOLD/PIN**. Surface **max-pain** as the pin target.
- **Net asymmetry:** near/at expiry biases toward **HOLDING / FADE-OK** and **away from BREAKOUT** (pins don't break; unwind = settlement). Matches R28 (pinning) + R33 (don't chase moves on expiry).
- **Risk note (unchanged):** higher trust = more conviction the wall holds; it does **not** change the ₹2,000 cap or max-2-trades.

---

## 2. Wall strength `str` (1–5) — the logic behind the STR badge

- `dominance = wall_OI ÷ median(OI of the other 7 ladder strikes)` (same option type: CE for cap, PE for floor). Ratio, never absolute OI.
- Map: `<1.3 → 1 · 1.3–1.8 → 2 · 1.8–2.5 → 3 · 2.5–3.5 → 4 · >3.5 → 5`. (Cutoffs = starting guess, tune on logged data.)
- Use: fade only `str ≥ 3` that's building/stable; **never** fade a `str 5` that's **unwinding** (real break).

---

## 3. VERDICT BOX comments — logic → old → new

| Comment (in box) | Trigger logic | Old wording | New plainer wording |
|---|---|---|---|
| **FADE OK** | state = HOLDING | FADE OK | keep ✅ |
| **DON'T FADE** | state = BREAKOUT/BREAKDOWN (confirmed) | DON'T FADE | keep ✅ |
| **WAIT** | state ∈ {DIVERGENCE, PARTIAL, NO SIGNAL, UNCONFIRMED, NIFTY-ONLY-unclear} | WAIT | keep + **add reason** (§5) |
| **CAP / FLOOR HOLDING** | `N.dir==building AND S.dir==building` | CAP/FLOOR HOLDING | **Top holding / Bottom holding** |
| — **HIGH** | both `tier≥signal` OR either `streak≥3` OR near/at-expiry mature wall | HIGH | **Strong** |
| — **MODERATE** | HOLDING but not the above | MODERATE | **OK** |
| **BREAKOUT / BREAKDOWN** | `N.dir==unwinding AND S.dir==unwinding` AND **not** expiry-settlement | BREAKOUT/BREAKDOWN | **Top breaking up / Bottom breaking down** |
| **UNCONFIRMED** | both unwinding but `N.streak<2 OR S.streak<2` | UNCONFIRMED | **Not confirmed — 1 read only** |
| **DIVERGENCE / LOW** | `N.dir` & `S.dir` opposite (one up, one down) | DIVERGENCE / LOW | **Nifty & Sensex disagree** |
| **PARTIAL / LOW** | one side non-flat, other flat | PARTIAL — one index only | **Only {index} moving, other quiet** |
| **NO SIGNAL** | both flat | NO SIGNAL | **Both quiet — nothing to do** |
| **NIFTY-ONLY** | `sensex_ok == false` | NIFTY-ONLY | **Sensex missing — Nifty alone, careful** |
| **ALIGNED / DIVERGENT** (per row) | row: N & S same dir → ALIGNED; opposite → DIVERGENT; one flat → blank | ALIGNED / DIVERGENT | **Agree / Disagree** |

---

## 4. TAGS, BADGES, ROW & HEADER — logic → old → new

**Tags / badges**

| Comment | Trigger logic | Old wording | New plainer wording |
|---|---|---|---|
| Near-expiry trust | `DTE==1` (that index) | near-expiry, low weight | **Expiry near — wall matured, higher trust** |
| Expiry pin | `DTE==0` (that index) | EXPIRY/PIN | **Expiry today — wall pinning** |
| Strength | §2 dominance → 1–5 | STR n/5 | **WALL SIZE n/5** |
| Wall broken | spot crosses wall; **latch** until spot clears back by a margin | CAP BROKEN — cleared X | **Old top X broke — don't fade the top** |
| Range broken | spot exits the 8-rung ladder | RANGE BROKEN | **Left the range — trending, stop fading** |
| Wall shift | challenger strike has ≥5% more OI (or spot crossed) | cap/floor shifting up/down | **Top moving up: 23950 → 24000** |
| Distance | `dist` = wall − spot | spot X pts below cap | **X pts (Y%) below top** |

**Row labels**

| Comment | Trigger logic | Old | New |
|---|---|---|---|
| ↑ building | Δ% > +5 | building | **OI rising (wall stronger)** |
| ↓ unwinding | Δ% < −5 | unwinding | **OI falling (wall weaker)** |
| – flat | |Δ%| ≤ 5 | flat | **no change** |
| NOISE/MILD/SIGNAL/STRONG | |Δ%| tier (§0) | same | keep (clear once scale known) |
| TREND | `streak ≥ 3` | TREND | keep ✅ |
| STREAK n | consecutive same-dir reads | STREAK n | **n in a row** |
| WALL | this is the chosen cap/floor strike | WALL | keep ✅ |

**Header**

| Comment | Trigger logic | New wording |
|---|---|---|
| MAX-PAIN | `argmin_S Σ CE_OI·max(0,S−K) + PE_OI·max(0,K−S)` | **Pin magnet {strike}** |
| PCR | `putOi ÷ callOi` | **PCR 0.84 (call-heavy)** if <1, **(put-heavy)** if >1 |
| DTE | nearest_expiry − today | **1 day to expiry** |
| SPOT / ATM | from chain | keep as-is |

---

## 5. NEW comments to ADD — logic → wording (the high-value ones)

**5.1 ACTION line (always on — the #1 add).** Compose one plain instruction from `state + prox + side`.
- `side_word`: CAP → "Top", FLOOR → "Bottom" · `fade_word`: CAP → "buy PE", FLOOR → "buy CE" · `agree_word`: aligned → "both agree", partial → "only {index}", divergent → "they disagree".
- **HOLDING + prox=AT** → `"{side} holding, {agree}, price {|dist|}pts away → FADE-NOW zone ({fade}). Wait for your 5-min candle."`
- **HOLDING + prox=APPROACHING** → `"{side} holding, {agree}, price {|dist|}pts away → get ready to fade ({fade})."`
- **HOLDING + prox=FAR** → `"{side} holding but price {|dist|}pts away → too far, just watch."`
- **BREAKOUT/BREAKDOWN confirmed** → `"{side} breaking {up/down} — DON'T FADE, stand aside."`
- **UNCONFIRMED** → append `" (1 read only — wait to confirm.)"`
- **DIVERGENCE** → `"Nifty & Sensex disagree — skip or tiny only."`
- **PARTIAL** → `"Only {index} moving, other quiet — wait for both."`
- **NO SIGNAL** → `"Both quiet — nothing to do."`
- **NIFTY-ONLY** → `"No Sensex check — Nifty alone, be careful."`
- Overlays: if `vix_regime==spiking` append `" · VIX spiking, trend risk."`; if `expiry` append `" · Expiry pin {max_pain}."`
- **Why:** one line = one decision; ends by pointing to Hi's own R1 trigger, never "just buy".

**5.2 WAIT reason.** Print the specific unmet condition next to WAIT:
- Sensex flat → `"(Sensex quiet)"` · divergent → `"(they disagree)"` · `|dist|>FAR` → `"(too far — {|dist|}pts)"` · unconfirmed → `"(1 read only)"`.
- **Why:** tells Hi whether to keep watching or move on, instead of a blank WAIT.

**5.3 VIX regime line** (VIX already in the Fyers response):
- `vix < 14` → `"VIX {v} — calm, fade-friendly"` · `14–20` → `"VIX {v} — normal"` · `vix > 20 OR intraday VIX jump > 5%` → `"VIX {v} — spiking, trend risk, don't fade"`.
- Thresholds tunable (ties to R11 VIX>22 no-trade, R22 IV crush). **Why:** stops fading into a trend day, one glance.

**5.4 Proximity word** `prox` (pairs with distance):
- Nifty: `|dist| ≤ 25` → **AT WALL** · `25–60` → **APPROACHING** · `>60` → **FAR**. Sensex ≈ ×3.2 (AT ≤75, APPROACHING 75–190, FAR >190). Tunable.
- **Why:** a fade only exists near the wall — this says when to actually watch.

**5.5 Warm-up notice** (early session):
- If no snapshot yet exists that is ≥ `window` old (i.e. Δ% not computable) → `"Collecting data — first read in ~{minutes_left} min"`.
- **Why:** right after 9:15 the Δ% is blank; this stops Hi thinking it's broken.

**5.6 Expiry pin note** (only when `DTE==0` on either index):
- → `"Pin target {max_pain} (max-pain) — price likely drawn here today."`
- **Why:** R33 says max-pain dominates expiry; makes it actionable on Tue/Thu.

**Do NOT add:** RSI/sentiment/extra scores as comments — noise. Plainer words + the one action line beat more labels.

---
*🐍 COBRA · comment/logic map · near-expiry = higher wall trust (fade), no false breakout on expiry unwind · one action line = one decision · all thresholds tunable after live days.*
