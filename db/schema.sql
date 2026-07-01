-- COBRA schema — run once against your Supabase Postgres.
-- Supabase dashboard → SQL Editor → paste & run, or:
--   psql "$DATABASE_URL" -f db/schema.sql

-- gen_random_uuid() for UUID primary keys (built into PG13+; this is a safe
-- no-op on Supabase, kept for portability to older Postgres).
create extension if not exists "pgcrypto";

-- Fyers auto-login token cache (one row per Fyers client_id).
-- auth.save_token() upserts here; auth.load_token() reads it.
-- Credentials stay in .env; only the issued access/refresh tokens live here.
create table if not exists fyers_tokens (
    client_id     text        primary key,
    access_token  text        not null,
    refresh_token text,
    updated_at    timestamptz not null default now()
);

-- Security: these are live bearer tokens. Enable RLS with NO policies so the
-- anon / authenticated API roles (PostgREST) cannot read the table. The app
-- connects with the postgres/service role (direct psycopg), which BYPASSES RLS,
-- so save_token/load_token are unaffected. Never expose this via the public API.
alter table fyers_tokens enable row level security;


-- Phase 1 — per-strike OI snapshots (append-only time series).
-- One row per monitored strike per fetch; all rows of a fetch share `ts`.
-- Feeds the intraday-refresh validation (Phase 1) and the Δ%-over-window
-- verdict math (Phase 4). Maps to the spec's snapshots(ts, index, type, strike,
-- oi); `index`/`type` are renamed to avoid awkward SQL identifiers.
-- Idempotency: a double-fired cron /tick writes two rows with distinct `ts`
-- (harmless to Δ% — just a denser series). Real de-dup belongs at the /tick
-- layer in Phase 6 (market-minute bucket), not a unique constraint here.
create table if not exists snapshots (
    id          uuid        primary key default gen_random_uuid(),
    ts          timestamptz not null default now(),       -- fetch time (shared per fetch)
    index_name  text        not null check (index_name in ('NIFTY', 'SENSEX')),
    option_type text        not null check (option_type in ('CE', 'PE')),
    strike      integer     not null,
    expiry      date        not null,                      -- chain expiry this strike belongs to
    oi          bigint      not null,                      -- open interest (contracts)
    ltp         numeric(14, 2),                            -- last traded premium
    volume      bigint,
    prev_oi     bigint,                                    -- Fyers prev-day OI (reference)
    oichp       numeric(14, 4)                             -- Fyers OI %chg vs prev day (reference)
);

-- Time-series lookup: "OI history for this exact strike, newest first" —
-- backs value_at_or_before(now - window) in the Δ% computation.
create index if not exists snapshots_series_idx
    on snapshots (index_name, option_type, strike, expiry, ts desc);

-- General recent-rows scan.
create index if not exists snapshots_ts_idx on snapshots (ts desc);


-- Instrument registry — the two index underlyings + their fixed trading params
-- (spec §2). This is COBRA's "symbol" table. Static config: load it once at
-- startup and cache; don't query it per fetch.
create table if not exists instruments (
    name            text          primary key,            -- 'NIFTY' | 'SENSEX'
    symbol          text          not null unique,        -- Fyers underlying, e.g. 'NSE:NIFTY50-INDEX'
    strike_interval integer       not null,               -- 50 | 100
    lot_size        integer       not null,               -- 75 | 20  (spec §2)
    expiry_weekday  text          not null,               -- 'TUE' | 'THU'
    price_mult      numeric(8, 4) not null default 1.0,   -- × Nifty level (Sensex ≈ 3.20)
    is_active       boolean       not null default true,
    created_at      timestamptz   not null default now(),
    updated_at      timestamptz   not null default now()
);

-- Seed the two indices. Idempotent: re-running won't duplicate. Use
-- `do update` instead of `do nothing` if you want it to re-sync to these values.
insert into instruments (name, symbol, strike_interval, lot_size, expiry_weekday, price_mult)
values
    ('NIFTY',  'NSE:NIFTY50-INDEX',  50, 75, 'TUE', 1.00),
    ('SENSEX', 'BSE:SENSEX-INDEX',  100, 20, 'THU', 3.20)
on conflict (name) do nothing;


-- DEPRECATED in v3 — the spot-anchored ladder replaced typed price zones, so the
-- running service no longer reads/writes this table. Kept only for any historical
-- rows; safe to drop. (No manual zone input in v3 — §3 / §A.)
create table if not exists zones (
    id           uuid           primary key default gen_random_uuid(),
    trading_date date           not null,
    role         text           not null,
    low          numeric(12, 2) not null,
    high         numeric(12, 2) not null,
    created_at   timestamptz    not null default now(),
    unique (trading_date, role)
);

-- v3 §3 — the locked spot-anchored ladder per index per expiry. Built once per
-- session from live spot (ATM + 3 up + 4 down); both CE & PE are tracked on every
-- rung. Keyed by expiry so a rolled expiry locks a fresh ladder. RANGE BROKEN =
-- live spot has left [min(strikes), max(strikes)].
create table if not exists ladders (
    id           uuid           primary key default gen_random_uuid(),
    trading_date date           not null,
    index_name   text           not null check (index_name in ('NIFTY', 'SENSEX')),
    expiry       date           not null,
    spot_at_lock numeric(14, 2) not null,
    atm          integer        not null,
    interval     integer        not null,
    strikes      integer[]      not null,           -- 8 rungs, ATM+3i … ATM-4i
    locked_at    timestamptz    not null default now(),
    unique (trading_date, index_name, expiry)
);

-- v3 §3 — locked walls + monitored neighbors, per index per SIDE per expiry.
-- CAP = highest CE OI on the ladder, FLOOR = highest PE OI. Keyed by `expiry`:
-- when the nearest expiry rolls, there's no row for the new expiry → the system
-- selects + locks fresh. Lock once at session start; OI migration to a neighbor
-- is FLAGGED at runtime, never silently re-picked (§3).
create table if not exists monitored_strikes (
    id              uuid        primary key default gen_random_uuid(),
    trading_date    date        not null,
    side            text        not null check (side in ('CAP', 'FLOOR')),
    index_name      text        not null check (index_name in ('NIFTY', 'SENSEX')),
    option_type     text        not null check (option_type in ('CE', 'PE')),
    expiry          date        not null,
    wall_strike     integer     not null,
    monitored       integer[]   not null,    -- [wall-interval, wall, wall+interval]
    wall_oi_at_lock bigint,
    locked_at       timestamptz not null default now(),
    unique (trading_date, side, index_name, expiry)
);

create index if not exists monitored_lookup_idx
    on monitored_strikes (trading_date, index_name);

-- v3 §6 — per-index max-pain + PCR, appended every tick (the pin magnet; most
-- useful on the 0-DTE index). Computed from the chain we already pull — no extra
-- API call. Latest row per index backs /state; the per-tick history backs backtest.
create table if not exists index_metrics (
    id           uuid          primary key default gen_random_uuid(),
    ts           timestamptz   not null,
    trading_date date          not null,
    index_name   text          not null check (index_name in ('NIFTY', 'SENSEX')),
    expiry       date          not null,
    spot         numeric(14, 2),
    atm          integer,
    max_pain     integer,
    pcr          numeric(10, 4),
    call_oi      bigint,
    put_oi       bigint
);

create index if not exists index_metrics_lookup_idx
    on index_metrics (trading_date, index_name, ts desc);


-- Phase 4 / Phase 5 — verdict log (spec §1, §6.2): the backtest dataset.
-- Append-only; ONE row per zone (RESISTANCE/SUPPORT) per tick. Each row freezes
-- the dual-index verdict the dashboard showed at that instant, plus the context
-- needed to slice it later: `weekday` for per-weekday bucketing and `dte_n`/
-- `dte_s` for DTE bucketing (spec §3, §11). Phase 5 backtesting reads exclusively
-- from here — it never recomputes from snapshots. Mirrors schemas/verdict.py
-- (ZoneVerdict): verdict/conviction/meaning are the trader-facing call; the
-- *_sig strings are the compact per-index state. `suppressed` records that the
-- cross-check was paused (a 0-DTE index → NIFTY-ONLY fallback, spec §3).
create table if not exists verdicts (
    id             uuid        primary key default gen_random_uuid(),
    ts             timestamptz not null,                       -- verdict time (matches the tick)
    trading_date   date        not null,
    weekday        text        not null,                       -- 'Mon'..'Fri' for per-weekday bucketing (§4)
    window_minutes integer     not null,                       -- 15 | 30
    side           text        not null check (side in ('CAP', 'FLOOR')),
    option_type    text        not null check (option_type in ('CE', 'PE')),
    wall_strike    integer,                                    -- the locked NIFTY (primary) wall
    verdict        text        not null,                       -- e.g. 'CAP HOLDING','BREAKOUT','DIVERGENCE'
    conviction     text        not null,                       -- HIGH|MODERATE|LOW|UNCONFIRMED|NONE
    meaning        text,
    tag            text,                                       -- 'EXPIRY/PIN' when an index is 0-DTE (§4)
    nifty_sig      text,
    sensex_sig     text,
    dte_n          integer,
    dte_s          integer,
    suppressed     boolean     not null default false,         -- NIFTY-ONLY (Sensex data missing)
    expiry_label   text,
    -- Wall STRENGTH (size axis, not change): dominance = wall OI ÷ median of the
    -- other ladder rungs, bucketed 1–5. Logged per index so the cutoffs can be
    -- tuned on real data (§11). CAP rows carry CE-wall strength, FLOOR rows PE.
    nifty_strength   smallint,
    nifty_dominance  numeric(8, 2),
    sensex_strength  smallint,
    sensex_dominance numeric(8, 2),
    created_at     timestamptz not null default now()
);

-- "Replay this day's verdicts, newest first" — the backtest day-scan.
create index if not exists verdicts_date_idx on verdicts (trading_date, ts desc);

-- Per-weekday slice for the §4 / §11 weekday-bucketed accuracy stats.
create index if not exists verdicts_weekday_idx on verdicts (weekday);
