# Phase 5 DDL

**Phase 5 needs NO required DDL.** The `verdicts` table already exists from
Phase 4 (see `db/schema.sql`), and `market/verdict_store.py` writes to and reads
from it as-is. The running service does **not** depend on anything in this file.

The two snippets below are **OPTIONAL** and **idempotent** — you MAY paste them
into the Supabase SQL editor if you want the manual-backtest conveniences they
add, but skipping them changes nothing about how COBRA runs.

## (a) OPTIONAL: manual backtest labeling columns

Adds two nullable columns so you can hand-label whether each verdict turned out
right when judging the rule per weekday bucket (spec §3/§11). Populated by hand
later — never by the running service.

```sql
-- OPTIONAL: manual backtest labeling columns
alter table verdicts add column if not exists outcome text;
alter table verdicts add column if not exists notes text;
```

## (b) OPTIONAL: at-a-glance weekday buckets

Creates a convenience view that pre-aggregates verdict counts per weekday — the
same shape `bucket_counts()` computes, but queryable directly in the SQL editor.

```sql
-- OPTIONAL: at-a-glance weekday buckets
create or replace view verdict_weekday_buckets as
select weekday, verdict, count(*) as n
from verdicts
group by weekday, verdict
order by weekday, verdict;
```
