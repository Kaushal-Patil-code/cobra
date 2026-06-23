"""
Supabase Postgres access via a psycopg3 connection pool.

A single process-wide ConnectionPool is created lazily and reused across
requests. This is the main DB optimization for a concurrent Flask server:
the TCP + TLS + auth handshake is paid once per pooled connection instead of
on every query, and checkouts are O(1).

Usage:

    from db import get_conn

    with get_conn() as conn:                  # checked out from the pool
        with conn.cursor() as cur:
            cur.execute("select 1")
            row = cur.fetchone()              # rows are dicts (dict_row)
    # connection returned to the pool; transaction committed on clean exit,
    # rolled back if an exception propagated out of the block.

Lifecycle: call open_pool() at app startup and close_pool() at shutdown
(wired up in app.create_app). config.settings.effective_dsn is the single
source of truth for the connection string.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config.settings import settings

logger = logging.getLogger(__name__)

_pool: Optional[ConnectionPool] = None
_pool_pid: Optional[int] = None  # PID that opened _pool — for fork-safety


def _is_pooler(dsn: str) -> bool:
    """True for Supabase's transaction-mode pooler (pgBouncer on :6543).

    Transaction pooling multiplexes server backends per-transaction, which
    breaks both server-side prepared statements and session-level SETs.
    """
    d = dsn.lower()
    return ":6543" in d or "pgbouncer=true" in d


def _prepare_threshold(dsn: str) -> Optional[int]:
    if settings.db_disable_prepared_statements or _is_pooler(dsn):
        return None  # disable server-side prepared statements
    return settings.db_prepare_threshold


def _make_configure(dsn: str):
    """Per-connection setup run once when a physical connection is created."""
    pooler = _is_pooler(dsn)

    def configure(conn: psycopg.Connection) -> None:
        if pooler:
            return  # a session SET would not persist across pooled transactions
        with conn.cursor() as cur:
            # set_config(..., is_local=false) → session scope; parameterizable,
            # unlike `SET statement_timeout = <literal>`.
            cur.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(settings.db_statement_timeout_ms),),
            )
        conn.commit()

    return configure


def _build_pool() -> ConnectionPool:
    dsn = settings.effective_dsn
    if not dsn:
        raise RuntimeError(
            "No database connection configured. Set DATABASE_URL (or "
            "DB_HOST/DB_USER/DB_PASSWORD) in .env — see .env.example."
        )
    return ConnectionPool(
        conninfo=dsn,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        max_idle=settings.db_pool_max_idle,
        timeout=settings.db_connect_timeout,  # max wait to check out a conn
        name="cobra-pool",
        open=False,  # opened explicitly via open_pool()
        configure=_make_configure(dsn),
        # Validate a connection on checkout and recycle dead ones. A Supabase
        # pooler can close/rotate the underlying server connection between uses,
        # leaving a pooled conn stale (→ SSL "bad record mac" / "unexpected eof");
        # this transparently discards it and hands back a fresh one.
        check=ConnectionPool.check_connection,
        kwargs={
            "row_factory": dict_row,
            "connect_timeout": settings.db_connect_timeout,
            "prepare_threshold": _prepare_threshold(dsn),
            "application_name": settings.app_name,
        },
    )


def get_pool() -> ConnectionPool:
    global _pool, _pool_pid
    # Fork-safety: a pool opened BEFORE a fork (e.g. gunicorn --preload, or any
    # pre-fork DB use) has live sockets owned by the PARENT process. Inheriting
    # and using them from a worker means two processes share one SSL connection →
    # "decryption failed / bad record mac" / "unexpected eof". If the PID changed,
    # abandon the inherited pool (do NOT close it — those FDs belong to the parent)
    # and build a fresh pool for THIS process.
    if _pool is not None and _pool_pid != os.getpid():
        logger.warning("DB pool inherited across a fork — rebuilding for pid %d", os.getpid())
        _pool = None
        _pool_pid = None
    if _pool is None:
        pool = _build_pool()
        # Open with wait so the first use — Flask startup OR a standalone script
        # (scheduler / Fyers login) — warms min_size connections and fails fast
        # if the DB is unreachable. On failure, leave _pool unset so a later call
        # can retry cleanly rather than handing back a dead pool.
        try:
            pool.open(wait=True, timeout=settings.db_connect_timeout)
        except Exception:
            pool.close()
            raise
        _pool = pool
        _pool_pid = os.getpid()
    return _pool


def open_pool() -> None:
    """Open + warm the pool (also happens lazily on first get_conn). Call at startup."""
    get_pool()
    logger.info(
        "DB pool open (min=%d max=%d prepared=%s)",
        settings.db_pool_min_size,
        settings.db_pool_max_size,
        _prepare_threshold(settings.effective_dsn or "") is not None,
    )


def close_pool() -> None:
    """Close the pool and all its connections. Call at shutdown."""
    global _pool, _pool_pid
    if _pool is not None:
        _pool.close()
        _pool = None
        _pool_pid = None
        logger.info("DB pool closed")


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """Check out a pooled connection as a context manager.

    The transaction is committed on a clean exit, rolled back if an exception
    propagates out of the block.
    """
    with get_pool().connection() as conn:
        yield conn


def healthcheck() -> bool:
    """Return True if a trivial round-trip to Postgres succeeds."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        row = cur.fetchone()
        return bool(row and row.get("ok") == 1)
