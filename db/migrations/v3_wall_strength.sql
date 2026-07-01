-- COBRA v3 — log wall STRENGTH (the size axis) into the verdicts backtest dataset.
-- Additive & idempotent: adds nullable per-index strength (1–5 bucket) + the raw
-- dominance ratio (wall OI ÷ median of the other ladder rungs) so the strength
-- cutoffs can be TUNED ON LOGGED DATA (§11). Existing rows keep NULL. Safe to run
-- more than once.
--
--   psql "$DATABASE_URL" -f db/migrations/v3_wall_strength.sql

begin;

alter table verdicts add column if not exists nifty_strength   smallint;       -- 1–5 CE-wall dominance bucket
alter table verdicts add column if not exists nifty_dominance  numeric(8, 2);  -- wall OI ÷ median of the other ladder rungs
alter table verdicts add column if not exists sensex_strength  smallint;
alter table verdicts add column if not exists sensex_dominance numeric(8, 2);

commit;
