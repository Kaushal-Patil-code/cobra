"""Unit tests for auth.force_login.

force_full_login() must:
- Bypass cache: never call load_token().
- Skip refresh path: never call refresh_access_token().
- Run full TOTP via auth.full_totp_login() and persist via auth.save_token().
- Serialize concurrent calls via a module-level threading.Lock.
"""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from auth import force_login


class TestForceFullLogin:
    def test_returns_tokens_on_success_and_saves(self):
        fake_tokens = {"access_token": "a", "refresh_token": "r"}
        with patch("auth.force_login.full_totp_login", return_value=fake_tokens) as m_login, \
             patch("auth.force_login.save_token") as m_save:
            result = force_login.force_full_login()
        assert result == fake_tokens
        m_login.assert_called_once_with()
        m_save.assert_called_once_with(fake_tokens)

    def test_returns_empty_on_failure_and_does_not_save(self):
        with patch("auth.force_login.full_totp_login", return_value={}) as m_login, \
             patch("auth.force_login.save_token") as m_save:
            result = force_login.force_full_login()
        assert result == {}
        m_login.assert_called_once_with()
        m_save.assert_not_called()

    def test_does_not_call_load_token_or_refresh(self):
        # Force login MUST NOT consult cache or refresh; both shortcuts skipped.
        with patch("auth.force_login.full_totp_login", return_value={"access_token": "x", "refresh_token": "y"}), \
             patch("auth.force_login.save_token"), \
             patch("auth.auth.load_token") as m_load, \
             patch("auth.auth.refresh_access_token") as m_refresh:
            force_login.force_full_login()
        m_load.assert_not_called()
        m_refresh.assert_not_called()

    def test_concurrent_calls_serialize(self):
        # Two threads call force_full_login() at the same time.
        # full_totp_login is mocked to record the maximum number of overlapping
        # invocations. Lock guarantees max == 1.
        in_flight = 0
        max_in_flight = 0
        lock = __import__("threading").Lock()

        def slow_login():
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            # simulate work
            __import__("time").sleep(0.05)
            with lock:
                in_flight -= 1
            return {"access_token": "a", "refresh_token": "r"}

        with patch("auth.force_login.full_totp_login", side_effect=slow_login), \
             patch("auth.force_login.save_token"):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(force_login.force_full_login) for _ in range(4)]
                for f in futures:
                    f.result()
        assert max_in_flight == 1, f"expected serialized calls, got {max_in_flight} concurrent"
