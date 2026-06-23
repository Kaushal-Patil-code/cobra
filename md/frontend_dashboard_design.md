# COBRA — Next.js Dashboard Design (Live + Historical)

A suggested dashboard for the v3 backend. Two routes, both pure read-only views
over the existing API. (`frontend-next/` already scaffolds this — use it as the
starting point.)

## Data sources
- **Live:** `GET /api/state?window=15|30` — poll every ~30s.
- **History:** `GET /api/history?start=&end=&side=CAP|FLOOR|ALL` — fetch once / on filter change.
- Both fall back to bundled sample fixtures when the backend is down (flag the source).

Key `/state` fields: `expiry` (pin flags + label), `metrics[]` (spot/atm/max_pain/pcr),
`range_broken[]`, `sides[]` (one per CAP/FLOOR: verdict, conviction, tag, wall_strike,
`nifty`/`sensex` wall signals with wall + neighbors).

---

## Route `/` — Live dashboard

```
┌─ Header ──────────────────────────────────────────────────────────────┐
│ COBRA   Tue · 2026-06-23   updated 15:21:33   ● LIVE   [15m | 30m]      │
├─ Expiry/Pin banner ───────────────────────────────────────────────────┤
│ NIFTY DTE 0 [PIN]   SENSEX DTE 2     EXPIRY/PIN — settlement, no false  │
│                                       breakouts                          │
├─ RANGE BROKEN (only if present) ──────────────────────────────────────┤
│ ⚠ NIFTY spot left the ladder · likely trend day, stop fading            │
├─ Metrics strip ───────────────────────────────────────────────────────┤
│  NIFTY  spot 23,845  ATM 23,850  Max-pain 23,800  PCR 1.12              │
│  SENSEX spot 76,310  ATM 76,300  Max-pain 76,500  PCR 0.94             │
├─ CAP card (resistance / CE) ──────────────────────────────────────────┤
│  CAP · CE · wall 23,850                       [EXPIRY/PIN]              │
│  ┌─ verdict banner ─────────────────────────────────────────────┐     │
│  │  DIVERGENCE              LOW                                    │     │
│  │  Fake-out risk. Stand down / tiny.                             │     │
│  └───────────────────────────────────────────────────────────────┘     │
│  Index | Wall | Δ% (window) | Dir | Mag | Streak | Trend               │
│  NIFTY  23850   +6.2% signal  ↑    signal  2       —        [PIN]        │
│   nbr   23800   +1.1% noise   –    noise   0       —                     │
│  SENSEX 76500   -7.4% mild    ↓    mild    2       —                     │
│  (migration flag line if a neighbor overtook the wall)                  │
├─ FLOOR card (support / PE) ───────────────────────────────────────────┤
│  … same shape …                                                         │
└────────────────────────────────────────────────────────────────────────┘
```

**Sections (top → bottom):**
1. **Header** — date/weekday, last-updated, live/sample indicator, **15/30-min window toggle** (re-requests `/state?window=`).
2. **Expiry/Pin banner** — both DTEs, a `PIN` chip on any 0-DTE index, the `expiry.label`; amber when pinned, the "NIFTY-ONLY" note when Sensex data is missing.
3. **RANGE BROKEN alert** — render only when `range_broken` is non-empty (spot left the ladder → trend day).
4. **Metrics strip** — per index: spot, ATM, **max-pain (highlighted — the pin magnet)**, PCR. Most useful on the 0-DTE index.
5. **One card per side (CAP then FLOOR)** — big color-coded **verdict + conviction** banner with the trader-facing meaning, an `EXPIRY/PIN` tag when set, the wall strike, then the **dual-index table** (NIFTY vs SENSEX: wall row prominent + 2 neighbors, with Δ%, direction, magnitude, streak, trend). Surface the migration flag and a `PIN` chip per index.

**Empty/edge states:** `note` ("no walls locked yet"), insufficient history early in the session (cells show `—`), sample-data badge when offline.

---

## Route `/history` — Backtest review (ALL tables)

Show **every** data table, not just verdicts. A shared date-range picker at the top
drives all tabs; each tab is a dense, newest-first, paginated table with its own
filters. (The only table NOT shown is `fyers_tokens` — it holds live bearer tokens
and must never be exposed.)

```
┌─ Date range  [ 2026-06-09 → 2026-06-23 ]      source ● LIVE ───────────┐
├─ Tabs:  [Verdicts] [Snapshots] [Metrics] [Ladders] [Walls] [Instruments]│
└────────────────────────────────────────────────────────────────────────┘
```

### Tab 1 — Verdicts  (table `verdicts`)  ← the headline
```
┌─ Weekday buckets ─────────────────────────────────────────────────────┐
│  Mon  Tue  Wed  Thu  Fri   one card each: total + stacked by_verdict    │
├─ Verdict log ─────────────────────────────────────────────────────────┤
│  142 rows                                  [All | CAP | FLOOR]           │
│  Time Date Day Side Wall Verdict     Conv Tag      DTE n/s NIFTY SENSEX Out│
│  15:21 06-23 Tue CAP 23850 DIVERGENCE LOW EXPIRY/PIN 0/2  bld/… unw/…  —  │
└────────────────────────────────────────────────────────────────────────┘
```
Weekday buckets (`weekday_buckets[]`, the per-weekday judging) + the verdict log
(`records[]`), filterable by **side** (CAP/FLOOR/ALL). Show `tag` (EXPIRY/PIN),
`wall_strike`, and the manual `outcome`/`notes` if present.

### Tab 2 — Snapshots  (table `snapshots`) — raw OI time series
```
 Time   Index  Type Strike  Expiry     OI        LTP    Vol    PrevOI  oichp
 15:21  NIFTY  CE   23850   06-23   1,250,000   42.5  98,400  1.11M   +12.6%
```
Filters: index, option_type, strike, expiry. Default to one strike's series so it's
readable; offer a tiny **OI-over-time sparkline** per strike. This is the rawest layer.

### Tab 3 — Metrics  (table `index_metrics`) — max-pain / PCR over time
```
 Time   Index   Spot     ATM     Max-pain   PCR    CallOI    PutOI
 15:21  NIFTY  23,845  23,850   23,800     1.12   118.5M    132.2M
```
Filter by index. Plot **max-pain vs spot** and **PCR** as lines over the day — most
telling on the 0-DTE index (does the pin magnet predict the close?).

### Tab 4 — Ladders  (table `ladders`) — the locked ladder per day/index
```
 Date    Index   Expiry   Spot@lock  ATM     Strikes (8 rungs)        Locked
 06-23   NIFTY   06-23    23,851    23,850  24000…23650             15:06
```
One row per `(trading_date, index, expiry)`. Shows what the session anchored on.

### Tab 5 — Walls  (table `monitored_strikes`) — locked CAP/FLOOR walls
```
 Date    Side   Index   OptType  Expiry   Wall     Monitored          OI@lock
 06-23   CAP    NIFTY   CE       06-23    23,850   23800/23850/23900  1.25M
```
Filter by side. The walls each session locked + their monitored neighbors.

### Tab 6 — Instruments  (table `instruments`) — static reference (optional)
`name, symbol, strike_interval, lot_size, expiry_weekday, price_mult, is_active` —
not time-series; a small read-only reference card.

> **Excluded:** `fyers_tokens` (secrets) and `zones` (deprecated/empty in v3).

---

## API endpoints (with pagination)

Only `GET /state`, `GET /history`, `GET /tick` exist today. Add the read-only,
date-ranged, **paginated** endpoints below (all DB-backed → 503 when no DSN, 400 on
bad args, same patterns as `app/api/dashboard.py`). All sit under the `/api/*` proxy.

### Live (polling) endpoint — NOT paginated
The Live dashboard's heartbeat. It returns a single point-in-time state, so there's
no cursor — the client just re-requests it on an interval.

| Endpoint | Returns | Poll |
|---|---|---|
| `GET /api/state?window=15\|30` | the latest `VerdictState` — `expiry` (pin/label), `metrics[]` (spot/atm/max-pain/PCR), `range_broken[]`, `sides[]` (CAP/FLOOR walls + neighbors), `ts`, `weekday`, `note` | **every ~30s** |

- `window` selects the 15/30-min Δ% window (anything else → default 15).
- 200 normally; 503 if the DB is down. No `start`/`end`/`cursor`.
- Client: poll on a timer, show "updated <ts>", flag **stale** if no refresh in ~2.5 cycles, and re-poll immediately when the window toggle changes.
- Related (not for the dashboard to poll): `GET /api/tick` is cron/scheduler-driven (runs the capture cycle, market-hours + de-dup gated); `GET /api/health` for liveness.

### Pagination contract (keyset / cursor — identical on every list endpoint)
Time-series tables are large and append-only, and **many rows share one `ts`** (a
tick writes ~42 snapshot rows at the same instant), so use **keyset pagination on
`(ts, id)`** — not `OFFSET` (which drifts as new rows arrive and slows down deep in
the table).

- Query params: `limit` (default 100, **max 1000**), `cursor` (opaque, optional), `order` (`desc` default | `asc`), plus the per-endpoint date range & filters.
- The server fetches `limit + 1` rows to learn whether a next page exists.
- Response envelope:
  ```json
  {
    "items": [ /* rows */ ],
    "page": { "limit": 100, "count": 100, "has_more": true,
              "next_cursor": "eyJ0cyI6Ii4uLiIsImlkIjoiLi4uIn0=" }
  }
  ```
- `next_cursor` = base64(`{"ts": <last.ts>, "id": <last.id>}`) of the last item; pass it back as `?cursor=`. SQL: `WHERE (ts, id) < (%s, %s) ORDER BY ts DESC, id DESC LIMIT %s+1`. On the last page `has_more=false`, `next_cursor=null`.

### Endpoints
| Endpoint | Table | Filters (besides `start`/`end`/`limit`/`cursor`/`order`) | Keyset |
|---|---|---|---|
| `GET /api/history` | verdicts | `side` (CAP/FLOOR/ALL) | `ts, id` — **also returns `weekday_buckets` (aggregate over the FULL range, not paginated) alongside the first page of `records`** |
| `GET /api/snapshots` | snapshots | `index`, `option_type`, `strike`, `expiry` | `ts, id` |
| `GET /api/metrics` | index_metrics | `index` | `ts, id` |
| `GET /api/ladders` | ladders | `index` | `locked_at, id` (range on `trading_date`) |
| `GET /api/walls` | monitored_strikes | `side`, `index` | `locked_at, id` (range on `trading_date`) |
| `GET /api/instruments` | instruments | — | none — tiny static table, returns all (no pagination) |

- **Date range:** `start`/`end` inclusive ISO dates; default = last 14 days (match `/history`). snapshots/metrics filter on `ts::date`; verdicts/ladders/walls on `trading_date`.
- **Allowlist + safety:** a generic `GET /api/history/<table>` is fine too — but gate `<table>` against a hardcoded allowlist; **never** serve `fyers_tokens` (secrets) or `zones` (deprecated). 404 unknown tables.
- **Keep payloads sane:** `/api/snapshots` should default to (or require) one `index`+`option_type`+`strike` so it returns a single readable series instead of the whole chain × every tick.

### Example
```
GET /api/snapshots?index=NIFTY&option_type=CE&strike=23850&start=2026-06-23&limit=200
→ { "items": [ {ts, oi, ltp, volume, prev_oi, oichp}, … 200 ],
    "page": { "limit":200, "count":200, "has_more":true, "next_cursor":"eyJ0cyI6…" } }

GET /api/snapshots?index=NIFTY&option_type=CE&strike=23850&cursor=eyJ0cyI6…   # next page
```

Frontend: each history tab holds a `cursor` in state, loads the next page on scroll
or a "Load more" button, and stops when `has_more` is false. `count` + `has_more`
drive the row-count and "Load more" affordances; the shared date-range picker resets
all cursors.

---

## Component tree (App Router)
```
app/
  page.js            → Live: poll /state, window toggle
  history/page.js    → History: fetch /history
  lib/api.js         → fetchState / fetchHistory (+ sample fallback)
  components/
    ExpiryBanner     · MetricsPanel · SideCard → DualIndexTable
    WindowToggle · StatusIndicator · Nav · HistoryView
```

## Conventions
- **Verdict colors:** HOLDING → green · BREAKOUT/BREAKDOWN → red · DIVERGENCE → amber · PARTIAL → muted · NO SIGNAL → grey.
- **Conviction chip:** HIGH / MODERATE / LOW / UNCONFIRMED / NONE.
- **Direction:** ↑ building (green) · ↓ unwinding (red) · – flat.
- **Live ≠ signal generator** — this confirms a fade; keep it calm and readable, not a trading terminal.
- **Polling:** live ~30s; mark "stale" if no refresh in ~2.5 cycles. History is static (no polling).
- **No manual input** — v3 is spot-anchored; there is no zone/strike form.

## Optional additions
- A small **ladder view** per index (8 rungs, CE+PE OI, ATM + wall highlighted) — `/state` carries the wall + neighbors; the full 8 rungs would need a small `/ladder` endpoint or extending `/state`.
- A **max-pain vs spot** mini-gauge on expiry days (pin magnet).
- A **verdict-flip timeline** sparkline per side from `/history`.
