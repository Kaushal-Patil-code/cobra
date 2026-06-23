"""Flow-level retry for the Fyers TOTP login (no network — full_totp_login mocked)."""
from unittest.mock import patch

from auth import auth as a

FULL_CREDS = {
    "app_id": "X", "secret_key": "X", "redirect_url": "X",
    "username": "X", "pin": "X", "totp_secret": "X",
}


def test_retries_until_success():
    seq = [{}, {}, {"access_token": "a", "refresh_token": "r"}]
    with patch("auth.auth._get_credentials", return_value=FULL_CREDS), \
         patch("auth.auth.full_totp_login", side_effect=seq) as m:
        out = a.full_totp_login_with_retry(max_attempts=3, delay=0)
    assert out == {"access_token": "a", "refresh_token": "r"}
    assert m.call_count == 3


def test_gives_up_after_max_attempts():
    with patch("auth.auth._get_credentials", return_value=FULL_CREDS), \
         patch("auth.auth.full_totp_login", return_value={}) as m:
        out = a.full_totp_login_with_retry(max_attempts=2, delay=0)
    assert out == {}
    assert m.call_count == 2


def test_no_retry_when_credentials_missing():
    creds = {**FULL_CREDS, "pin": None}
    with patch("auth.auth._get_credentials", return_value=creds), \
         patch("auth.auth.full_totp_login", return_value={}) as m:
        out = a.full_totp_login_with_retry(max_attempts=3, delay=0)
    assert out == {}
    m.assert_not_called()
