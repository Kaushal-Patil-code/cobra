"""
Fyers Auto Login — V3 API with TOTP + PIN.

PURPOSE:
  Automatically generates a Fyers API access token every day without
  any manual intervention (no browser, no phone OTP, no typing).

LOGIN STRATEGY (3 attempts in order):
  1. Check if cached token is < 22h old (IST) in the fyers_tokens table → reuse it
  2. If cache expired but refresh_token exists → call Fyers refresh API (just PIN, no TOTP)
  3. If nothing works → full 5-step TOTP login from scratch

THE 5-STEP TOTP LOGIN FLOW:
  Step 1: POST /send_login_otp    → "I want to login" → Fyers sends OTP to phone
  Step 2: POST /verify_otp        → "Here's my TOTP code" → Fyers verifies identity
  Step 3: POST /verify_pin        → "Here's my 4-digit PIN" → Fyers gives temp token
  Step 4: POST /api/v3/token      → "Give me auth code for my app" → Fyers returns auth_code
  Step 5: POST /validate-authcode → "Exchange auth code for real token" → Final 24h token

CONFIRMED BEHAVIOR (from Chrome DevTools network capture):
  - send_login_otp: fy_id as plain text, app_id = "2"
  - verify_otp: otp as STRING not int (preserves leading zeros like "063265")
  - verify_pin: pin as plain string
  - /api/v3/token: response has "Url" field (capital U) containing redirect URL
    with auth_code as a query parameter
  - validate-authcode: needs appIdHash = SHA256(client_id:secret_key)

DEPENDENCIES:
  pip install fyers-apiv3 pyotp requests python-dotenv
"""

# ============================================================
# IMPORTS
# ============================================================

import time             # for retry delay (time.sleep)
import logging          # for structured log messages
import requests         # for making HTTP requests to Fyers API
import pyotp            # for generating TOTP codes (same as Google Authenticator)
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime         # for timestamp-based cache expiry
from hashlib import sha256           # for computing appIdHash (SHA256)
from urllib.parse import parse_qs, urlparse  # for extracting auth_code from redirect URL
from zoneinfo import ZoneInfo        # for IST timezone (Asia/Kolkata)
from fyers_apiv3 import fyersModel   # official Fyers SDK for token exchange

# Credentials are loaded lazily via _get_credentials() to avoid reading
# secrets into memory at import time. The settings singleton is imported here
# but values are only accessed when login is actually attempted.
from config.settings import settings as _settings
# Tokens are persisted in Postgres (fyers_tokens table), not on disk.
from db.db import get_conn


def _get_credentials() -> dict:
    """
    Load credentials lazily from config.settings.

    Returns dict with all 6 credential values.
    Called only when login is actually needed, not at import time.
    """
    return {
        "app_id": _settings.fyers_app_id,
        "secret_key": _settings.fyers_secret_key,
        "redirect_url": _settings.fyers_redirect_url,
        "username": _settings.fyers_username,
        "pin": _settings.fyers_pin,
        "totp_secret": _settings.fyers_totp_secret,
    }

# Create a logger for this module
# Messages show up as: "2026-04-15 08:00:01 INFO: Step 1: send_login_otp ✓"
logger = logging.getLogger(__name__)

# ============================================================
# TOKEN STORAGE LOCATION
# ============================================================

# Tokens are persisted in the `fyers_tokens` Postgres table (one row per
# client_id) — see db/schema.sql. Credentials stay in .env; only the issued
# access/refresh tokens live in the database.

# IST timezone for all timestamp operations
IST = ZoneInfo("Asia/Kolkata")

# Maximum token age in hours before re-login is required
# Fyers tokens last ~24h; 22h gives a 2-hour safety margin
MAX_TOKEN_AGE_HOURS = 22

# ============================================================
# HTTP HEADERS
# ============================================================

# These headers mimic a Chrome browser request
# Fyers API checks Origin and Referer headers — without them, requests get rejected
HEADERS = {
    "Accept": "application/json",           # we want JSON responses
    "Content-Type": "application/json",     # we're sending JSON payloads
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",  # pretend to be Chrome
    "Origin": "https://api-t1.fyers.in",    # required by Fyers CORS policy
    "Referer": "https://api-t1.fyers.in/",  # required by Fyers CORS policy
}


# ============================================================
# HELPER FUNCTION
# ============================================================

def _app_id_hash() -> str:
    """
    Compute SHA256 hash of "client_id:secret_key".

    Required by the validate-authcode and validate-refresh-token endpoints.
    This is how Fyers authenticates your API app without sending
    the secret key in plain text.

    Example:
      client_id = "OBOYSW9YS8-100"
      secret_key = "ABCDEFGHIJ"
      SHA256("OBOYSW9YS8-100:ABCDEFGHIJ") → "c3efb1075ef2332b..."
    """
    creds = _get_credentials()
    return sha256(f"{creds['app_id']}:{creds['secret_key']}".encode()).hexdigest()


# Maximum number of retry attempts for transient network failures
MAX_RETRIES = 5
# Seconds to wait between retry attempts
RETRY_DELAY = 2


def _post_with_retry(url: str, headers: dict, json_payload: dict,
                     timeout: int = 10) -> dict:
    """
    POST request with retry logic for transient network failures.

    Retries up to MAX_RETRIES times on ConnectionError and Timeout.
    Does NOT retry on API-level errors (4xx, wrong credentials, etc.)
    — those are permanent failures.

    Returns: parsed JSON response dict on success.
    Raises: requests.RequestException if all retries exhausted.
            ValueError if response is not valid JSON.
    """
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=json_payload, headers=headers,
                                 timeout=timeout)
            return resp.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                logger.debug(f"Retry {attempt}/{MAX_RETRIES} for {url}: {e}")
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"All {MAX_RETRIES} attempts failed for {url}: {e}")
    raise last_exc


# ============================================================
# STRATEGY 1: REFRESH TOKEN (fast path, no TOTP needed)
# ============================================================

def refresh_access_token(refresh_token: str) -> dict:
    """
    Use an existing refresh_token + PIN to get a new access_token.

    This is much faster than full TOTP login because:
    - No OTP step (Step 1 skipped)
    - No TOTP verification (Step 2 skipped)
    - No PIN verification (Step 3 skipped)
    - No auth code generation (Step 4 skipped)
    - Just one API call with refresh_token + PIN

    Refresh tokens last ~15 days, so this works across multiple days
    without needing TOTP at all.

    API endpoint: POST https://api-t1.fyers.in/api/v3/validate-refresh-token
    Payload: {
        "grant_type": "refresh_token",
        "appIdHash": SHA256(client_id:secret_key),
        "refresh_token": "eyJ...",
        "pin": "1234"
    }

    Returns: {"access_token": "...", "refresh_token": "..."} on success
             {} on failure (refresh token expired, need full login)
    """
    try:
        creds = _get_credentials()
        payload = {
            "grant_type": "refresh_token",
            "appIdHash": _app_id_hash(),
            "refresh_token": refresh_token,
            "pin": str(creds["pin"]),
        }

        data = _post_with_retry(
            "https://api-t1.fyers.in/api/v3/validate-refresh-token",
            headers=HEADERS, json_payload=payload
        )

        if data.get("s") == "ok" and data.get("access_token"):
            logger.info("Refresh token ✓")
            return {
                "access_token": data["access_token"],
                # Use new refresh_token if provided, otherwise keep the old one
                "refresh_token": data.get("refresh_token", refresh_token),
            }
    except requests.RequestException as e:
        logger.debug(f"Refresh network error: {e}")
    except (KeyError, ValueError) as e:
        logger.debug(f"Refresh parse error: {e}")

    return {}


# ============================================================
# STRATEGY 2: FULL TOTP LOGIN (5 steps)
# ============================================================

def step1_send_login_otp() -> str:
    """
    Step 1: Tell Fyers "I want to login" → get a request_key.

    This triggers an OTP to be sent to your registered mobile/email.
    But since we use TOTP (Step 2), we never actually need that OTP.
    The request_key is a JWT token that tracks this login session.

    API endpoint: POST https://api-t2.fyers.in/vagator/v2/send_login_otp
    Payload: {"fy_id": "FAI84781", "app_id": "2"}
      - fy_id: your Fyers client ID (plain text works)
      - app_id: "2" means web login (always "2" for API automation)

    Success response: {
        "s": "ok",
        "request_key": "eyJ...",    ← JWT we need for Step 2
        "mobile_no": "93xxxxxx15",
        "totp_enabled": True,
        "client_name": "KAUSHAL"
    }

    Returns: request_key string on success, empty string on failure
    """
    try:
        creds = _get_credentials()
        d = _post_with_retry(
            "https://api-t2.fyers.in/vagator/v2/send_login_otp",
            headers=HEADERS,
            json_payload={"fy_id": creds["username"], "app_id": "2"}
        )

        if d.get("s") == "ok":
            logger.info("Step 1: send_login_otp ✓")
            return d["request_key"]
        else:
            logger.error(f"Step 1 FAILED: {d.get('message', d)}")
    except requests.RequestException as e:
        logger.error(f"Step 1 network error: {e}")
    except (KeyError, ValueError) as e:
        logger.error(f"Step 1 parse error: {e}")

    return ""


def step2_verify_totp(request_key: str) -> str:
    """
    Step 2: Generate a TOTP code and verify it with Fyers.

    pyotp.TOTP(secret).now() generates the same 6-digit code
    that Google Authenticator shows on your phone at this exact moment.
    Both use the same algorithm: HMAC-SHA1(secret, current_time / 30).

    IMPORTANT: OTP is sent as STRING, not integer.
    If the code is "063265", sending int(63265) would fail because
    the leading zero is lost. Fyers expects the string "063265".

    API endpoint: POST https://api-t2.fyers.in/vagator/v2/verify_otp
    Payload: {"request_key": "eyJ...", "otp": "063265"}

    Success response: {
        "s": "ok",
        "request_key": "eyJ...",       ← NEW request_key for Step 3
        "client_name": "Kaushal Sanjay Patil",
        "message": "fy_id and OTP verified successfully"
    }

    Returns: new request_key on success, empty string on failure
    """
    try:
        creds = _get_credentials()
        otp = pyotp.TOTP(creds["totp_secret"]).now()

        d = _post_with_retry(
            "https://api-t2.fyers.in/vagator/v2/verify_otp",
            headers=HEADERS,
            json_payload={"request_key": request_key, "otp": otp}
        )

        if d.get("s") == "ok":
            logger.info("Step 2: verify_totp ✓")
            return d["request_key"]
        else:
            logger.error(f"Step 2 FAILED: {d.get('message', d)}")
    except requests.RequestException as e:
        logger.error(f"Step 2 network error: {e}")
    except (KeyError, ValueError) as e:
        logger.error(f"Step 2 parse error: {e}")

    return ""


def step3_verify_pin(request_key: str) -> str:
    """
    Step 3: Verify 4-digit PIN → get temporary access_token.

    This is the last authentication step. After this, Fyers considers
    you fully logged in and gives you a temporary access_token.
    This temp token is NOT the final API token — it's used in Step 4
    to request an auth_code for your specific API app.

    API endpoint: POST https://api-t2.fyers.in/vagator/v2/verify_pin
    Payload: {
        "request_key": "eyJ...",
        "identity_type": "pin",
        "identifier": "1234"       ← PIN as plain string
    }

    Success response: {
        "s": "ok",
        "data": {
            "access_token": "eyJ..."   ← temporary token for Step 4
        },
        "message": "pin is Verified"
    }

    Returns: temporary access_token on success, empty string on failure
    """
    try:
        creds = _get_credentials()
        d = _post_with_retry(
            "https://api-t2.fyers.in/vagator/v2/verify_pin",
            headers=HEADERS,
            json_payload={
                "request_key": request_key,
                "identity_type": "pin",
                "identifier": str(creds["pin"]),
            }
        )

        if d.get("s") == "ok":
            logger.info("Step 3: verify_pin ✓")
            return d["data"]["access_token"]
        else:
            logger.error(f"Step 3 FAILED: {d.get('message', d)}")
    except requests.RequestException as e:
        logger.error(f"Step 3 network error: {e}")
    except (KeyError, ValueError) as e:
        logger.error(f"Step 3 parse error: {e}")

    return ""


def step4_get_auth_response(access_token: str) -> dict:
    """
    Step 4 (full response): Use the temporary access_token to get an
    auth_code for your specific API app — and return the COMPLETE
    Fyers response with all fields parsed out.

    Unlike step4_get_auth_code (which returns only the bare auth_code
    string), this returns a structured dict containing the redirect Url,
    the session cookie JWT, the Fyers status fields, AND the extracted
    auth_code. Use this when you need the cookie or the full URL — for
    example, to hand off a browser session, log the redirect for audit,
    or run the equivalent of test.py end-to-end programmatically.

    API endpoint: POST https://api-t1.fyers.in/api/v3/token
    Headers: Authorization: Bearer <temp_access_token>
    Payload: {
        "fyers_id": "FAI84781",
        "app_id": "OBOYSW9YS8",        ← without -100 suffix
        "redirect_uri": "https://v0-fyersanalysis.vercel.app",
        "appType": "100",
        ...
    }

    Confirmed Fyers response shape (from Chrome DevTools + test.py):
    {
        "Url": "https://v0-fyersanalysis.vercel.app?s=ok&code=200&auth_code=eyJ...",
        "code": 308,
        "cookie": "_FYERS=eyJ...",
        "message": "",
        "s": "ok"
    }

    NOTE: Field name is "Url" with capital U; auth_code lives as a query
    parameter inside Url, not as its own top-level field.

    Returns on success:
        {
            "ok": True,
            "auth_code": "eyJ...",       # extracted from Url query string
            "url": "https://...",         # full redirect URL (Fyers' "Url")
            "cookie": "_FYERS=eyJ...",    # session cookie JWT (may be "")
            "code": 308,                  # Fyers internal step code
            "status": "ok",               # Fyers "s" field
            "message": "",
            "raw": { ... }                # full unmodified response dict
        }

    Returns on failure:
        {"ok": False, "error": "<reason>", "raw": { ... } | None}
    """
    try:
        creds = _get_credentials()
        app_id = creds["app_id"].split("-")[0]
        h = {**HEADERS, "Authorization": f"Bearer {access_token}"}

        payload = {
            "fyers_id": creds["username"],
            "app_id": app_id,
            "redirect_uri": creds["redirect_url"],
            "appType": "100",
            "code_challenge": "",
            "state": "sample_state",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True,
        }

        d = _post_with_retry(
            "https://api-t1.fyers.in/api/v3/token",
            headers=h, json_payload=payload
        )

        if d.get("s") != "ok":
            logger.error(f"Step 4 FAILED: {d}")
            return {"ok": False, "error": d.get("message") or "non-ok status", "raw": d}

        redirect_url = d.get("Url", "")
        auth_code = parse_qs(urlparse(redirect_url).query).get("auth_code", [""])[0]

        if not auth_code:
            logger.error(f"Step 4 FAILED: auth_code missing in Url: {redirect_url!r}")
            return {"ok": False, "error": "auth_code missing in Url", "raw": d}

        logger.info("Step 4: get_auth_response ✓")
        return {
            "ok": True,
            "auth_code": auth_code,
            "url": redirect_url,
            "cookie": d.get("cookie", ""),
            "code": d.get("code"),
            "status": d.get("s"),
            "message": d.get("message", ""),
            "raw": d,
        }
    except requests.RequestException as e:
        logger.error(f"Step 4 network error: {e}")
        return {"ok": False, "error": f"network: {e}", "raw": None}
    except (KeyError, ValueError) as e:
        logger.error(f"Step 4 parse error: {e}")
        return {"ok": False, "error": f"parse: {e}", "raw": None}


def step4_get_auth_code(access_token: str) -> str:
    """
    Step 4 (compact): Return only the auth_code string.

    Thin wrapper around step4_get_auth_response — preserved for callers
    (full_totp_login) that only need the auth_code and discard the rest.

    Returns: auth_code string on success, empty string on failure.
    """
    resp = step4_get_auth_response(access_token)
    return resp["auth_code"] if resp.get("ok") else ""


def get_auth_code() -> dict:
    """
    Run the TOTP login flow up to Step 4 and return the full Step 4
    response — exactly what test.py prints, but as a reusable module
    function with credential validation, retry logic, and structured
    error reporting.

    Pipeline: validate credentials → Step 1 → Step 2 → Step 3 → Step 4.
    Step 5 (auth_code → access_token exchange) is intentionally NOT run
    here — call full_totp_login() or get_fyers_token() for that.

    Use this when you need the auth_code together with the session
    cookie / redirect URL (e.g., to hand off a browser-style session,
    audit the redirect, or test the flow without minting a final token).

    Returns on success:
        {
            "ok": True,
            "auth_code": "eyJ...",
            "url":  "https://<redirect>?s=ok&code=200&auth_code=eyJ...&state=...",
            "cookie": "_FYERS=eyJ...",
            "code": 308,
            "status": "ok",
            "message": "",
            "raw": { ... }                # full Step 4 response, unmodified
        }

    Returns on failure (any step):
        {"ok": False, "error": "<which step + reason>",
         "step": <1|2|3|4|"credentials">, "raw": <step response or None>}
    """
    creds = _get_credentials()
    missing = [k for k, v in creds.items() if not v]
    if missing:
        msg = f"Missing in .env: {', '.join(missing)}"
        logger.error(msg)
        return {"ok": False, "step": "credentials", "error": msg, "raw": None}

    logger.info(f"get_auth_code: running steps 1-4 as {creds['username']}...")

    request_key = step1_send_login_otp()
    if not request_key:
        return {"ok": False, "step": 1, "error": "send_login_otp failed", "raw": None}

    request_key = step2_verify_totp(request_key)
    if not request_key:
        return {"ok": False, "step": 2, "error": "verify_totp failed", "raw": None}

    temp_token = step3_verify_pin(request_key)
    if not temp_token:
        return {"ok": False, "step": 3, "error": "verify_pin failed", "raw": None}

    resp = step4_get_auth_response(temp_token)
    if not resp.get("ok"):
        resp.setdefault("step", 4)
        return resp

    return resp


def step5_validate_auth_code(auth_code: str) -> dict:
    """
    Step 5: Exchange auth_code for the FINAL access_token + refresh_token.

    This is the last step. The access_token returned here is the one
    you use for all API calls (quotes, orders, option chain, etc.).
    Valid for ~24 hours.

    The refresh_token is valid for ~15 days and can be used to get
    a new access_token without doing the full 5-step login again.

    Two methods tried:
      A) Fyers SDK: fyersModel.SessionModel.generate_token()
         - Handles appIdHash computation internally
         - Uses the official SDK flow
      B) Direct API call (fallback if SDK fails):
         POST https://api-t1.fyers.in/api/v3/validate-authcode
         {"grant_type": "authorization_code", "appIdHash": "SHA256...", "code": "eyJ..."}

    Returns: {"access_token": "...", "refresh_token": "..."} on success
             {} on failure
    """

    # ---- Method A: Use Fyers SDK (with 15s timeout) ----
    try:
        creds = _get_credentials()
        session = fyersModel.SessionModel(
            client_id=creds["app_id"],
            secret_key=creds["secret_key"],
            redirect_uri=creds["redirect_url"],
            response_type="code",
            grant_type="authorization_code",
        )
        session.set_token(auth_code)

        # Wrap SDK call in a thread with timeout — generate_token() has no
        # built-in timeout and can hang indefinitely
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(session.generate_token)
            resp = future.result(timeout=15)

        if resp.get("s") == "ok" or resp.get("code") == 200:
            logger.info("Step 5: validate_auth_code ✓ (SDK)")
            return {
                "access_token": resp.get("access_token", ""),
                "refresh_token": resp.get("refresh_token", ""),
            }
    except FuturesTimeoutError:
        logger.warning("Step 5 SDK timed out after 15s, trying direct API...")
    except Exception as e:
        logger.debug(f"Step 5 SDK: {e}")

    # ---- Method B: Direct API call (fallback) ----
    try:
        payload = {
            "grant_type": "authorization_code",
            "appIdHash": _app_id_hash(),
            "code": auth_code,
        }

        d = _post_with_retry(
            "https://api-t1.fyers.in/api/v3/validate-authcode",
            headers=HEADERS, json_payload=payload
        )

        if d.get("s") == "ok":
            logger.info("Step 5: validate_auth_code ✓ (direct API)")
            return {
                "access_token": d.get("access_token", ""),
                "refresh_token": d.get("refresh_token", ""),
            }
        else:
            logger.error(f"Step 5 FAILED: {d}")
    except requests.RequestException as e:
        logger.error(f"Step 5 network error: {e}")
    except (KeyError, ValueError) as e:
        logger.error(f"Step 5 parse error: {e}")

    return {}


def full_totp_login() -> dict:
    """
    Execute the complete 5-step TOTP login flow.

    First validates that all 6 credentials exist in .env.
    Then runs Steps 1→2→3→4→5 in sequence.
    If any step fails, the whole login fails (returns empty dict).

    Returns: {"access_token": "...", "refresh_token": "..."} on success
             {} on failure
    """

    creds = _get_credentials()
    missing = [k for k, v in creds.items() if not v]
    if missing:
        logger.error(f"Missing in .env: {', '.join(missing)}")
        return {}
    logger.info(f"Full TOTP login as {creds['username']}...")

    # Step 1: "I want to login" → get request_key
    request_key = step1_send_login_otp()
    if not request_key: return {}  # failed, abort

    # Step 2: "Here's my TOTP code" → get new request_key
    request_key = step2_verify_totp(request_key)
    if not request_key: return {}

    # Step 3: "Here's my PIN" → get temporary access_token
    access_token = step3_verify_pin(request_key)
    if not access_token: return {}

    # Step 4: "Give me auth code for my app" → get auth_code
    auth_code = step4_get_auth_code(access_token)
    if not auth_code: return {}

    # Step 5: "Exchange auth code for real token" → get final tokens
    return step5_validate_auth_code(auth_code)


def full_totp_login_with_retry(max_attempts=None, delay=None) -> dict:
    """
    Run full_totp_login() up to `max_attempts` times, sleeping `delay` seconds
    between attempts, until it returns tokens.

    This is the flow-level retry that sits on top of the per-request network
    retries in _post_with_retry(): it re-runs the whole 5-step sequence — with a
    fresh request_key and a fresh TOTP code — when an attempt fails for a
    non-network reason (e.g. a transient non-"ok" Fyers response, or a TOTP code
    rejected right at a 30-second window boundary).

    Missing credentials is a permanent error: returns {} immediately, no retry.
    Defaults come from settings (FYERS_LOGIN_MAX_ATTEMPTS / FYERS_LOGIN_RETRY_DELAY).

    Returns: {"access_token": ..., "refresh_token": ...} on success, {} on failure.
    """
    creds = _get_credentials()
    missing = [k for k, v in creds.items() if not v]
    if missing:
        logger.error(f"Missing in .env: {', '.join(missing)}")
        return {}

    if max_attempts is None:
        max_attempts = _settings.fyers_login_max_attempts
    if delay is None:
        delay = _settings.fyers_login_retry_delay
    max_attempts = max(1, int(max_attempts))

    for attempt in range(1, max_attempts + 1):
        tokens = full_totp_login()
        if tokens.get("access_token"):
            if attempt > 1:
                logger.info(f"Login succeeded on attempt {attempt}/{max_attempts}")
            return tokens
        if attempt < max_attempts:
            logger.warning(
                f"Login attempt {attempt}/{max_attempts} failed; retrying in {delay}s"
            )
            time.sleep(delay)

    logger.error(f"Login failed after {max_attempts} attempt(s)")
    return {}


# ============================================================
# TOKEN STORAGE
# ============================================================

def save_token(tokens: dict):
    """
    Persist access_token and refresh_token to the `fyers_tokens` table.

    Replaces the existing row's data with a plain UPDATE (no upsert). On the
    very first login there is no row yet, so a one-time INSERT seeds it; every
    subsequent re-login just UPDATEs in place. updated_at doubles as the
    "issued at" timestamp used for age-based cache expiry.

    Keyed by the Fyers client_id (settings.fyers_app_id).
    """
    creds = _get_credentials()
    client_id = creds["app_id"]
    now_ist = datetime.now(IST)
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE fyers_tokens
               SET access_token  = %s,
                   refresh_token = %s,
                   updated_at    = %s
             WHERE client_id = %s
            """,
            (access_token, refresh_token, now_ist, client_id),
        )
        if cur.rowcount == 0:
            # No existing row for this client_id — seed it once.
            cur.execute(
                """
                INSERT INTO fyers_tokens (client_id, access_token, refresh_token, updated_at)
                VALUES (%s, %s, %s, %s)
                """,
                (client_id, access_token, refresh_token, now_ist),
            )
            logger.info(f"Tokens inserted in DB (fyers_tokens) for client_id={client_id}")
        else:
            logger.info(f"Tokens updated in DB (fyers_tokens) for client_id={client_id}")


def load_token() -> dict:
    """
    Load cached tokens for the configured client_id from the `fyers_tokens` table.

    Returns a dict shaped like the legacy file cache —
        {"access_token": ..., "refresh_token": ..., "timestamp": <ISO 8601>}
    — so get_fyers_token()'s age check is unchanged. Returns {} if there's no
    row or the lookup fails (table missing, DB unreachable), which makes
    get_fyers_token() fall through to refresh / full login exactly as a missing
    file did before.
    """
    creds = _get_credentials()
    client_id = creds["app_id"]
    if not client_id:
        return {}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT access_token, refresh_token, updated_at "
                "FROM fyers_tokens WHERE client_id = %s",
                (client_id,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.debug(f"load_token DB error: {e}")
        return {}
    if not row:
        return {}
    ts = row["updated_at"]
    return {
        "access_token": row["access_token"] or "",
        "refresh_token": row["refresh_token"] or "",
        "timestamp": ts.isoformat() if ts else "",
    }


def invalidate_token():
    """Mark the cached access token stale so the next get_fyers_token() re-auths.

    Used when Fyers rejects a token mid-use (e.g. it expired before our 22h
    heuristic). Backdates updated_at to the epoch so the freshness check fails;
    the refresh_token is kept, so get_fyers_token() tries refresh first, then a
    full TOTP login.
    """
    creds = _get_credentials()
    client_id = creds["app_id"]
    if not client_id:
        return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE fyers_tokens SET updated_at = to_timestamp(0) WHERE client_id = %s",
                (client_id,),
            )
        logger.info("Invalidated cached token for client_id=%s", client_id)
    except Exception as e:
        logger.debug(f"invalidate_token DB error: {e}")


# ============================================================
# MAIN ENTRY POINTS
# ============================================================

def get_fyers_token() -> str:
    """
    THE MAIN FUNCTION — called by every part of the pipeline.

    Returns a valid access_token string.

    Strategy (tried in order):
      1. Today's cached token exists → return it immediately (0 API calls)
      2. Refresh token exists → call refresh API (1 API call, no TOTP)
      3. Neither works → full 5-step TOTP login (5 API calls)

    Usage:
        token = get_fyers_token()
        # token is now a valid string like "OBOYSW9YS8-100:eyJhb..."
    """

    # Load whatever tokens we have cached from previous runs
    cached = load_token()

    # ---- Strategy 1: Reuse cached token if still fresh ----
    # Token is valid if age < MAX_TOKEN_AGE_HOURS (22h, gives 2h safety margin on 24h lifetime)
    if cached.get("timestamp") and cached.get("access_token"):
        try:
            saved_time = datetime.fromisoformat(cached["timestamp"])
            now_ist = datetime.now(IST)
            age_hours = (now_ist - saved_time).total_seconds() / 3600
            if age_hours < MAX_TOKEN_AGE_HOURS:
                logger.info(f"Using cached token ({age_hours:.1f}h old)")
                return cached["access_token"]
            else:
                logger.info(f"Cached token expired ({age_hours:.1f}h old)")
        except (ValueError, TypeError):
            logger.debug("Could not parse cached timestamp, will re-login")

    # ---- Strategy 2: Refresh using stored refresh_token ----
    # Refresh tokens last ~15 days, so even if the access token expired,
    # we can get a new one without doing the full TOTP dance
    if cached.get("refresh_token"):
        logger.info("Attempting token refresh...")
        tokens = refresh_access_token(cached["refresh_token"])
        if tokens.get("access_token"):
            save_token(tokens)               # save the new access token
            return tokens["access_token"]    # return it
        # If refresh failed (token expired after 15 days), fall through to Strategy 3

    # ---- Strategy 3: Full TOTP login from scratch (with retry) ----
    logger.info("Full TOTP login...")
    tokens = full_totp_login_with_retry()
    if tokens.get("access_token"):
        save_token(tokens)             # save both access and refresh tokens
        logger.info("Login successful!")
        return tokens["access_token"]

    # All strategies failed
    logger.error("All login strategies failed!")
    return ""  # empty string = no valid token


def get_fyers_client():
    """
    Returns a ready-to-use FyersModel client with a valid token.

    This is the convenience function that collectors and compute modules use.
    They don't need to worry about login/caching/refresh — just call this.

    Usage:
        client = get_fyers_client()
        quotes = client.quotes({"symbols": "NSE:RELIANCE-EQ"})
        chain = client.optionchain({"symbol": "NSE:NIFTY50-INDEX"})
        profile = client.get_profile()

    Raises RuntimeError if login fails (all strategies exhausted).
    """

    # Get a valid token (cached, refreshed, or fresh login)
    token = get_fyers_token()

    # If no token, we can't proceed — raise an error so the caller knows
    if not token:
        raise RuntimeError("No valid Fyers token. Check .env")

    # Create and return a FyersModel client
    # client_id = your app ID, token = the access token
    # is_async=False means synchronous calls (simpler, blocking)
    return fyersModel.FyersModel(
        client_id=_get_credentials()["app_id"],  # "OBOYSW9YS8-100"
        token=token,              # "OBOYSW9YS8-100:eyJhb..."
        is_async=False,           # synchronous mode
    )


def ensure_valid_token() -> str:
    """Return an access token validated *live* with Fyers (one get_profile call).

    get_fyers_token() trusts a 22h age heuristic, but Fyers expires tokens on a
    daily cutoff — so an age-"fresh" cached token can already be dead. This
    confirms the token actually works and re-authenticates if not. Heavier than
    get_fyers_token() (one API call) — use at startup/warm, not per fetch.
    """
    token = get_fyers_token()
    if not token:
        return ""
    try:
        client = fyersModel.FyersModel(
            client_id=_get_credentials()["app_id"], token=token, is_async=False
        )
        resp = client.get_profile()
    except Exception as e:
        logger.debug(f"token validation call errored: {e}")
        resp = {}
    if isinstance(resp, dict) and resp.get("s") == "ok":
        return token
    logger.info("cached token failed validation — re-authenticating")
    invalidate_token()
    return get_fyers_token()


def get_profile() -> dict:
    """Get account profile from Fyers API."""
    client = get_fyers_client()
    resp = client.get_profile()
    if resp.get("s") == "ok":
        return resp.get("data", {})
    raise RuntimeError(f"Profile fetch failed: {resp}")
