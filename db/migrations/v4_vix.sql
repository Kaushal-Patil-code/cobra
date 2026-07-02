-- COBRA v4 migration — persist India VIX per tick for the §5.3 VIX regime line.
-- ADDITIVE & data-preserving: adds a nullable `vix` column to index_metrics so the
-- dashboard can show "VIX {v} — calm/normal/spiking" and overlay a trend-day warning
-- on the action line. Idempotent — safe to run more than once. Existing rows keep a
-- NULL vix (pre-migration ticks simply have no regime line until the next tick).
--
--   psql "$DATABASE_URL" -f db/migrations/v4_vix.sql

begin;

alter table index_metrics add column if not exists vix numeric(8, 4);

commit;
