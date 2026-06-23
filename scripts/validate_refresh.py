"""Phase 1 verify gate — does Fyers `oi` actually refresh INTRADAY?

Flagged unknown in spec §2. This compares the two most recent fetches already in
the `snapshots` table.

How to use (DURING market hours, 09:15-15:30 IST):
    uv run python -m market.fetch          # fetch #1
    # ...wait ~3-5 minutes...
    uv run python -m market.fetch          # fetch #2
    uv run python -m scripts.validate_refresh

Per index it reports how many monitored strikes changed OI between the two
snapshots, the largest move, and whether `oi` equals `prev_oi` (a sign the value
is frozen at the previous day's close rather than updating live).
"""
from db.db import close_pool, get_conn

_SQL = """
SELECT n.index_name,
       count(*)                                  AS total,
       count(*) FILTER (WHERE n.oi <> o.oi)       AS changed,
       max(abs(n.oi - o.oi))                      AS max_abs_delta,
       count(*) FILTER (WHERE n.oi = n.prev_oi)   AS eq_prevday
FROM snapshots n
JOIN snapshots o
  ON  n.index_name = o.index_name AND n.option_type = o.option_type
  AND n.strike = o.strike AND n.expiry = o.expiry
WHERE n.ts = %s AND o.ts = %s
GROUP BY n.index_name
ORDER BY n.index_name
"""


def main() -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ts FROM snapshots ORDER BY ts DESC LIMIT 2")
        rows = cur.fetchall()
        if len(rows) < 2:
            print(
                "Need >= 2 fetches. Run `uv run python -m market.fetch` twice, a few "
                "minutes apart, during market hours, then re-run this."
            )
            return
        t_new, t_old = rows[0]["ts"], rows[1]["ts"]
        print(f"comparing {t_old}  ->  {t_new}   (gap {t_new - t_old})\n")
        cur.execute(_SQL, (t_new, t_old))
        for r in cur.fetchall():
            if r["changed"]:
                verdict = "LIVE OK — oi refreshes intraday"
            else:
                verdict = "FROZEN — oi identical across fetches (use depth() fallback)"
            print(
                f"  {r['index_name']}: {r['changed']}/{r['total']} strikes changed | "
                f"max |Δoi|={r['max_abs_delta']} | oi==prev_oi on {r['eq_prevday']} "
                f"-> {verdict}"
            )
    close_pool()


if __name__ == "__main__":
    main()
