"""
Central settings — typed, validated configuration loaded from environment / .env.

Single source of truth. Import the `settings` singleton everywhere:

    from config.settings import settings
    settings.effective_dsn

Validation runs once at import (cached). A missing/invalid required value fails
fast with a clear pydantic error at startup instead of a late AttributeError.

Mirrors Analyzer's config/settings.py role, upgraded to pydantic-settings so
every value is typed, coerced, and documented in one place.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional
from urllib.parse import quote

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # tolerate unrelated keys (Supabase anon key, etc.)
    )

    # ------------------------------------------------------------------
    # App
    # ------------------------------------------------------------------
    app_name: str = "cobra"
    env: Literal["development", "production", "testing"] = "development"
    debug: bool = False

    # HTTP server (dev server / gunicorn bind)
    host: str = "0.0.0.0"
    port: int = 8000

    # ------------------------------------------------------------------
    # Database — Supabase Postgres, direct connection (psycopg3)
    # ------------------------------------------------------------------
    # Preferred: full connection string from the Supabase dashboard.
    database_url: Optional[str] = None

    # Fallback parts — used only if database_url is unset (mirrors Analyzer .env).
    db_host: Optional[str] = None
    db_port: int = 5432
    db_name: str = "postgres"
    db_user: Optional[str] = None
    db_password: Optional[str] = None
    db_sslmode: str = "require"  # Supabase requires TLS

    # Connection-pool tuning (the main perf lever for a concurrent server).
    db_pool_min_size: int = Field(default=1, ge=0)
    db_pool_max_size: int = Field(default=10, ge=1)
    db_pool_max_idle: int = 600          # seconds an over-min idle conn lives
    db_connect_timeout: int = 10         # seconds
    db_statement_timeout_ms: int = 30_000

    # Prepared statements: psycopg auto-prepares after N executions (fast path).
    # Disabled automatically when the Supabase transaction pooler (:6543) is
    # detected — pgBouncer transaction mode breaks server-side prepares.
    db_prepare_threshold: int = 5
    db_disable_prepared_statements: bool = False

    # ------------------------------------------------------------------
    # Fyers broker auto-login (TOTP) — credentials live here; the resulting
    # access/refresh tokens are persisted in the fyers_tokens DB table.
    # ------------------------------------------------------------------
    fyers_app_id: Optional[str] = None
    fyers_secret_key: Optional[str] = None
    fyers_redirect_url: Optional[str] = None
    fyers_username: Optional[str] = None
    fyers_pin: Optional[str] = None
    fyers_totp_secret: Optional[str] = None
    # Warm the Fyers token on server startup (single-flight across workers via a
    # Postgres advisory lock). No-ops when credentials are unset.
    fyers_autologin_on_startup: bool = True
    # Flow-level retry: re-run the whole 5-step TOTP login this many times, with
    # this delay (seconds) between attempts, when an attempt fails. This is on
    # top of the per-request network retries in auth._post_with_retry.
    fyers_login_max_attempts: int = Field(default=3, ge=1)
    fyers_login_retry_delay: int = Field(default=5, ge=0)

    # ------------------------------------------------------------------
    # Internal tick scheduler (v3) — self-drives the cycle (fetch → store →
    # metrics → lock → compute → persist) right after auto-login, then every
    # tick_interval_minutes during market hours. Single-flight across workers via
    # a Postgres advisory lock + the market-minute de-dup. Turn OFF to drive
    # GET /tick from an external cron instead (§6.2). Requires autologin on.
    # ------------------------------------------------------------------
    scheduler_enabled: bool = True
    tick_interval_minutes: int = Field(default=3, ge=1)
    tick_strikecount: int = Field(default=10, ge=1)
    tick_window_minutes: int = 15
    # Pre-warm the Fyers token a couple minutes before the open (09:13 IST) so the
    # first capture at 09:15 already has a valid token.
    prelogin_hour: int = Field(default=9, ge=0, le=23)
    prelogin_minute: int = Field(default=13, ge=0, le=59)

    # ------------------------------------------------------------------
    # Render keep-alive — a free web service spins down after ~15 min idle, and
    # the internal scheduler can't wake a slept process. So the app pings its OWN
    # public /health every keepalive_interval_minutes to stay warm 24/7 (which
    # lets the 09:13 pre-login + 09:15 ticks fire). On Render, RENDER_EXTERNAL_URL
    # is set automatically and maps to render_external_url here. Blank = disabled.
    # ------------------------------------------------------------------
    render_external_url: Optional[str] = None   # Render injects this automatically
    keepalive_url: Optional[str] = None          # set MANUALLY (your public base URL); wins over the above
    keepalive_enabled: bool = True
    keepalive_interval_minutes: int = Field(default=7, ge=1)

    @property
    def effective_dsn(self) -> Optional[str]:
        """Resolved connection string: database_url, else assembled from parts."""
        if self.database_url:
            return self.database_url
        if self.db_host and self.db_user and self.db_password:
            pw = quote(self.db_password, safe="")
            return (
                f"postgresql://{self.db_user}:{pw}@{self.db_host}:{self.db_port}"
                f"/{self.db_name}?sslmode={self.db_sslmode}"
            )
        return None

    @property
    def keepalive_target(self) -> Optional[str]:
        """Public base URL the keep-alive job self-pings. A manually-set
        KEEPALIVE_URL wins; otherwise Render's auto-injected RENDER_EXTERNAL_URL."""
        return self.keepalive_url or self.render_external_url

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
