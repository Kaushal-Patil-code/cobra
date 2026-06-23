-- COBRA v3 migration — spot-anchored ladder + EXPIRY/PIN + max-pain/PCR.
-- ADDITIVE & data-preserving: renames role→side (backfilling RESISTANCE→CAP,
-- SUPPORT→FLOOR), adds columns, and creates the new ladders / index_metrics
-- tables. Idempotent — safe to run more than once. Existing snapshots & verdicts
-- rows are kept.
--
--   psql "$DATABASE_URL" -f db/migrations/v3_ladder.sql

begin;

-- ── monitored_strikes: role → side (CAP/FLOOR) ──────────────────────────────
do $$ begin
    if exists (select 1 from information_schema.columns
               where table_name = 'monitored_strikes' and column_name = 'role') then
        alter table monitored_strikes rename column role to side;
    end if;
end $$;

-- Old value-domain check must go before we backfill the new values.
alter table monitored_strikes drop constraint if exists monitored_strikes_role_check;

update monitored_strikes set side = 'CAP'   where side = 'RESISTANCE';
update monitored_strikes set side = 'FLOOR' where side = 'SUPPORT';

do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'monitored_strikes_side_check') then
        alter table monitored_strikes
            add constraint monitored_strikes_side_check check (side in ('CAP', 'FLOOR'));
    end if;
end $$;
-- (The unique constraint already covers the renamed column — its tuple is now
--  (trading_date, side, index_name, expiry); no recreation needed.)


-- ── verdicts: role → side, + wall_strike, + tag ─────────────────────────────
do $$ begin
    if exists (select 1 from information_schema.columns
               where table_name = 'verdicts' and column_name = 'role') then
        alter table verdicts rename column role to side;
    end if;
end $$;

alter table verdicts drop constraint if exists verdicts_role_check;

update verdicts set side = 'CAP'   where side = 'RESISTANCE';
update verdicts set side = 'FLOOR' where side = 'SUPPORT';

do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'verdicts_side_check') then
        alter table verdicts
            add constraint verdicts_side_check check (side in ('CAP', 'FLOOR'));
    end if;
end $$;

alter table verdicts add column if not exists wall_strike integer;
alter table verdicts add column if not exists tag         text;     -- 'EXPIRY/PIN'


-- ── ladders: the locked spot-anchored ladder per index/expiry (§3) ──────────
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


-- ── index_metrics: per-index max-pain + PCR per tick (§6) ────────────────────
create table if not exists index_metrics (
    id           uuid          primary key default gen_random_uuid(),
    ts           timestamptz   not null,                 -- matches the tick / snapshots ts
    trading_date date          not null,
    index_name   text          not null check (index_name in ('NIFTY', 'SENSEX')),
    expiry       date          not null,
    spot         numeric(14, 2),
    atm          integer,
    max_pain     integer,                               -- argmin writer payout (the pin)
    pcr          numeric(10, 4),                        -- putOi / callOi
    call_oi      bigint,
    put_oi       bigint
);

create index if not exists index_metrics_lookup_idx
    on index_metrics (trading_date, index_name, ts desc);


-- ── zones: DEPRECATED in v3 (spot-anchored ladder replaces typed zones). ─────
-- Left in place so historical rows aren't lost; the running service no longer
-- reads or writes it. Drop manually if you want it gone.

commit;
