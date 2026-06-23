"""Liveness + DB readiness probes."""
from __future__ import annotations

from flask import Blueprint, jsonify

from config.settings import settings
from db.db import healthcheck

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    """Liveness — process is up. No DB dependency."""
    return jsonify(status="ok", app=settings.app_name, env=settings.env)


@bp.get("/health/db")
def health_db():
    """Readiness — can we reach Supabase Postgres?"""
    if not settings.effective_dsn:
        return jsonify(status="unconfigured", detail="DATABASE_URL not set"), 503
    try:
        ok = healthcheck()
    except Exception as exc:
        return jsonify(status="down", detail=str(exc)), 503
    return jsonify(status="up" if ok else "down"), (200 if ok else 503)
