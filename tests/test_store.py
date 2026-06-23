"""Unit test for snapshot persistence — mocks the DB connection (no real DB)."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from market.store import store_snapshot
from schemas.market import ChainSnapshot, StrikeOI

TS = datetime(2026, 6, 18, 16, 0, tzinfo=timezone.utc)


def _chain(strikes):
    return ChainSnapshot(
        index_name="NIFTY", fetched_at=TS, expiry=date(2026, 6, 23),
        vix=12.67, call_oi=1, put_oi=2, strikes=strikes,
    )


def _mock_conn():
    """Return (cursor_mock, contextmanager replacing get_conn)."""
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False

    @contextmanager
    def fake_get_conn():
        yield conn

    return cur, fake_get_conn


def test_store_inserts_rows_with_shared_ts():
    chain = _chain([
        StrikeOI(index_name="NIFTY", option_type="CE", strike=24200,
                 expiry=date(2026, 6, 23), oi=100, ltp=5.0, volume=10, prev_oi=90, oichp=11.1),
        StrikeOI(index_name="NIFTY", option_type="PE", strike=24200,
                 expiry=date(2026, 6, 23), oi=200, ltp=4.0, volume=20, prev_oi=180, oichp=11.1),
    ])
    cur, fake = _mock_conn()
    with patch("market.store.get_conn", fake):
        n = store_snapshot(chain)

    assert n == 2
    cur.executemany.assert_called_once()
    sql, rows = cur.executemany.call_args[0]
    assert "INSERT INTO snapshots" in sql
    assert len(rows) == 2
    # column order: ts, index_name, option_type, strike, expiry, oi, ...
    assert all(r[0] == TS for r in rows)             # shared fetch ts
    assert {r[2] for r in rows} == {"CE", "PE"}
    assert rows[0][1] == "NIFTY" and rows[0][3] == 24200


def test_store_empty_is_noop():
    cur, fake = _mock_conn()
    with patch("market.store.get_conn", fake):
        n = store_snapshot(_chain([]))
    assert n == 0
    cur.executemany.assert_not_called()
