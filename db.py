"""Postgres access for the hosted app: schema, accounts, sessions, and credits.

  python db.py migrate      # create or update tables (safe to re-run)

Every credit change is a single conditional UPDATE, so two tailors started at
the same moment can never spend the same credit, and a credit is only kept
when the tailor it was reserved for succeeds.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

FREE_TAILORS = 5
SUBSCRIPTION_TAILORS = 60  # per billing period
IMPORTS_PER_DAY = 10
STALE_RESERVATION_MINUTES = 20  # well past the function's 300 s limit

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                  BIGSERIAL PRIMARY KEY,
    email               TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    free_used           INT NOT NULL DEFAULT 0,
    pack_credits        INT NOT NULL DEFAULT 0,
    stripe_customer_id  TEXT UNIQUE,
    sub_id              TEXT,
    sub_status          TEXT,
    sub_period_end      TIMESTAMPTZ,
    sub_used            INT NOT NULL DEFAULT 0,
    billing_hold        BOOLEAN NOT NULL DEFAULT false,
    failed_logins       INT NOT NULL DEFAULT 0,
    locked_until        TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,           -- tailor | import
    source      TEXT,                    -- free | sub | pack (tailors only)
    status      TEXT NOT NULL,           -- reserved | charged | refunded
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_user_kind_time ON usage (user_id, kind, created_at);
CREATE TABLE IF NOT EXISTS pack_purchases (
    checkout_session_id  TEXT PRIMARY KEY,
    user_id              BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_intent_id    TEXT,
    credits              INT NOT NULL,
    refunded             BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS stripe_events (
    id           TEXT PRIMARY KEY,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One account per network, ever. Kept when the account is deleted, or
-- deleting and re-creating would hand out a fresh free tier.
CREATE TABLE IF NOT EXISTS signup_networks (
    net_hash    TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
DROP TABLE IF EXISTS signups;
-- Every DeepSeek call, so spend can be traced to a user and a tailor/import.
-- Kept (with user_id cleared) when an account is deleted, for cost accounting.
CREATE TABLE IF NOT EXISTS api_calls (
    id                 BIGSERIAL PRIMARY KEY,
    user_id            BIGINT REFERENCES users(id) ON DELETE SET NULL,
    usage_id           BIGINT REFERENCES usage(id) ON DELETE SET NULL,
    kind               TEXT NOT NULL,           -- tailor | import
    model              TEXT NOT NULL,
    ok                 BOOLEAN NOT NULL,
    error              TEXT,
    prompt_tokens      INT NOT NULL DEFAULT 0,
    cached_tokens      INT NOT NULL DEFAULT 0,
    completion_tokens  INT NOT NULL DEFAULT 0,
    reasoning_tokens   INT NOT NULL DEFAULT 0,
    cost_usd           NUMERIC(10, 6) NOT NULL DEFAULT 0,
    duration_ms        INT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_calls_user_time ON api_calls (user_id, created_at);
-- One row per user: how many API calls they caused, what they cost, and how
-- many tailors they were actually charged for.
CREATE OR REPLACE VIEW api_usage_by_user AS
SELECT
    c.user_id,
    u.email,
    count(*)                                      AS api_calls,
    count(*) FILTER (WHERE c.kind = 'tailor')     AS tailor_calls,
    count(*) FILTER (WHERE c.kind = 'import')     AS import_calls,
    count(*) FILTER (WHERE NOT c.ok)              AS failed_calls,
    count(DISTINCT c.usage_id) FILTER (WHERE c.kind = 'tailor') AS tailor_runs,
    (SELECT count(*) FROM usage x WHERE x.user_id = c.user_id
        AND x.kind = 'tailor' AND x.status = 'charged') AS tailors_charged,
    sum(c.prompt_tokens)                          AS prompt_tokens,
    sum(c.completion_tokens)                      AS completion_tokens,
    sum(c.cost_usd)                               AS cost_usd,
    max(c.created_at)                             AS last_call
FROM api_calls c
LEFT JOIN users u ON u.id = c.user_id
GROUP BY c.user_id, u.email;
ALTER TABLE users ADD COLUMN IF NOT EXISTS sub_cancel_at_period_end BOOLEAN NOT NULL DEFAULT false;
"""


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    # Local development: `vercel env pull` writes the connection string here.
    env_file = Path(__file__).resolve().parent / ".env.local"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "DATABASE_URL":
                return value.strip().strip("\"'")
    raise RuntimeError("DATABASE_URL is not set")


@contextmanager
def connect():
    """One transaction: commits on success, rolls back on any exception."""
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        yield conn


# ---- credits ---------------------------------------------------------------

# Spend order: free uses first, then the subscription allowance (it resets each
# period, so unused allowance is lost), then packs (they never expire).
SUB_ACTIVE = (
    "sub_status IN ('active', 'trialing') AND sub_period_end > now() AND NOT billing_hold"
)
RESERVE = {
    "free": f"UPDATE users SET free_used = free_used + 1 WHERE id = %s AND free_used < {FREE_TAILORS}",
    "sub": (
        f"UPDATE users SET sub_used = sub_used + 1 WHERE id = %s AND {SUB_ACTIVE} "
        f"AND sub_used < {SUBSCRIPTION_TAILORS}"
    ),
    "pack": "UPDATE users SET pack_credits = pack_credits - 1 WHERE id = %s "
            "AND pack_credits > 0 AND NOT billing_hold",
}
REFUND = {
    "free": "UPDATE users SET free_used = greatest(free_used - 1, 0) WHERE id = %s",
    "sub": "UPDATE users SET sub_used = greatest(sub_used - 1, 0) WHERE id = %s",
    "pack": "UPDATE users SET pack_credits = pack_credits + 1 WHERE id = %s",
}


def reserve_tailor(user_id: int) -> int | None:
    """Take one tailor credit. Returns a usage id, or None when none is left."""
    with connect() as conn:
        # A function instance can be frozen before it settles a reservation;
        # anything still reserved long after the request limit is returned.
        stale = conn.execute(
            "UPDATE usage SET status = 'refunded' WHERE user_id = %s AND kind = 'tailor' "
            f"AND status = 'reserved' AND created_at < now() - interval '{STALE_RESERVATION_MINUTES} minutes' "
            "RETURNING source",
            (user_id,),
        ).fetchall()
        for row in stale:
            conn.execute(REFUND[row["source"]], (user_id,))
        for source, sql in RESERVE.items():
            if conn.execute(sql, (user_id,)).rowcount:
                row = conn.execute(
                    "INSERT INTO usage (user_id, kind, source, status) "
                    "VALUES (%s, 'tailor', %s, 'reserved') RETURNING id",
                    (user_id, source),
                ).fetchone()
                return row["id"]
    return None


def settle_tailor(usage_id: int, succeeded: bool) -> None:
    """Keep the credit if the tailor produced documents; give it back if not."""
    with connect() as conn:
        row = conn.execute(
            "UPDATE usage SET status = %s WHERE id = %s AND status = 'reserved' "
            "RETURNING user_id, source",
            ("charged" if succeeded else "refunded", usage_id),
        ).fetchone()
        if row and not succeeded:
            conn.execute(REFUND[row["source"]], (row["user_id"],))


def record_import(user_id: int) -> int | None:
    """Count one resume import; returns its usage id, or None once today's
    limit is reached."""
    with connect() as conn:
        # Lock the user's row so parallel imports cannot both pass the count.
        conn.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
        used = conn.execute(
            "SELECT count(*) AS n FROM usage WHERE user_id = %s AND kind = 'import' "
            "AND created_at > now() - interval '1 day'",
            (user_id,),
        ).fetchone()["n"]
        if used >= IMPORTS_PER_DAY:
            return None
        return conn.execute(
            "INSERT INTO usage (user_id, kind, status) VALUES (%s, 'import', 'charged') "
            "RETURNING id",
            (user_id,),
        ).fetchone()["id"]


def record_api_call(user_id: int, usage_id: int | None, kind: str, call: dict) -> None:
    """Log one model call (a record from tailormade.run_model)."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO api_calls (user_id, usage_id, kind, model, ok, error, prompt_tokens, "
            "cached_tokens, completion_tokens, reasoning_tokens, cost_usd, duration_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (user_id, usage_id, kind, call["model"], call["ok"], call["error"],
             call["prompt_tokens"], call["cached_tokens"], call["completion_tokens"],
             call["reasoning_tokens"], call["cost_usd"], call["duration_ms"]),
        )


def balance(user: dict) -> dict:
    # Mirrors SUB_ACTIVE so the page never shows allowance the database refuses.
    sub_active = (
        user["sub_status"] in ("active", "trialing")
        and user["sub_period_end"] is not None
        and user["sub_period_end"] > datetime.now(timezone.utc)
        and not user["billing_hold"]
    )
    return {
        "email": user["email"],
        "free_left": max(FREE_TAILORS - user["free_used"], 0),
        "pack_credits": user["pack_credits"],
        "subscription": {
            "active": bool(sub_active),
            "status": user["sub_status"],
            "left": max(SUBSCRIPTION_TAILORS - user["sub_used"], 0) if sub_active else 0,
            "renews": user["sub_period_end"].isoformat() if sub_active else None,
            "cancel_at_period_end": user["sub_cancel_at_period_end"],
        },
        "billing_hold": user["billing_hold"],
        "has_billing_account": user["stripe_customer_id"] is not None,
    }


def migrate() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)


if __name__ == "__main__":
    if sys.argv[1:] == ["migrate"]:
        migrate()
        print("Schema is up to date.")
    else:
        sys.exit("usage: python db.py migrate")
