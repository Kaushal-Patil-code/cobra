"""Instrument registry loader — mocks the DB (no real connection)."""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import market.instruments as inst_mod

ROWS = [
    {"name": "NIFTY", "symbol": "NSE:NIFTY50-INDEX", "strike_interval": 50,
     "lot_size": 75, "expiry_weekday": "TUE", "price_mult": 1.0, "is_active": True},
    {"name": "SENSEX", "symbol": "BSE:SENSEX-INDEX", "strike_interval": 100,
     "lot_size": 20, "expiry_weekday": "THU", "price_mult": 3.20, "is_active": True},
]


def _fake_get_conn(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False

    @contextmanager
    def fake():
        yield conn

    return fake


def test_load_and_cache():
    inst_mod._cache = None
    try:
        with patch("market.instruments.get_conn", _fake_get_conn(ROWS)):
            insts = inst_mod.all_instruments(refresh=True)
        assert set(insts) == {"NIFTY", "SENSEX"}
        assert insts["SENSEX"].strike_interval == 100
        assert insts["SENSEX"].price_mult == 3.20
        assert inst_mod.get_instrument("NIFTY").lot_size == 75
    finally:
        inst_mod._cache = None
