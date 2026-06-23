"""v3 — internal scheduler (self-driving, Render-friendly).

Three jobs, all on one BackgroundScheduler per worker (timezone = IST):

  1. tick      — Mon–Fri, every `tick_interval_minutes`, **only within the
                 09–15 hour window** (cron-bounded → the scheduler is "closed"
                 for ticks at night / on weekends). The precise 09:15–15:30 edges
                 are enforced by is_market_hours() inside scheduled_tick.
  2. pre-login — Mon–Fri at 09:13 IST: warm the Fyers token so the first capture
                 at 09:15 already has a valid token.
  3. keep-alive — every `keepalive_interval_minutes`, pings our OWN public
                 /health so a free Render web service never spins down (its own
                 scheduler can't wake a slept process). Runs 24/7.

Single-flight across gunicorn workers is handled downstream: scheduled_tick takes
a Postgres advisory lock, and warm_login_once takes the login lock.
"""
from __future__ import annotations

import atexit
import logging
from zoneinfo import ZoneInfo

import requests

from config.settings import settings

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_scheduler = None  # one BackgroundScheduler per worker process


def _tick_job() -> None:
    from market.tick import scheduled_tick

    res = scheduled_tick(
        strikecount=settings.tick_strikecount,
        window_minutes=settings.tick_window_minutes,
    )
    logger.info("scheduled tick: %s", res)


def _prelogin_job() -> None:
    """Warm the Fyers token before the open (single-flight via the login lock)."""
    from auth.startup import warm_login_once

    token = warm_login_once()
    logger.info("pre-open Fyers login: %s", "token ready" if token else "skipped/failed")


def _keepalive_job() -> None:
    """Ping our own public /health so Render doesn't spin the service down."""
    url = settings.keepalive_target
    if not url:
        return
    target = url.rstrip("/") + "/health"
    try:
        resp = requests.get(target, timeout=10)
        logger.debug("keepalive ping %s -> %s", target, resp.status_code)
    except Exception as exc:  # never let the keep-alive job die
        logger.warning("keepalive ping failed (%s): %s", target, exc)


def start_scheduler():
    """Start the tick + pre-login + keep-alive jobs (idempotent per process)."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BackgroundScheduler(timezone=IST)

    # 1) Ticks — Mon–Fri, every N min, only in the 09–15 hour band (closed
    #    outside). is_market_hours() trims the 09:15 / 15:30 edges precisely.
    sched.add_job(
        _tick_job,
        CronTrigger(day_of_week="mon-fri", hour="9-15",
                    minute=f"*/{settings.tick_interval_minutes}", timezone=IST),
        id="cobra-tick", max_instances=1, coalesce=True, misfire_grace_time=120,
    )

    # 2) Pre-login at HH:MM IST (default 09:13) so the token is ready by 09:15.
    sched.add_job(
        _prelogin_job,
        CronTrigger(day_of_week="mon-fri", hour=settings.prelogin_hour,
                    minute=settings.prelogin_minute, timezone=IST),
        id="cobra-prelogin", max_instances=1,
    )

    # 3) Keep-alive — ping our own /health every N min (24/7) so a free Render
    #    web service stays warm. No-ops without a URL.
    if settings.keepalive_enabled and settings.keepalive_target:
        sched.add_job(
            _keepalive_job, "interval",
            minutes=settings.keepalive_interval_minutes,
            id="cobra-keepalive", max_instances=1, coalesce=True,
        )
        logger.info(
            "keep-alive on: ping %s/health every %d min",
            settings.keepalive_target.rstrip("/"), settings.keepalive_interval_minutes,
        )

    sched.start()
    atexit.register(lambda: sched.shutdown(wait=False))
    _scheduler = sched
    logger.info(
        "scheduler started — ticks Mon–Fri 09:15–15:30 IST every %d min; "
        "pre-login %02d:%02d IST",
        settings.tick_interval_minutes, settings.prelogin_hour, settings.prelogin_minute,
    )
    return sched
