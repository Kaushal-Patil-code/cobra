"""fetch_chains token self-heal — mocked (no network/DB)."""
from datetime import date, datetime, timezone
from unittest.mock import patch

from market import fetch as fetchmod
from market.chain import TokenRejectedError
from schemas.market import ChainSnapshot, Instrument

TS = datetime(2026, 6, 18, tzinfo=timezone.utc)
INST = {"NIFTY": Instrument(name="NIFTY", symbol="NSE:NIFTY50-INDEX", strike_interval=50,
                            lot_size=75, expiry_weekday="TUE", price_mult=1.0)}
CHAIN = ChainSnapshot(index_name="NIFTY", fetched_at=TS, expiry=date(2026, 6, 23), strikes=[])


def test_fetch_chains_self_heals_on_token_rejection():
    calls = {"n": 0}

    def fake_fetch(client, symbol, strikecount=10):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TokenRejectedError("dead token")
        return {"s": "ok"}

    with patch("market.fetch.all_instruments", return_value=INST), \
         patch("market.fetch.fetch_raw_chain", side_effect=fake_fetch), \
         patch("market.fetch.parse_chain", return_value=CHAIN), \
         patch("market.fetch._relogin", return_value="fresh-client") as m_relogin:
        out = fetchmod.fetch_chains("stale-client")

    assert "NIFTY" in out
    m_relogin.assert_called_once()
    assert calls["n"] == 2  # failed once, retried after re-auth


def test_fetch_chains_reauth_failure_is_graceful():
    def always_dead(client, symbol, strikecount=10):
        raise TokenRejectedError("dead")

    with patch("market.fetch.all_instruments", return_value=INST), \
         patch("market.fetch.fetch_raw_chain", side_effect=always_dead), \
         patch("market.fetch._relogin", side_effect=RuntimeError("login failed")):
        out = fetchmod.fetch_chains("stale-client")  # must NOT raise

    assert out == {}
