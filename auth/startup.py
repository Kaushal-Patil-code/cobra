"""Fyers login coordination — startup warm + reactive re-auth, both single-flight.

Both acquire a Postgres **transaction-level advisory lock** on the same key, so
only one worker logs in at a time (concurrent TOTP logins trip Fyers anti-fraud).
The lock auto-releases on commit/rollback/crash — pooler-safe (a session lock
would leak on the transaction pooler).

- warm_login_once(): startup warm. Non-blocking try-lock — the winner ensures a
  *validated* token; the rest skip.
- relogin_single_flight(): reactive re-auth when a live call rejects the token.
  Blocking lock — waits out any in-flight login and reuses its fresh token rather
  than logging in again.
"""
from __future__ import annotations

import logging

from auth.auth import (
    _get_credentials,
    ensure_valid_token,
    get_fyers_client,
    get_fyers_token,
    invalidate_token,
    load_token,
)
from db.db import get_conn

logger = logging.getLogger(__name__)

# Stable bigint key for COBRA's Fyers-login advisory lock (shared by warm + re-auth).
_LOGIN_LOCK_KEY = 728_051_193


def warm_login_once() -> str:
    """Ensure a valid Fyers token exists, only on the worker that wins the lock.

    Returns the validated access token on the winning worker, or "" when skipped
    (credentials unset, another worker holds the lock, or login failed). The
    caller uses a non-empty return to decide whether THIS worker should fire the
    immediate startup tick — so only the worker that just secured the token does,
    avoiding a second concurrent TOTP login. No-ops quietly and never raises.
    """
    creds = _get_credentials()
    missing = [k for k, v in creds.items() if not v]
    if missing:
        logger.info(
            "Fyers startup login skipped — not configured (missing: %s)",
            ", ".join(missing),
        )
        return ""

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS got", (_LOGIN_LOCK_KEY,))
                got = cur.fetchone()["got"]
            if not got:
                logger.info("Fyers startup login: another worker holds the lock, skipping")
                return ""

            token = ensure_valid_token()  # validated live; re-auths if the cached token is dead
            if token:
                logger.info("Fyers startup login: token ready")
                return token
            logger.warning("Fyers startup login: no token obtained")
            return ""
    except Exception as exc:
        logger.warning("Fyers startup login error: %s", exc)
        return ""


def relogin_single_flight():
    """Re-authenticate, serialized across workers by the login advisory lock.

    Blocking: if another worker is mid-login we wait, then reuse the token it
    refreshed (detected via a changed timestamp) instead of logging in again.
    Returns a fresh FyersModel client. Raises if re-login ultimately fails.
    """
    before = load_token().get("timestamp")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOGIN_LOCK_KEY,))  # blocks until held

        after = load_token()
        if after.get("access_token") and after.get("timestamp") != before:
            logger.info("re-login: reusing token refreshed by another worker")
            return get_fyers_client()

        invalidate_token()
        if not get_fyers_token():
            raise RuntimeError("re-login failed (no token obtained)")
        return get_fyers_client()
