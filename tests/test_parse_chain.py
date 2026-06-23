"""Offline parser tests against the captured live fixtures (no network/DB)."""
import json
import os
from datetime import date, datetime, timezone

from market.parse import parse_chain

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
TS = datetime(2026, 6, 18, 16, 0, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


def test_parse_nifty():
    chain = parse_chain(_load("nifty_optionchain.json"), "NIFTY", TS)
    assert chain.index_name == "NIFTY"
    assert chain.expiry == date(2026, 6, 23)          # nearest = 23-06 (Tue)
    # expiry derived from the epoch (format-proof) and the epoch is retained
    assert chain.expiry_epoch and chain.available_expiries
    assert chain.available_expiries[0].date == chain.expiry
    assert chain.available_expiries[0].epoch == chain.expiry_epoch
    assert len(chain.available_expiries) > 1          # full list captured
    assert chain.vix == 12.67
    assert chain.call_oi and chain.put_oi
    assert chain.spot == 24168            # v3: live spot from the underlying row (ltp)
    assert chain.strikes
    # underlying / blank-type row skipped
    assert all(s.option_type in ("CE", "PE") for s in chain.strikes)
    # Nifty strikes are multiples of 50
    assert all(s.strike % 50 == 0 for s in chain.strikes)
    # both sides present, all share the nearest expiry + fetch ts
    assert any(s.option_type == "CE" for s in chain.strikes)
    assert any(s.option_type == "PE" for s in chain.strikes)
    assert all(s.expiry == date(2026, 6, 23) for s in chain.strikes)


def test_parse_sensex():
    chain = parse_chain(_load("sensex_optionchain.json"), "SENSEX", TS)
    assert chain.index_name == "SENSEX"
    assert chain.expiry == date(2026, 6, 18)          # nearest = 18-06 (Thu)
    assert chain.spot == 77409.98                     # v3: live Sensex spot (ltp)
    assert chain.strikes
    assert all(s.option_type in ("CE", "PE") for s in chain.strikes)
    # Sensex strikes are multiples of 100
    assert all(s.strike % 100 == 0 for s in chain.strikes)
