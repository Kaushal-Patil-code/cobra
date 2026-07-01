-- v4 — sticky BROKEN badge: remember the former wall spot cleared, per side/index.
-- Idempotent. Apply BEFORE deploying the v4 code (lock_walls degrades if absent,
-- but the BROKEN badge stays dormant until this runs).
begin;
alter table monitored_strikes add column if not exists broken_level integer;
commit;
