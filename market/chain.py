"""Fyers option-chain client — pull the raw chain for an underlying.

One call per underlying returns the whole chain (spec §2), so a full cycle is
two calls. Symbols live in the `instruments` table (market.instruments) — the
caller resolves the symbol and passes it here.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Transient-failure retry for a single chain pull (network blip / momentary
# non-ok). A fully failed pull just waits for the next 3-min tick; a couple of
# quick retries smooth over blips without risking the rate limit.
FETCH_MAX_ATTEMPTS = 3
FETCH_RETRY_DELAY = 2  # seconds


class ChainFetchError(RuntimeError):
    """Raised when Fyers returns a non-ok option-chain response (or errors)."""


class TokenRejectedError(ChainFetchError):
    """Fyers rejected the access token (code -15) — needs re-login, not retry."""


def _is_token_error(resp) -> bool:
    return isinstance(resp, dict) and (
        resp.get("code") == -15
        or "valid token" in str(resp.get("message", "")).lower()
    )


def fetch_raw_chain(client, symbol: str, strikecount: int = 10, timestamp: str = "") -> dict:
    """Return the raw Fyers `optionchain` response dict for `symbol`.

    `client` is a FyersModel from auth.get_fyers_client() (built once per cycle).
    `symbol` is the Fyers underlying (e.g. 'NSE:NIFTY50-INDEX') from the
    instruments registry. `timestamp` selects which expiry's chain to pull:
    "" → nearest (default; the response still lists every expiry in `expiryData`);
    pass an expiry epoch (from a prior pull's `expiry_epoch` / `available_expiries`)
    to fetch that specific expiry.

    Retries transient failures a few times. Raises TokenRejectedError immediately
    on an auth error (no point retrying a dead token — the caller re-authenticates),
    or ChainFetchError if all attempts fail (so we never store partial data).
    """
    last_error = None
    for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
        try:
            resp = client.optionchain(
                data={"symbol": symbol, "strikecount": strikecount, "timestamp": timestamp}
            )
        except Exception as exc:  # network/SDK errors are transient — retry
            last_error = exc
        else:
            if isinstance(resp, dict) and resp.get("s") == "ok":
                return resp
            if _is_token_error(resp):
                raise TokenRejectedError(f"{symbol}: Fyers rejected the token: {resp}")
            last_error = ChainFetchError(f"{symbol} optionchain non-ok: {resp}")
        if attempt < FETCH_MAX_ATTEMPTS:
            logger.warning(
                "optionchain %s attempt %d/%d failed (%s); retrying in %ds",
                symbol, attempt, FETCH_MAX_ATTEMPTS, last_error, FETCH_RETRY_DELAY,
            )
            time.sleep(FETCH_RETRY_DELAY)
    raise ChainFetchError(
        f"{symbol} optionchain failed after {FETCH_MAX_ATTEMPTS} attempts: {last_error}"
    )
