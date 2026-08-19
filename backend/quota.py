"""Daily extraction budget per provider -- a circuit breaker in front of the AI.

WHY THIS EXISTS SEPARATELY FROM THE RATE LIMITER

They stop different failures. `ratelimit.py` stops a runaway script: twenty
requests in one second. This stops something quieter and, for this system, worse
-- steady, entirely reasonable-looking use exhausting a provider for the rest of
the day. Gemini's free tier is 20 requests per DAY, and it is the only route
that can read a scanned invoice. Twenty polite requests spread an hour apart
never trip a per-minute limit and still leave the process unable to read a scan
by lunchtime, with no signal that anything is wrong.

WHAT "FAIL SAFELY" MEANS HERE

When the budget is spent, extraction does NOT call the provider. It takes the
route it already takes when a provider is unreachable: a text PDF falls back to
regex, and a scan goes to route "none" and therefore to a human. No new decision
semantics, no new verdict, nothing auto-approved that would not have been -- the
same well-tested paths a 503 takes, reached without spending a request to
discover the obvious.

WHY THE COUNTER IS IN THE DATABASE

The budget is a calendar-day quantity, so it has to survive a restart. An
in-process counter would reset every time uvicorn reloaded and hand out a fresh
20 requests, which is precisely the accounting error the breaker exists to
prevent. It shares the app's SQLite file and its write transaction, so two
concurrent runs cannot both consume the last request.

Counting is by UTC day, matching how the providers reckon their own quotas
rather than the operator's local midnight.
"""
import sqlite3
import sys
from datetime import datetime, timezone

import config
import storage

VISION = "gemini-vision"
TEXT = "groq-text"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def limit_for(provider: str) -> int:
    return config.DAILY_QUOTA_VISION if provider == VISION else config.DAILY_QUOTA_TEXT


def _ensure_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS extraction_quota (
            day TEXT NOT NULL,
            provider TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, provider)
        )"""
    )


def used(provider: str, day: str = None) -> int:
    """Requests already spent today. 0 if the store cannot be read."""
    try:
        conn = storage.get_conn()
        try:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT used FROM extraction_quota WHERE day=? AND provider=?",
                (day or _today(), provider)).fetchone()
            return row["used"] if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def status(provider: str) -> dict:
    u, lim = used(provider), limit_for(provider)
    return {"provider": provider, "used": u, "limit": lim,
            "remaining": max(0, lim - u), "day": _today(),
            "enabled": config.DAILY_QUOTA_ENABLED}


def try_consume(provider: str) -> bool:
    """Reserve one request against today's budget. True if the call may proceed.

    Read and increment happen inside one write transaction, so the last request
    of the day cannot be handed to two concurrent runs.

    A failure of the counter itself is deliberately FAIL-OPEN: it returns True
    and logs. This is a cost guard, not a security control -- refusing to
    process invoices because a bookkeeping table would not open would be a
    self-inflicted outage, and the provider still enforces its own quota
    underneath. The security controls (authentication, authorization) all fail
    closed; this one does not, and the difference is intentional.
    """
    if not config.DAILY_QUOTA_ENABLED:
        return True

    limit = limit_for(provider)
    if limit <= 0:
        return False        # a budget of zero disables the provider outright

    day = _today()
    try:
        with storage.write_txn() as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT used FROM extraction_quota WHERE day=? AND provider=?",
                (day, provider)).fetchone()
            spent = row["used"] if row else 0
            if spent >= limit:
                return False
            conn.execute(
                """INSERT INTO extraction_quota (day, provider, used) VALUES (?,?,1)
                   ON CONFLICT(day, provider) DO UPDATE SET used = used + 1""",
                (day, provider))
            return True
    except Exception as exc:
        print(f"[quota] counter unavailable ({exc.__class__.__name__}); allowing the call",
              file=sys.stderr)
        return True


def exhausted_note(provider: str) -> str:
    """The line shown in the run trail when the breaker is open.

    Says what happened and what it means for this invoice, without implying the
    invoice did anything wrong -- the budget is a property of the day, not of
    the document.
    """
    label = "Vision" if provider == VISION else "Text"
    limit = limit_for(provider)
    if provider == VISION:
        consequence = ("This scanned invoice was not read, and no fields were guessed at. "
                       "It is held for a human.")
    else:
        consequence = "Fell back to the built-in pattern extractor for this invoice."
    return (f"{label} extraction skipped: the daily budget of {limit} request(s) is "
            f"already spent, so the provider was not called. {consequence}")
