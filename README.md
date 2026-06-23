# COBRA

Flask + Pydantic API over Supabase Postgres. Typed config, validated I/O, and a
psycopg3 connection pool tuned for a concurrent server.

## Layout

```
config/      pydantic-settings — typed, validated env (config.settings.settings)
db/          psycopg3 connection pool to Supabase Postgres
  schema.sql       fyers_tokens (token cache) — the only table
schemas/     pydantic v2 base model (ApiModel) — scaffold for future endpoints
auth/        Fyers broker auto-login (TOTP); tokens persisted in Postgres
app/         Flask app factory, error handlers, health blueprint
  validation.py  parse_body() — Flask↔Pydantic request-body helper
main.py      dev entrypoint  (uv run main.py)
wsgi.py      prod entrypoint (gunicorn wsgi:app)
```

## Authentication — Fyers broker auto-login

Automated 5-step TOTP+PIN login to the Fyers trading API, replicated from the
Analyzer project. Strategy, in order: reuse cached token (< 22h old) →
refresh-token fast path → full TOTP login. Credentials live in `.env`; the
issued **access/refresh tokens are persisted in the `fyers_tokens` Postgres
table** (one row per `client_id`) rather than on disk — `auth.save_token()`
upserts and `auth.load_token()` reads it.

```python
from auth import get_fyers_token, get_fyers_client
token  = get_fyers_token()        # cached → refresh → full TOTP, persisted to DB
client = get_fyers_client()       # ready-to-use FyersModel

from auth.force_login import force_full_login
force_full_login()                # bypass cache/refresh; full TOTP, then persist
```

**Retries:** two layers — per-request network retries (`_post_with_retry`, on
`ConnectionError`/`Timeout`) and a flow-level retry that re-runs the whole 5-step
login (`FYERS_LOGIN_MAX_ATTEMPTS` × `FYERS_LOGIN_RETRY_DELAY`s) on a failed
attempt. Missing credentials never retries.

**On startup:** `create_app()` warms the token automatically
(`FYERS_AUTOLOGIN_ON_STARTUP=true`). Under multiple gunicorn workers, a Postgres
**advisory lock** ensures only one worker performs the login; the rest skip and
read the token from the DB. It no-ops when credentials are unset and never blocks
the server from booting on a Fyers/DB error. Set `FYERS_AUTOLOGIN_ON_STARTUP=false`
to disable.

## Why these choices

- **pydantic-settings** — config is typed and validated once at startup; a bad
  value fails fast with a clear error instead of a late `AttributeError`.
- **psycopg3 + connection pool** — the TCP/TLS/auth handshake is paid once per
  pooled connection, not per request. Min/max size, idle timeout, and statement
  timeout are configurable. The Supabase transaction pooler (`:6543`) is
  detected automatically and prepared statements are disabled for it.
- **App factory + blueprints** — each worker builds its own app and DB pool,
  so it scales cleanly under gunicorn.

## Setup

This project lives on an exFAT USB, which can't hold a venv (no symlinks), so the
venv lives on the local ext4 disk. Point uv at it:

```bash
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/cobra"

uv sync --extra dev          # install deps into the ext4 venv
cp .env.example .env         # then fill in your Supabase DATABASE_URL
```

Apply the schema to Supabase (SQL Editor, or psql):

```bash
psql "$DATABASE_URL" -f db/schema.sql
```

## Run

```bash
# dev (auto-reload)
uv run main.py

# production — each worker opens its own pool; do NOT use --preload
uv run gunicorn -w 4 -b 0.0.0.0:8000 wsgi:app
```

## Endpoints

| Method | Path          | Notes                        |
|--------|---------------|------------------------------|
| GET    | `/`           | service info                 |
| GET    | `/health`     | liveness (no DB)             |
| GET    | `/health/db`  | DB readiness (503 if down)   |

No business endpoints yet — add resource blueprints under `app/api/` (subclass
`schemas.ApiModel`, validate with `app.validation.parse_body`) as the server grows.

## Test

```bash
uv run pytest          # health + validation tests run without a database
```
