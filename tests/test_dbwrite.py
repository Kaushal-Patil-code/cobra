"""v3 item 3 + strength logging — DB write-path unit tests (fake cursor).

The pure logic (select_wall, pairing, dominance) is tested elsewhere; these drive
the SQL boundary that the mocked-DB suite otherwise never exercises: the incumbent
key-tuple, the UPSERT value order + rowcount interpretation, and the graceful
degrade when the strength columns aren't migrated yet.
"""
from datetime import date

import psycopg

from market import ladders as ladders_mod
from market import selection as sel
from market import verdict_store as vs
from schemas.market import Ladder
from schemas.strikes import WallSelection

TD = date(2026, 6, 19)
EXP = date(2026, 6, 23)


class FakeCursor:
    def __init__(self, fetch=None, rowcounts=None):
        self.fetch = fetch or []
        self._rowcounts = list(rowcounts or [])
        self.calls = []          # [(sql, params)]
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.rowcount = self._rowcounts.pop(0) if self._rowcounts else 0

    def executemany(self, sql, seq):
        self.calls.append((sql, list(seq)))

    def fetchall(self):
        return self.fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, module, cur):
    monkeypatch.setattr(module, "get_conn", lambda: FakeConn(cur))


def test_read_incumbent_walls_keys_by_side_index_expiry(monkeypatch):
    cur = FakeCursor(fetch=[
        {"side": "CAP", "index_name": "NIFTY", "expiry": EXP, "wall_strike": 24400},
        {"side": "FLOOR", "index_name": "SENSEX", "expiry": EXP, "wall_strike": 77200},
    ])
    _patch(monkeypatch, sel, cur)
    got = sel.read_incumbent_walls(TD)
    # exact key tuple — a wrong key would silently disable stickiness (walls flip)
    assert got == {("CAP", "NIFTY", EXP): 24400, ("FLOOR", "SENSEX", EXP): 77200}


def test_upsert_wall_passes_columns_in_order_and_returns_rowcount(monkeypatch):
    cur = FakeCursor(rowcounts=[1])
    wsel = WallSelection(side="CAP", index_name="NIFTY", option_type="CE", expiry=EXP,
                         wall_strike=24400, wall_oi=900,
                         monitored=[24350, 24400, 24450], interval=50)
    changed = sel._upsert_wall(cur, TD, "CAP", wsel)
    assert changed == 1                                   # rowcount passed through
    _sql, params = cur.calls[0]
    assert params == (TD, "CAP", "NIFTY", "CE", EXP, 24400,
                      [24350, 24400, 24450], 900)         # exact INSERT column order


def test_upsert_wall_sticky_hold_returns_zero(monkeypatch):
    cur = FakeCursor(rowcounts=[0])                       # WHERE wall unchanged → no update
    wsel = WallSelection(side="CAP", index_name="NIFTY", option_type="CE", expiry=EXP,
                         wall_strike=24400, wall_oi=900,
                         monitored=[24350, 24400, 24450], interval=50)
    assert sel._upsert_wall(cur, TD, "CAP", wsel) == 0


def test_upsert_ladders_counts_only_changed_rows(monkeypatch):
    lad = Ladder(index_name="NIFTY", expiry=EXP, spot_at_lock=24300.0, atm=24300,
                 interval=50, strikes=[24450, 24400, 24350, 24300, 24250, 24200, 24150, 24100])
    cur = FakeCursor(rowcounts=[0, 1])                    # first unchanged, second shifted
    _patch(monkeypatch, ladders_mod, cur)
    assert ladders_mod.upsert_ladders(TD, [lad, lad]) == 1


def test_insert_verdicts_degrades_when_strength_columns_missing(monkeypatch):
    calls = []

    class DegradingCursor(FakeCursor):
        def executemany(self, sql, seq):
            calls.append(sql)
            if "nifty_strength" in sql:                  # full INSERT before migration
                raise psycopg.errors.UndefinedColumn("column does not exist")

    _patch(monkeypatch, vs, DegradingCursor())
    row = {"ts": None, "trading_date": TD, "weekday": "Fri", "window_minutes": 15,
           "side": "CAP", "option_type": "CE", "verdict": "CAP HOLDING",
           "conviction": "HIGH", "nifty_strength": 4, "nifty_dominance": 3.33}
    n = vs.insert_verdicts([row])
    assert n == 1
    assert any("nifty_strength" in s for s in calls)                       # tried the full insert
    assert any("nifty_strength" not in s and "INSERT INTO verdicts" in s for s in calls)  # fell back
