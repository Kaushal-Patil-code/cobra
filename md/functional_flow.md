# COBRA — Functional Flow (after login) + Data Storage

How the service runs once it has a Fyers token, and what lands in the DB.

## 1. After auto-login — the startup sequence
`app/__init__.py` (`_startup_sequence`, runs in a daemon thread per gunicorn worker):

1. `warm_login_once()` — single-flight auto-login (Postgres advisory lock); only one worker logs in and gets the token.
2. `start_scheduler()` — every worker starts an APScheduler job that fires every `tick_interval_minutes` (default 3).
3. **Immediate first tick** — fired right away, but **only on the login-winning worker** (its token is fresh → no second TOTP login).

From then on the app is self-driving — no external cron needed.

## 2. Each tick — `scheduled_tick()` → `run_tick()` (`market/tick.py`)
Every firing passes three gates before doing work:
- **market hours** (09:15–15:30 IST, Mon–Fri) — else skip.
- **advisory lock** (`_TICK_LOCK_KEY`) — only ONE worker ticks per round; the rest skip.
- **market-minute de-dup** — a tick already ran this clock minute → skip.

The winning worker then runs the cycle, in order, from **one shared Fyers pull**:

1. **Fetch** both option chains — `NSE:NIFTY50-INDEX` + `BSE:SENSEX-INDEX` (2 API calls). Parse per-strike OI/LTP/volume, chain totals, nearest expiry, India VIX, and the **live spot** from the underlying row.
2. **Store snapshots** — the whole chain (`market/store.py`).
3. **Compute + store metrics** — max-pain + PCR per index (`compute/metrics.py` → `market/metrics_store.py`), no extra API call.
4. **Lock ladders + walls** — build each index's 8-rung spot-anchored ladder, lock CAP (max CE OI) + FLOOR (max PE OI). Idempotent: locked once per session; later ticks log "already locked".
5. **Compute the verdict** — `build_state()` reads the snapshot series, scores Δ%/direction/streak per strike, assembles the dual-index verdict per side, applies the EXPIRY/PIN guard, attaches metrics + range-broken (`compute/engine.py`).
6. **Persist verdicts** — one row per side (`compute/persist.py` → `market/verdict_store.py`).

`run_tick` never raises — any failure degrades into a summary dict so the scheduler keeps going.

```
login → scheduler → tick (every 3 min, single-flight, market hours):
  fetch 2 chains ──┬─→ store snapshots
                   ├─→ compute + store metrics (max-pain, PCR)
                   ├─→ lock ladders + CAP/FLOOR walls   (once/day, idempotent)
                   └─→ build verdict ─→ persist verdicts
```

## 3. How the data is stored (Supabase Postgres)
All append-only time series share one timestamp per tick.

| Table | Written when | Holds |
|---|---|---|
| `fyers_tokens` | at login | access/refresh token per client (not on disk) |
| `snapshots` | every tick | one row per strike: `ts, index_name, option_type, strike, expiry, oi, ltp, volume, prev_oi, oichp` |
| `index_metrics` | every tick | one row per index: `ts, index_name, expiry, spot, atm, max_pain, pcr, call_oi, put_oi` |
| `ladders` | session start (per expiry) | the locked 8-rung ladder: `index_name, expiry, spot_at_lock, atm, interval, strikes[]` |
| `monitored_strikes` | session start (per expiry) | locked walls: `side (CAP/FLOOR), index_name, option_type, expiry, wall_strike, monitored[]` |
| `verdicts` | every tick | one row per side: `ts, weekday, window_minutes, side, option_type, wall_strike, verdict, conviction, tag, nifty_sig, sensex_sig, dte_n, dte_s, suppressed, expiry_label` — **the backtest dataset** |

- `snapshots` feeds the windowed Δ% (`read_oi_series`); the whole chain is stored so wall-migration + expiry re-lock have history.
- `verdicts` is the hypothesis log — read back by weekday/DTE bucket (`build_history`); EXPIRY/PIN-tagged rows bucket out distorted expiry days.
- Served read-only to the dashboard via `GET /state` (latest) and `GET /history` (range + weekday buckets).
