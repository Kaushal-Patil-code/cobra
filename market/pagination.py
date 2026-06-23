"""Keyset (cursor) pagination helpers for the history/list endpoints.

Time-series tables (snapshots, index_metrics, verdicts) are large, append-only,
and many rows share one `ts` (a tick writes ~42 snapshot rows at the same instant),
so we paginate by a **keyset on `(key, id)`** — never OFFSET, which drifts as new
rows arrive and slows down deep in the table.

The cursor is an opaque base64 token carrying the last row's `(key, id)`; the next
page asks for rows ordered after it. `id` (the uuid PK) is the deterministic
tiebreaker for rows that share a `key` value.
"""
from __future__ import annotations

import base64
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def clamp_limit(raw, default: int = DEFAULT_LIMIT) -> int:
    """Coerce a `limit` query arg into [1, MAX_LIMIT] (bad/missing → default)."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, MAX_LIMIT))


def _iso(v) -> str:
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def encode_cursor(key, row_id) -> str:
    """Opaque cursor for the last row's sort key `(key, id)`."""
    payload = json.dumps({"k": _iso(key), "id": str(row_id)})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(token: Optional[str]):
    """(key, id) from a cursor, or None when absent. Raises ValueError if malformed."""
    if not token:
        return None
    try:
        d = json.loads(base64.urlsafe_b64decode(token.encode()))
        return d["k"], d["id"]
    except Exception as exc:  # malformed / tampered cursor → caller maps to 400
        raise ValueError(f"invalid cursor: {exc}") from exc


def _coerce(v):
    """Make one DB value JSON-safe (Flask jsonify can't do datetime/uuid/Decimal)."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, list):
        return [_coerce(x) for x in v]
    return v


def jsonable(row: dict) -> dict:
    """Coerce a dict_row into a JSON-safe dict."""
    return {k: _coerce(v) for k, v in row.items()}


def build_envelope(rows: list, limit: int, key_attr: str, id_attr: str = "id") -> dict:
    """Wrap already-coerced rows in the paginated response envelope.

    Callers fetch `limit + 1` rows; if the extra row is present there's a next
    page. `next_cursor` is built from the last row WE return (index limit-1).
    """
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = (
        encode_cursor(page[-1][key_attr], page[-1][id_attr])
        if has_more and page
        else None
    )
    return {
        "items": page,
        "page": {
            "limit": limit,
            "count": len(page),
            "has_more": has_more,
            "next_cursor": next_cursor,
        },
    }
