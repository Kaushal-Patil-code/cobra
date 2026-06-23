"""Flask application factory.

    from app import create_app
    app = create_app()

Each worker process (gunicorn worker / dev server) calls create_app() once and
opens its own DB pool. Do NOT run gunicorn with --preload: forking a process
that already holds open DB sockets corrupts the connections.
"""
from __future__ import annotations

import atexit
import logging

from flask import Flask, jsonify

from config.settings import settings
from db.db import close_pool, open_pool

from .api.dashboard import bp as dashboard_bp
from .api.health import bp as health_bp
from .api.tables import bp as tables_bp
from .errors import register_error_handlers


def create_app() -> Flask:
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = Flask(__name__)
    app.config.update(
        ENV=settings.env,
        DEBUG=settings.debug,
        JSON_SORT_KEYS=False,
        JSONIFY_PRETTYPRINT_REGULAR=not settings.is_production,
    )

    register_error_handlers(app)
    app.register_blueprint(health_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(tables_bp)

    @app.get("/")
    def index():
        return jsonify(app=settings.app_name, status="ok", env=settings.env)

    _init_db(app)
    _warm_fyers(app)
    return app


def _init_db(app: Flask) -> None:
    """Open the pool at startup if configured; tolerate absence so the server
    still boots (and /health works) before Supabase creds are filled in."""
    if not settings.effective_dsn:
        app.logger.warning(
            "No DATABASE_URL configured — DB-backed endpoints will return 503. "
            "Add Supabase credentials to .env (see .env.example)."
        )
        return
    try:
        open_pool()
        atexit.register(close_pool)
    except Exception as exc:  # unreachable DB shouldn't stop the server booting
        app.logger.warning("DB pool not opened at startup: %s", exc)


def _warm_fyers(app: Flask) -> None:
    """Warm the Fyers token on boot, then self-drive the tick cycle (v3).

    Runs in a daemon thread so a slow or retrying TOTP login can't block — or get
    the worker killed during — gunicorn boot. The sequence is: single-flight
    auto-login → start the repeating tick scheduler → fire the FIRST tick
    immediately, but only on the worker that just won the login (so we never kick
    off a second concurrent TOTP login). All steps no-op without credentials/DB
    and never raise.
    """
    if not settings.fyers_autologin_on_startup:
        return
    if not settings.effective_dsn:
        app.logger.info("Fyers startup login skipped — no database configured")
        return
    import threading

    threading.Thread(target=_startup_sequence, name="fyers-warm", daemon=True).start()


def _startup_sequence() -> None:
    """Login → scheduler → immediate first tick. Lazy imports keep the Fyers SDK
    out of import time; never raises (a daemon thread crashing is silent)."""
    log = logging.getLogger(__name__)
    from auth.startup import warm_login_once

    token = warm_login_once()  # single-flight; "" unless THIS worker secured the token

    if not settings.scheduler_enabled:
        return
    try:
        from app.scheduler import start_scheduler

        start_scheduler()  # repeating job (first fire one interval out), every worker
        # Immediate capture, only on the login winner — its token is fresh, so
        # run_tick reuses it instead of racing a parallel login.
        if token:
            from market.tick import scheduled_tick

            res = scheduled_tick(
                strikecount=settings.tick_strikecount,
                window_minutes=settings.tick_window_minutes,
            )
            log.info("startup kick-off tick: %s", res)
    except Exception as exc:  # scheduler/tick must never take down the boot thread
        log.warning("tick scheduler startup error: %s", exc)
