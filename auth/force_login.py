"""Force a fresh full TOTP login, bypassing all caches and refresh shortcuts.

Used by:
  - the daily 09:13 IST scheduler
  - the Telegram /frc command

Concurrent callers are serialized by a module-level threading.Lock so the
TOTP flow never runs twice in parallel (which would double-charge the
underlying API and could trigger anti-fraud throttling on Fyers' side).
"""
import logging
import threading

from auth.auth import full_totp_login, save_token

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def force_full_login() -> dict:
    """Run the full 5-step TOTP flow and persist the result.

    Bypasses load_token() and refresh_access_token() — every call hits steps 1-5.
    On success, save_token() persists the new access + refresh tokens.

    Returns the tokens dict on success, {} on failure.
    """
    with _LOCK:
        logger.info("force_full_login: starting full TOTP flow")
        tokens = full_totp_login()
        if tokens.get("access_token"):
            save_token(tokens)
            logger.info("force_full_login: success")
            return tokens
        logger.error("force_full_login: failed (full_totp_login returned empty)")
        return {}
