# Phase 4 — DDL to run manually

Phase 4 adds exactly one new table: `verdicts`, the append-only backtest log
(one row per zone per tick — spec §1, §6.2). As with every prior phase, run the
SQL below by hand in the Supabase dashboard → SQL Editor (or
`psql "$DATABASE_URL" -f ...`). It is already appended to `db/schema.sql`, so a
fresh `schema.sql` run also creates it; this file is just the standalone copy
for an incremental apply on an existing database. Everything uses
`create … if not exists`, so re-running is safe.

No other DDL is needed for Phase 4: the read side (`compute/series.py`) only
queries the existing `snapshots` and `monitored_strikes` tables, both of which
were created in earlier phases.

```sql
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
    weekday        text        not null,                       -- 'Mon'..'Fri' for per-weekday bucketing (spec §3)
    window_minutes integer     not null,                       -- 15 | 30
    role           text        not null check (role in ('RESISTANCE', 'SUPPORT')),
    option_type    text        not null check (option_type in ('CE', 'PE')),
    verdict        text        not null,                       -- e.g. 'CAP HOLDING','BREAKOUT','DIVERGENCE'
    conviction     text        not null,                       -- HIGH|MODERATE|LOW|UNCONFIRMED|NONE
    meaning        text,
    nifty_sig      text,
    sensex_sig     text,
    dte_n          integer,
    dte_s          integer,
    suppressed     boolean     not null default false,
    expiry_label   text,
    created_at     timestamptz not null default now()
);

-- "Replay this day's verdicts, newest first" — the backtest day-scan.
create index if not exists verdicts_date_idx on verdicts (trading_date, ts desc);

-- Per-weekday slice for the spec §3 / §11 weekday-bucketed accuracy stats.
create index if not exists verdicts_weekday_idx on verdicts (weekday);
```
