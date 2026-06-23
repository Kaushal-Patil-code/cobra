"""Keyset-pagination helpers (app/pagination.py) — pure, no DB."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from market.pagination import (
    MAX_LIMIT,
    build_envelope,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    jsonable,
)


def test_clamp_limit():
    assert clamp_limit(None) == 100          # default
    assert clamp_limit("50") == 50
    assert clamp_limit("0") == 1             # floor
    assert clamp_limit("999999") == MAX_LIMIT
    assert clamp_limit("abc") == 100         # bad → default


def test_cursor_roundtrip():
    c = encode_cursor("2026-06-23T15:06:00+05:30", "abc-123")
    assert decode_cursor(c) == ("2026-06-23T15:06:00+05:30", "abc-123")
    assert decode_cursor(None) is None
    assert decode_cursor("") is None


def test_decode_bad_cursor_raises():
    with pytest.raises(ValueError):
        decode_cursor("!!!not-base64!!!")


def test_jsonable_coerces_db_types():
    row = {
        "ts": datetime(2026, 6, 23, 15, 6),
        "d": date(2026, 6, 23),
        "id": UUID(int=1),
        "pcr": Decimal("1.12"),
        "arr": [Decimal("1"), 2],
        "n": None,
        "s": "x",
    }
    j = jsonable(row)
    assert j["ts"].startswith("2026-06-23T") and j["d"] == "2026-06-23"
    assert j["id"] == str(UUID(int=1)) and j["pcr"] == 1.12
    assert j["arr"] == [1.0, 2] and j["n"] is None and j["s"] == "x"


def test_build_envelope_has_more_and_cursor():
    rows = [{"ts": f"t{i:03d}", "id": str(i)} for i in range(101)]  # limit+1
    env = build_envelope(rows, 100, "ts")
    assert env["page"]["count"] == 100 and env["page"]["has_more"] is True
    # next_cursor points at the LAST returned row (index 99), not the extra one
    assert decode_cursor(env["page"]["next_cursor"]) == ("t099", "99")
    assert len(env["items"]) == 100


def test_build_envelope_last_page():
    rows = [{"ts": f"t{i}", "id": str(i)} for i in range(40)]  # < limit
    env = build_envelope(rows, 100, "ts")
    assert env["page"]["has_more"] is False and env["page"]["next_cursor"] is None
    assert env["page"]["count"] == 40
