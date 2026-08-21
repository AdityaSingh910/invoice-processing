"""PostgreSQL persistence: seed data (POs/vendors) + run history / dashboard.

MIGRATED FROM SQLITE. The table shapes, column names and business semantics
below are unchanged from the SQLite version -- this migration is a dialect and
connection-management change, not a schema redesign. Every docstring that
explained a business decision (why allocations are derived, why the audit
trail lives beside the run, why reversal needs no refund step) is preserved
verbatim, because none of those decisions changed.

WHAT DID CHANGE, AND WHY

* `?` placeholders -> `%s` (psycopg2's paramstyle).
* `sqlite3.Row` -> `psycopg2.extras.RealDictCursor`. Both give dict-like row
  access (`row["col"]`) and both support `dict(row)`, so every existing
  `dict(r)` call site in this file is unaffected.
* `cur.lastrowid` -> `INSERT ... RETURNING id`, fetched from the cursor.
  psycopg2 has no `lastrowid`.
* `INTEGER PRIMARY KEY AUTOINCREMENT` -> `SERIAL PRIMARY KEY`.
* `PRAGMA table_info(...)` -> a query against `information_schema.columns`,
  scoped to the active schema (see PG_SCHEMA below).
* `BEGIN IMMEDIATE` (a database-wide write lock taken up front) is replaced by
  a per-PO `SELECT ... FOR UPDATE` at the exact point a balance is decided and
  committed (`save_run_checked`). This is not a weaker guarantee -- it is a
  MORE PRECISE one: two invoices racing the SAME PO still serialise exactly as
  before, but invoices against DIFFERENT POs no longer block each other at
  all, which a single SQLite file could never do. See `write_txn` below.
* Connection pooling: `get_conn()` used to open a new SQLite file handle per
  call, which is nearly free. A TCP+auth round trip to Postgres is not, and
  this file calls `get_conn()` on nearly every function, so a small pool sits
  behind it. Every call site is unchanged (`conn = get_conn(); ...;
  conn.close()`) -- see the pooling note on `get_conn()` for how that stays
  true while the connection is actually being returned to the pool.

WHAT DID NOT CHANGE

Every table, every column, every derivation (balances are still summed from
`run_allocations` joined to `runs.status='APPROVED'`, never stored), every
function signature, every return shape. The `users` table does not exist here
before or after this migration -- authentication reads `data/users.json`
directly (see auth.py) and was never part of this database.
"""
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import psycopg2.pool

import config
import documents

PO_SEED = os.path.join(os.path.dirname(__file__), "..", "data", "purchase_orders.json")
VENDOR_SEED = os.path.join(os.path.dirname(__file__), "..", "data", "approved_vendors.json")
# Phase F. Same treatment as the two above: reference data the business owns,
# reloaded from JSON on every startup rather than edited through the API.
TRUSTED_SENDER_SEED = os.path.join(os.path.dirname(__file__), "..", "data",
                                   "trusted_email_senders.json")

# --------------------------------------------------------------------------
# connection target
#
# Both are module-level and deliberately monkeypatchable, mirroring the old
# `storage.DB_PATH` -- test fixtures used to repoint that at a fresh SQLite
# file per test; they now repoint PG_SCHEMA at a fresh, uniquely-named schema
# inside one shared Postgres database, which is the closest true analogue
# ("one isolated namespace per test") that a shared server can offer. Every
# test fixture's edit is therefore one line, not a rewrite.
# --------------------------------------------------------------------------
DATABASE_URL = None   # None => read from config.database_url() at connect time
PG_SCHEMA = "public"

_POOL = None
_POOL_DSN = None


def _dsn() -> str:
    return DATABASE_URL or config.database_url()


def _pool() -> "psycopg2.pool.ThreadedConnectionPool":
    """The connection pool for the current DSN, created lazily.

    Rebuilt if the DSN changes (a test process could in principle point at a
    different DATABASE_URL mid-run, though nothing here does that today) --
    checked on every call rather than cached forever, so a stale pool bound to
    the wrong database is not silently reused.
    """
    global _POOL, _POOL_DSN
    dsn = _dsn()
    if _POOL is None or _POOL_DSN != dsn:
        if _POOL is not None:
            try:
                _POOL.closeall()
            except Exception:
                pass
        _POOL = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=dsn)
        _POOL_DSN = dsn
    return _POOL


class _PooledConnection:
    """Thin proxy around a pooled psycopg2 connection.

    POOLING, WITHOUT CHANGING A SINGLE CALL SITE

    Every function in this file follows `conn = get_conn(); ...; conn.close()`
    -- that pattern is everywhere, and rewriting every call site to a
    pool-aware `try/finally` would touch nearly the whole module for no
    behavioural gain. The natural fix is to rebind `.close()` on the returned
    connection so it returns the connection to the pool instead of tearing
    down the socket -- but psycopg2's connection is a C extension type and
    refuses instance-level attribute assignment for its own methods
    (`AttributeError: attribute 'close' is read-only`). This proxy exists
    solely to make that rebinding possible: everything except `close()` is
    forwarded straight through via `__getattr__`/`__setattr__`, so
    `conn.cursor()`, `conn.commit()`, `conn.autocommit = True` and so on all
    behave exactly as they would on the real connection.
    """
    __slots__ = ("_conn", "_pool")

    def __init__(self, conn, pool):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_pool", pool)

    def close(self):
        self._pool.putconn(self._conn)

    def execute(self, query, params=None):
        """sqlite3.Connection had this as a convenience; psycopg2's does not.
        A handful of test fixtures use it directly against a raw connection
        for a one-off INSERT/DELETE, so it is reproduced here rather than
        asking every one of those fixtures to open its own cursor for a
        single statement."""
        cur = self._conn.cursor()
        cur.execute(query, params)
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


def get_conn():
    """A connection scoped to PG_SCHEMA, dict-row cursors by default.

    Backed by a small pool (see `_PooledConnection`): `.close()` returns the
    connection to the pool rather than closing the socket, so every existing
    `conn = get_conn(); ...; conn.close()` call site in this file needed no
    change at all.
    """
    pool = _pool()
    raw = pool.getconn()
    raw.cursor_factory = psycopg2.extras.RealDictCursor
    raw.autocommit = True   # matches sqlite3's default; write_txn() turns it off
    with raw.cursor() as cur:
        cur.execute(f'SET search_path TO "{PG_SCHEMA}"')
    return _PooledConnection(raw, pool)


@contextmanager
def write_txn():
    """A serialised read-modify-write against the PO ledger.

    SQLite used `BEGIN IMMEDIATE` to take a write lock over the WHOLE database
    at the start of the transaction, because SQLite has no row-level locking.
    Postgres does, so this context manager only turns off autocommit; the
    actual serialisation now happens at the specific row a caller locks with
    `SELECT ... FOR UPDATE` (see `save_run_checked`, which locks exactly the
    purchase_orders row(s) an invoice is being charged against).

    The property this exists to protect -- the balance check and the write
    that consumes the balance happen inside one atomic unit, so two invoices
    for the same PO cannot both read the same remaining balance and both
    approve -- is unchanged. What changed is the GRANULARITY of the lock: a
    row, not the database. Two invoices against DIFFERENT POs no longer
    serialise against each other at all, which SQLite could never offer.
    """
    conn = get_conn()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_columns(conn, table, columns: dict):
    """Add any missing columns to an existing table.

    `CREATE TABLE IF NOT EXISTS` is a no-op against a database that already
    exists, so a new column would silently never appear on anyone's working
    database. Postgres has no `PRAGMA table_info`; the existing columns are
    read from `information_schema.columns`, scoped to the active schema so a
    same-named table in a different test schema is never consulted by
    mistake.
    """
    # psycopg2 connections have no .execute() of their own (unlike sqlite3.Connection);
    # every other call in this module already goes through a cursor, so this
    # helper does too.
    with conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = %s AND table_name = %s""",
            (PG_SCHEMA, table))
        have = {r["column_name"] for r in cur.fetchall()}
        for name, decl in columns.items():
            if name not in have:
                cur.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')


def _consumed(conn, po_number, exclude_run_id=None):
    """Balance consumed on a PO, read on a caller-supplied connection.

    Deliberately derived from run history rather than read off a stored counter.
    Nothing to deduct means nothing to deduct twice, so re-evaluating a run can
    never double-count it, and reversing one refunds the balance by definition.

    WHY THIS SUMS ALLOCATIONS RATHER THAN RUN TOTALS

    This used to read `SUM(total) FROM runs WHERE po_number=?`, which silently
    assumed one PO per invoice: the run's WHOLE total was charged to whichever
    PO happened to be bound. An invoice covering two POs therefore over-consumed
    the first by the value of the second and never touched the second at all --
    measured, before this change, as PO-1001 dropping to -$5,000 remaining while
    PO-1002 stayed untouched at $5,000.

    `run_allocations` records how much of a run went to WHICH PO, so the sum is
    per-PO rather than per-run. The derived-ledger property is unchanged and is
    the reason this stays safe: the join is on `runs.status='APPROVED'`, so
    allocation rows count only while their run is approved. Nothing is deducted,
    so nothing can be deducted twice, and moving a run out of APPROVED still
    refunds every PO it touched in the same instant.
    """
    q = """SELECT COALESCE(SUM(a.amount), 0) AS c
           FROM run_allocations a JOIN runs r ON r.id = a.run_id
           WHERE a.po_number = %s AND r.status = 'APPROVED'"""
    params = [po_number]
    if exclude_run_id is not None:
        q += " AND r.id != %s"
        params.append(exclude_run_id)
    with conn.cursor() as cur:
        cur.execute(q, params)
        return float(cur.fetchone()["c"] or 0.0)


def consumed_amounts_by_po():
    """`_consumed` for every PO at once: {po_number: consumed}.

    THE SAME LEDGER RULE, NOT A SECOND ONE. `_consumed` above answers the
    question for one PO because that is what committing a run needs; an
    analytics table needs the answer for all of them, and asking `_consumed`
    once per PO is a query per row. So the rule -- sum `run_allocations`,
    joined to `runs.status = 'APPROVED'` -- is expressed once more, set-based,
    and it lives HERE, immediately beside its single-PO sibling, so the two
    cannot be edited apart by someone who only found one of them.

    That adjacency is a convention, not a guarantee, so it is backed by a test:
    `test_analytics.py::test_set_based_ledger_matches_the_per_po_ledger` asserts
    this function and `consumed_amount_for_po()` agree for every PO on a
    database with multi-PO invoices, reversals and rejections in it. If someone
    changes what "consumed" means in one place and not the other, that test
    fails rather than a dashboard quietly disagreeing with the PO screen.

    POs with no approved allocations are absent from the result rather than
    present as 0.0 -- callers use `.get(po, 0.0)`, which keeps this a pure
    projection of the allocation rows instead of a list that also has to know
    which POs exist.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.po_number, COALESCE(SUM(a.amount), 0) AS c
                   FROM run_allocations a JOIN runs r ON r.id = a.run_id
                   WHERE r.status = 'APPROVED'
                   GROUP BY a.po_number""")
            return {r["po_number"]: float(r["c"] or 0.0) for r in cur.fetchall()}
    finally:
        conn.close()


def _write_allocations(conn, run_id, allocations):
    """Record which POs a run's total was charged against, and how much to each.

    Replaces any existing rows for the run so a re-write cannot double-charge.

    The invariant that matters is enforced by the caller, not here: the
    allocations for a run must sum to that run's total. If they ever do not, the
    ledger is describing money that was never billed (or failing to describe
    money that was).
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM run_allocations WHERE run_id=%s", (run_id,))
        for seq, alloc in enumerate(allocations or []):
            cur.execute(
                "INSERT INTO run_allocations (run_id, po_number, amount, seq) VALUES (%s,%s,%s,%s)",
                (run_id, alloc["po_number"], round(float(alloc["amount"]), 2), seq),
            )


def _insert_activity(cur, run_id, event_type, actor, note=None, metadata=None):
    """Append one activity row on an already-open cursor, inside a caller's
    own transaction -- so an activity event always lands atomically with the
    write it describes (a run being created, a claim taken, a decision
    recorded), never as a separate step that could succeed or fail on its own
    and leave the history disagreeing with what actually happened."""
    cur.execute(
        """INSERT INTO invoice_activity (run_id, event_type, actor, created_at, note, metadata_json)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
        (run_id, event_type, actor, datetime.now(timezone.utc).isoformat(), note,
         json.dumps(metadata) if metadata is not None else None),
    )
    return cur.fetchone()["id"]


def log_activity(run_id: int, event_type: str, actor: str = None, note: str = None,
                 metadata: dict = None):
    """Append one activity row in its own transaction, for callers with no
    open cursor of their own (e.g. logging a document view from the API
    layer). Prefer `_insert_activity` when already inside a `write_txn()`."""
    with write_txn() as conn:
        with conn.cursor() as cur:
            return _insert_activity(cur, run_id, event_type, actor, note, metadata)


def list_activity(run_id: int):
    """The full chronological history for a run, oldest first. Immutable by
    construction -- this table is never UPDATEd or individually deleted from,
    only inserted into (or cleared as a whole run's history, in
    clear_run_history)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, event_type, actor, created_at, note, metadata_json
                   FROM invoice_activity WHERE run_id=%s ORDER BY id ASC""",
                (run_id,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        r["metadata"] = json.loads(r.pop("metadata_json")) if r.get("metadata_json") else None
    return rows


def get_active_claim(run_id: int):
    """Who currently owns the review of this run, or None.

    DERIVED, not stored: the most recent review_claims row for this run that
    has not been released AND whose lease has not expired. A claim past its
    `expires_at` reads as "no active claim" here even before anything has
    gone back and marked it released -- the row is cleaned up lazily by the
    next call to claim_review(), but callers that only want to know "is
    someone reviewing this right now" never have to wait for that cleanup to
    get the right answer.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, claimed_by, claimed_at, expires_at FROM review_claims
                   WHERE run_id=%s AND released_at IS NULL ORDER BY id DESC LIMIT 1""",
                (run_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if row["expires_at"] <= datetime.now(timezone.utc).isoformat():
        return None
    return dict(row)


def claim_review(run_id: int, username: str, lease_minutes: int = None):
    """Claim exclusive ownership of a NEEDS_REVIEW run for human review.

    THE CONCURRENCY GUARANTEE

    `SELECT ... FOR UPDATE` on the `runs` row, exactly the same tool
    `save_run_checked` uses to serialise two invoices racing the same PO --
    here it serialises two employees racing the same run. Two concurrent
    claim attempts for the same run_id cannot both read "no active claim" and
    both insert one: whichever commits first is the winner, and the second
    sees the first's row and is refused. The database is the authority, not a
    frontend timer or an in-memory flag, which is why this works correctly
    across multiple worker processes and would still work correctly if this
    application ever ran more than one.

    THREE OUTCOMES, NOT TWO

    * No active claim (none ever existed, or the last one was released or has
      expired) -> a new claim row is inserted and this call wins.
    * An active, unexpired claim held by THIS SAME username -> treated as a
      heartbeat/retry, not a conflict: the lease is renewed and `renewed` is
      True. Covers a caller that already holds the claim retrying the same
      request (a flaky network, a double click) without being told "someone
      else has this".
    * An active, unexpired claim held by SOMEONE ELSE -> refused. The caller
      is told who holds it and when the lease expires, so the frontend can
      show "currently being reviewed by X" rather than a bare error.

    Only a NEEDS_REVIEW run can be claimed -- reviewing an already-decided run
    makes no sense, and this reuses the exact eligibility test
    record_human_review() already applies for the same reason.
    """
    lease_minutes = lease_minutes or config.review_claim_lease_minutes()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    expires_iso = (now + timedelta(minutes=lease_minutes)).isoformat()

    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, automated_decision FROM runs WHERE id=%s FOR UPDATE",
                (run_id,))
            run = cur.fetchone()
            if run is None:
                return {"ok": False, "error": "unknown run"}

            automated = run["automated_decision"] or run["status"]
            if automated != "NEEDS_REVIEW":
                return {"ok": False,
                        "error": f"only NEEDS_REVIEW runs can be claimed (this one is {automated})"}

            cur.execute(
                """SELECT id, claimed_by, claimed_at, expires_at FROM review_claims
                   WHERE run_id=%s AND released_at IS NULL ORDER BY id DESC LIMIT 1""",
                (run_id,))
            active = cur.fetchone()

            if active and active["expires_at"] > now_iso:
                if active["claimed_by"] == username:
                    cur.execute("UPDATE review_claims SET expires_at=%s WHERE id=%s",
                               (expires_iso, active["id"]))
                    return {"ok": True, "claim_id": active["id"], "claimed_by": username,
                            "claimed_at": active["claimed_at"], "expires_at": expires_iso,
                            "renewed": True}
                return {"ok": False, "error": "claimed",
                        "claimed_by": active["claimed_by"], "expires_at": active["expires_at"]}

            if active:
                # Unexpired-but-unreleased claim from a prior holder: the
                # lease ran out and nobody came back to release it (a closed
                # tab, a lost connection). Close it out as expired rather than
                # silently overwriting it, so the history still shows what
                # happened to it.
                cur.execute(
                    "UPDATE review_claims SET released_at=%s, release_reason='expired' WHERE id=%s",
                    (now_iso, active["id"]))
                _insert_activity(cur, run_id, "REVIEW_RELEASED", None,
                                 note="Claim lease expired without action; taken over on next claim.",
                                 metadata={"claim_id": active["id"],
                                           "previous_holder": active["claimed_by"]})

            cur.execute(
                """INSERT INTO review_claims (run_id, claimed_by, claimed_at, expires_at)
                   VALUES (%s,%s,%s,%s) RETURNING id""",
                (run_id, username, now_iso, expires_iso))
            claim_id = cur.fetchone()["id"]
            _insert_activity(cur, run_id, "REVIEW_CLAIMED", username,
                             metadata={"claim_id": claim_id, "expires_at": expires_iso})
            return {"ok": True, "claim_id": claim_id, "claimed_by": username,
                    "claimed_at": now_iso, "expires_at": expires_iso, "renewed": False}


def release_review_claim(run_id: int, username: str, is_admin: bool = False,
                         reason: str = "released"):
    """Release the active claim on a run.

    Only the claim's own holder may release it, unless the caller is an
    admin -- the same override authority `invoice:admin` already carries for
    `/status`, applied here so a departed or unreachable employee's claim can
    be freed without waiting out the full lease.

    Locks the `runs` row first (Phase E), before locking the claim row itself
    -- the same lock, in the same order, claim_review()/record_human_review()/
    set_run_status() already take. Two effects: an unknown run now reads as
    "unknown run" rather than the misleading "no active claim on this run" it
    would previously report (there being no claim rows for a run that does not
    exist at all), and a release can no longer land in the same instant as a
    concurrent claim or decision on the same run -- one fully commits before
    the other's transaction can proceed.
    """
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (run_id,))
            if cur.fetchone() is None:
                return {"ok": False, "error": "unknown run"}

            cur.execute(
                """SELECT id, claimed_by FROM review_claims
                   WHERE run_id=%s AND released_at IS NULL ORDER BY id DESC LIMIT 1 FOR UPDATE""",
                (run_id,))
            active = cur.fetchone()
            if not active:
                return {"ok": False, "error": "no active claim on this run"}
            if active["claimed_by"] != username and not is_admin:
                return {"ok": False, "error": "claimed by another reviewer",
                        "claimed_by": active["claimed_by"]}
            now_iso = datetime.now(timezone.utc).isoformat()
            cur.execute("UPDATE review_claims SET released_at=%s, release_reason=%s WHERE id=%s",
                       (now_iso, reason, active["id"]))
            _insert_activity(cur, run_id, "REVIEW_RELEASED", username,
                             metadata={"claim_id": active["id"], "reason": reason})
            return {"ok": True, "run_id": run_id, "released_by": username}


def add_comment(run_id: int, username: str, note: str):
    """Append a standalone note to a run's activity history, without ruling
    on it -- for a reviewer flagging something mid-review before accepting or
    rejecting. Distinct from the note a review decision itself may carry
    (record_human_review), which is attached to that decision specifically."""
    note = (note or "").strip()
    if not note:
        return {"ok": False, "error": "note must not be empty"}
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM runs WHERE id=%s", (run_id,))
            if cur.fetchone() is None:
                return {"ok": False, "error": "unknown run"}
            activity_id = _insert_activity(cur, run_id, "COMMENT_ADDED", username, note=note)
            return {"ok": True, "activity_id": activity_id}


def allocations_from_match(po_match: dict, total):
    """The allocation rows implied by a PO match.

    One place, so the single-PO case is not a separate code path from the
    multi-PO one -- a run bound to one PO is simply a run with one allocation.

    Returns [] when nothing was bound, which is correct: an invoice with no PO
    consumes no budget anywhere.
    """
    po_match = po_match or {}
    explicit = po_match.get("allocations")
    if explicit:
        return [{"po_number": a["po_number"], "amount": a["amount"]} for a in explicit]
    po_number = po_match.get("po_number")
    if not po_number or total is None:
        return []
    return [{"po_number": po_number, "amount": total}]


def allocations_for_run(run_id: int):
    """What this run charged, to which POs, in the order the invoice referenced them."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT po_number, amount, seq FROM run_allocations WHERE run_id=%s ORDER BY seq",
                (run_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def save_document(run_id: int, original_filename: str, mime_type: str, size_bytes: int,
                  sha256_hex: str, uploaded_by: str, source: str, storage_backend: str,
                  storage_key: str) -> int:
    """Record metadata for a document whose bytes are already written to the
    store. Returns the new document id.

    `source` is validated against `config.DOCUMENT_SOURCES` here rather than
    trusted from the caller -- this function is the one place a document row
    can be created, so it is also the one place that can refuse a value that
    is not one of the sources this application actually recognises.
    """
    if source not in config.DOCUMENT_SOURCES:
        raise ValueError(f"unknown document source: {source!r}")
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO documents (run_id, original_filename, mime_type, size_bytes,
                   sha256, uploaded_by, uploaded_at, source, storage_backend, storage_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (run_id, original_filename, mime_type, size_bytes, sha256_hex, uploaded_by,
                 datetime.now(timezone.utc).isoformat(), source, storage_backend, storage_key),
            )
            return cur.fetchone()["id"]


def get_document_for_run(run_id: int):
    """The document recorded against a run, or None if the run has none --
    either because it predates this feature, or because persisting the file
    failed at upload time (see main.py's `_persist_document`, which never
    lets that failure fail the run itself). The most recent row wins; nothing
    in this application writes more than one per run today."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM documents WHERE run_id=%s ORDER BY id DESC LIMIT 1",
                (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def init_db(reset_runs: bool = False):
    """Create/upgrade the schema, then (re)load seed reference data.

    Runs on every startup, exactly as it did against SQLite -- including the
    reload-from-JSON behaviour for purchase_orders/vendors, which is why
    neither of those tables carries a foreign key TO it from run_allocations
    (a historical run must still be able to name a PO that a later edit to
    purchase_orders.json removed).
    """
    # write_txn(), not a bare get_conn(): this function is many statements long
    # (create tables, add columns, reload seed rows) and must land as one unit.
    # Under sqlite3's implicit-transaction default that was automatic; here it
    # has to be asked for, or a failure partway through (e.g. a malformed
    # purchase_orders.json on the seed-reload step) could commit a schema with
    # no reference data, or half of one seed table and none of the other.
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{PG_SCHEMA}"')

            cur.execute(
                """CREATE TABLE IF NOT EXISTS purchase_orders (
                    po_number TEXT PRIMARY KEY,
                    vendor TEXT,
                    amount REAL,
                    currency TEXT,
                    issued_date TEXT,
                    status TEXT,
                    description TEXT,
                    source_file TEXT,
                    source_row INTEGER
                )"""
            )
            _ensure_columns(conn, "purchase_orders",
                            {"source_file": "TEXT", "source_row": "INTEGER"})

            cur.execute(
                """CREATE TABLE IF NOT EXISTS vendors (
                    vendor_name TEXT PRIMARY KEY,
                    vendor_id TEXT,
                    status TEXT
                )"""
            )

            cur.execute(
                """CREATE TABLE IF NOT EXISTS runs (
                    id SERIAL PRIMARY KEY,
                    filename TEXT,
                    status TEXT,
                    created_at TEXT,
                    vendor_name TEXT,
                    invoice_number TEXT,
                    total REAL,
                    po_number TEXT,
                    extracted_json TEXT,
                    po_match_json TEXT,
                    stages_json TEXT,
                    reasons_json TEXT,
                    audit_json TEXT,
                    automated_decision TEXT,
                    human_decision TEXT,
                    final_decision TEXT,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_note TEXT
                )"""
            )
            _ensure_columns(conn, "runs", {
                "audit_json": "TEXT",
                # The review columns are separate from `status` on purpose. `status` is
                # what the LEDGER reads -- consumption sums APPROVED runs -- so a human
                # approval has to land there for the money to move. But the automated
                # decision is a historical fact that must survive being overridden, so
                # it gets its own column that nothing ever rewrites.
                "automated_decision": "TEXT",
                "human_decision": "TEXT",
                "final_decision": "TEXT",
                "reviewed_by": "TEXT",
                "reviewed_at": "TEXT",
                "review_note": "TEXT",
            })
            # Runs that predate these columns: backfill the automated decision from the
            # status they were committed with. That is exactly what it was.
            cur.execute("""UPDATE runs SET automated_decision = status
                          WHERE automated_decision IS NULL""")
            cur.execute("""UPDATE runs SET final_decision = status
                          WHERE final_decision IS NULL""")

            # HOW MUCH OF EACH RUN WAS CHARGED TO WHICH PO.
            #
            # `runs.po_number` holds one PO, which cannot describe an invoice covering
            # several. This table can. It is NOT a stored balance -- the distinction is
            # the whole reason the design survives:
            #
            #   * A COUNTER would be authoritative, would need an explicit refund on
            #     reversal, and would be one missed code path away from a PO that can
            #     never be spent again. That was rejected, twice.
            #   * An ALLOCATION is an immutable fact about a run: this invoice billed
            #     $X against PO-Y. Whether it COUNTS is still derived, by joining to
            #     `runs.status='APPROVED'` at read time. Reversal and idempotency stay
            #     structural exactly as before.
            #
            # `runs.po_number` is kept as the primary PO for display and for existing
            # queries; the ledger no longer reads it.
            cur.execute(
                """CREATE TABLE IF NOT EXISTS run_allocations (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    po_number TEXT NOT NULL,
                    amount REAL NOT NULL,
                    seq INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_alloc_po ON run_allocations(po_number)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_alloc_run ON run_allocations(run_id)")

            # METADATA about the uploaded invoice PDF, never the bytes -- the
            # bytes live behind the DocumentStore abstraction (documents.py),
            # keyed by `storage_key`, which is a server-generated identifier and
            # never the original filename (see documents.py's module docstring
            # for why). `original_filename` here is display-only, and is already
            # the sanitised name main.py computed at upload time (no directory
            # component, no control characters) -- not the raw client-supplied
            # one, which never reaches storage at all.
            cur.execute(
                """CREATE TABLE IF NOT EXISTS documents (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    original_filename TEXT,
                    mime_type TEXT,
                    size_bytes INTEGER,
                    sha256 TEXT,
                    uploaded_by TEXT,
                    uploaded_at TEXT,
                    source TEXT,
                    storage_backend TEXT,
                    storage_key TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_run_id ON documents(run_id)")

            # WHAT HAPPENED TO THIS INVOICE, AND WHEN (Phase D).
            #
            # Deliberately a SEPARATE concept from the audit trail on `runs`.
            # `audit_json` explains why the DETERMINISTIC RULES reached the
            # verdict they did -- it is evidence about the process, written
            # once by decide() and never appended to. This table is evidence
            # about PEOPLE (and the system, acting on their behalf) over time:
            # who claimed a review, who accepted or rejected it, who left a
            # note, who viewed the source document. Append-only, never
            # updated or deleted except as a unit when the run itself is
            # cleared (clear_run_history) -- a later event must never erase
            # or overwrite an earlier one, or the question "who did what, and
            # when" stops being answerable.
            #
            # `actor` is the authenticated username, or NULL for an event the
            # system generated on its own (a cascade auto-approval, a claim
            # that expired unattended) -- never a name invented for either
            # case. `metadata_json` carries whatever structured detail is
            # useful for that event type (a claim id, an expiry, a status
            # transition); `note` is free text, used for review notes and
            # standalone comments.
            cur.execute(
                """CREATE TABLE IF NOT EXISTS invoice_activity (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    created_at TEXT NOT NULL,
                    note TEXT,
                    metadata_json TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_activity_run_id ON invoice_activity(run_id)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_created_at ON invoice_activity(created_at)")

            # WHO IS CURRENTLY REVIEWING THIS INVOICE (Phase D).
            #
            # One employee at a time may own the review of a NEEDS_REVIEW
            # invoice. No `runs.current_reviewer` column and no in-memory
            # lock -- both would drift the moment two processes or two
            # requests raced, and an in-memory value cannot be shared across
            # workers at all. Instead this is append-only, same spirit as
            # `run_allocations`: a row is an immutable fact ("X claimed this
            # run at T, until it expires or is released"), and "who currently
            # holds it" is DERIVED at read time (get_active_claim) as the
            # most recent row for the run with `released_at IS NULL` and an
            # unexpired lease -- never a stored "current owner" field that
            # could go stale.
            #
            # `expires_at` is a LEASE, not a permanent lock: a claim with no
            # activity for config.review_claim_lease_minutes() is treated as
            # abandoned by the next claim attempt, which marks it released
            # (release_reason='expired') and takes over. There is no
            # background sweep -- exactly like PO balances, staleness is
            # resolved lazily, on the next read/write that cares, rather than
            # by a job that has to run on a schedule to stay correct.
            cur.execute(
                """CREATE TABLE IF NOT EXISTS review_claims (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    claimed_by TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT,
                    release_reason TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_claims_run_id ON review_claims(run_id)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claims_active ON review_claims(run_id, released_at)")

            # SENDERS THIS BUSINESS EXPECTS INVOICES FROM (Phase F).
            #
            # Reloaded from data/trusted_email_senders.json every startup,
            # exactly like purchase_orders and vendors, and for the same
            # reason: it is procurement reference data, owned by the business
            # and changed by editing a file under review, not by an API call
            # that would make "who are we willing to accept invoices from"
            # editable at runtime by anyone holding a token.
            #
            # An entry is a domain or a full address. Being on this list is
            # NOT authentication -- a spoofer can put an allowlisted domain in
            # From for free. It only decides whether an ALREADY-authenticated
            # sender is one we do business with.
            cur.execute(
                """CREATE TABLE IF NOT EXISTS trusted_email_senders (
                    sender TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    vendor_name TEXT,
                    status TEXT,
                    note TEXT
                )"""
            )

            # WHAT WE COULD PROVE ABOUT AN INCOMING MESSAGE (Phase F).
            #
            # METADATA AND AUTHENTICATION EVIDENCE ONLY. The message body is
            # never written here or anywhere else, and neither are attachment
            # bytes -- `auth_json` holds the authentication headers, the
            # signature parameters and the alignment arithmetic, which is what
            # an auditor needs to re-derive the verdict, and nothing that
            # needs it also needs the invoice text. `sha256` is over the raw
            # message so a stored record can still be tied to a message
            # produced from another source, without keeping the message.
            #
            # `classification` and `status` are split for exactly the reason
            # `runs.automated_decision` and `runs.status` are split: the
            # classification is what the deterministic evaluator concluded and
            # is never rewritten, while `status` moves when a person releases
            # or discards a quarantined message.
            #
            # `run_id` is nullable and stays NULL in Phase F -- feeding an
            # admitted message's attachment through the pipeline is Phase G.
            # The column exists now so that when it does, the security record
            # and the run it produced are joinable without a schema change.
            cur.execute(
                """CREATE TABLE IF NOT EXISTS email_messages (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER,
                    sha256 TEXT NOT NULL,
                    message_id TEXT,
                    received_at TEXT NOT NULL,
                    submitted_by TEXT,
                    source TEXT,
                    from_address TEXT,
                    from_domain TEXT,
                    from_display_name TEXT,
                    envelope_from TEXT,
                    subject TEXT,
                    size_bytes INTEGER,
                    attachment_count INTEGER,
                    has_pdf_attachment BOOLEAN,
                    spf_result TEXT,
                    dkim_result TEXT,
                    dmarc_result TEXT,
                    dmarc_aligned BOOLEAN,
                    signature_kind TEXT,
                    signature_result TEXT,
                    trusted_sender BOOLEAN,
                    classification TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reasons_json TEXT,
                    auth_json TEXT,
                    released_by TEXT,
                    released_at TEXT,
                    release_note TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_email_status ON email_messages(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_email_sha256 ON email_messages(sha256)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_email_run_id ON email_messages(run_id)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_email_received_at ON email_messages(received_at)")

            # WHAT PEOPLE DID ABOUT A MESSAGE (Phase F).
            #
            # WHY THIS IS NOT invoice_activity: that table's `run_id` is NOT
            # NULL and foreign-keyed to `runs`. A quarantined message has no
            # run, and if it is discarded it never will have one -- so it
            # cannot be represented there without dropping that constraint,
            # which is a Phase D invariant this phase has no business
            # weakening. Same design, same columns, same append-only rule,
            # different subject. Once Phase G turns an admitted message into a
            # run, that run's own history continues in invoice_activity as
            # normal; these two are joined by email_messages.run_id, not
            # merged.
            cur.execute(
                """CREATE TABLE IF NOT EXISTS email_activity (
                    id SERIAL PRIMARY KEY,
                    email_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    created_at TEXT NOT NULL,
                    note TEXT,
                    metadata_json TEXT,
                    FOREIGN KEY (email_id) REFERENCES email_messages(id)
                )"""
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_email_activity_email_id ON email_activity(email_id)")

            # INGESTION STATE (Phase G) -- added to the Phase F table rather
            # than a parallel one, because it is the same subject: one row per
            # incoming message. `_ensure_columns` is how every earlier phase
            # has extended an existing table, and it is what makes this work on
            # a database that already has Phase F rows in it.
            _ensure_columns(conn, "email_messages", {
                "provider": "TEXT",
                "provider_message_id": "TEXT",
                "provider_received_at": "TEXT",
                "reply_to": "TEXT",
                "recipients_json": "TEXT",
                "sender_type": "TEXT",
                "trust_status": "TEXT",
                "relevance": "TEXT",
                "triage_json": "TEXT",
                "ingest_status": "TEXT",
                "ingest_error": "TEXT",
                "processed_at": "TEXT",
            })
            # THE IDEMPOTENCY MECHANISM.
            #
            # Not a check-then-insert in Python, which two concurrent pollers
            # (or two uvicorn workers) would both pass before either wrote.
            # A UNIQUE INDEX means the database refuses the second write no
            # matter how the race is timed, and the ingestion layer treats that
            # refusal as "already seen" rather than as an error. Partial, so
            # Phase F rows that predate ingestion (NULL provider) do not all
            # collide on a single NULL key.
            cur.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_email_provider_msg
                   ON email_messages(provider, provider_message_id)
                   WHERE provider IS NOT NULL AND provider_message_id IS NOT NULL""")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_email_ingest_status "
                "ON email_messages(ingest_status)")

            # ONE ROW PER ATTACHMENT (Phase G).
            #
            # `email_messages.run_id` holds ONE run, which cannot describe an
            # email carrying three invoices -- the same shape of problem
            # `runs.po_number` had before `run_allocations` existed, and solved
            # the same way. Each attachment gets its own row, its own status and
            # its own nullable run_id, so:
            #
            #   * one email can produce several runs,
            #   * an attachment that failed can be retried without reprocessing
            #     the ones that already succeeded (the retry skips any row that
            #     is already PROCESSED),
            #   * and an attachment deliberately skipped records WHY, so
            #     "we ignored your logo.png" is answerable later.
            #
            # The bytes are not here. They go to the DocumentStore through the
            # existing pipeline, exactly as a browser upload does.
            cur.execute(
                """CREATE TABLE IF NOT EXISTS email_attachments (
                    id SERIAL PRIMARY KEY,
                    email_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL DEFAULT 0,
                    filename TEXT,
                    content_type TEXT,
                    size_bytes INTEGER,
                    sha256 TEXT,
                    is_invoice_candidate BOOLEAN,
                    status TEXT NOT NULL,
                    skip_reason TEXT,
                    run_id INTEGER,
                    run_status TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    storage_backend TEXT,
                    storage_key TEXT,
                    FOREIGN KEY (email_id) REFERENCES email_messages(id),
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                )"""
            )
            # A QUARANTINED message's PDF is held in the existing DocumentStore
            # (Phase C) so that releasing it later actually has something to
            # process. Only the attachment is kept -- never the message body,
            # which Phase F deliberately does not store and this phase does not
            # start storing. The key is server-generated
            # (documents.new_storage_key), never derived from the sender's
            # filename, so the same path-safety argument Phase C made applies
            # unchanged. Once the attachment becomes a run, the run's own
            # document row owns a copy and this holding copy is deleted.
            _ensure_columns(conn, "email_attachments", {
                "storage_backend": "TEXT",
                "storage_key": "TEXT",
            })
            cur.execute("CREATE INDEX IF NOT EXISTS idx_email_att_email "
                        "ON email_attachments(email_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_email_att_run "
                        "ON email_attachments(run_id)")
            # An attachment is identified within its message by content hash,
            # so the same PDF attached twice to one email is stored once and
            # processed once -- and a redelivery of the whole message cannot
            # append a second copy of every attachment.
            cur.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_email_att_unique
                   ON email_attachments(email_id, sha256)
                   WHERE sha256 IS NOT NULL""")

            # Read heavily by find_duplicate() (invoice_number equality) and by every
            # ledger query that filters on status -- neither existed as an index under
            # SQLite because a single-file database at this volume never needed one;
            # a shared server under concurrent load benefits from both.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_invoice_number ON runs(invoice_number)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)")

            # ANALYTICS (Phase H). Indexes, and nothing else -- no counter
            # column, no summary table. Every KPI is a query over the rows
            # that already exist (see analytics.py's module docstring for why
            # a stored rollup was rejected), so the only thing analytics needs
            # from the schema is for those queries to be cheap.
            #
            # `created_at` is the one every analytics query filters on and the
            # only column here nothing previously indexed -- date windows are
            # half-open ISO-string ranges, which a plain B-tree on a TEXT
            # column serves directly, because every timestamp this application
            # writes is UTC with the same `+00:00` spelling and therefore
            # sorts lexicographically in true chronological order.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at)")
            # Per-vendor and per-reviewer breakdowns group by these.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_vendor_name ON runs(vendor_name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_reviewed_by ON runs(reviewed_by)")
            # Per-person activity counts group by actor; the existing
            # invoice_activity indexes are on run_id and created_at only.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_activity_actor ON invoice_activity(actor)")

            # Runs committed before this table existed carry their charge in
            # (po_number, total). Synthesise the one allocation row each of them always
            # implied, so every historical balance reads exactly as it did before -- the
            # migration must not move a single number.
            #
            # Idempotent by construction: it only touches runs that have no allocation
            # rows at all, and every run written from now on gets its rows at insert
            # time. A genuine multi-PO run can therefore never be "topped up" by a later
            # startup.
            cur.execute(
                """INSERT INTO run_allocations (run_id, po_number, amount, seq)
                   SELECT r.id, r.po_number, COALESCE(r.total, 0), 0
                   FROM runs r
                   WHERE r.po_number IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM run_allocations a WHERE a.run_id = r.id)"""
            )

            if reset_runs:
                cur.execute("DELETE FROM run_allocations")
                cur.execute("DELETE FROM runs")

            # (re)load seed reference data every start so edits to the JSON files take effect
            with open(PO_SEED) as f:
                pos = json.load(f)
            cur.execute("DELETE FROM purchase_orders")
            source_file = os.path.basename(PO_SEED)
            for i, po in enumerate(pos):
                # Where this PO came from, so an audit trail can cite it instead of
                # asserting a balance with no provenance.
                #
                # `source_row` is the record's position in the procurement file, 1-based.
                # For a JSON array that IS the row, and it is read from the data rather
                # than assumed -- if a record carries its own `source_row` (which is what
                # an export from a spreadsheet would provide) that value wins, because it
                # refers to the real sheet row and the array index would not.
                #
                # Nothing here invents a number. A record with no derivable position
                # stores NULL, and the audit trail then says the row is unknown rather
                # than printing a plausible-looking one.
                row_no = po.get("source_row")
                if row_no is None:
                    row_no = i + 1
                cur.execute(
                    """INSERT INTO purchase_orders
                       (po_number, vendor, amount, currency, issued_date, status, description,
                        source_file, source_row)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (po["po_number"], po["vendor"], po["amount"], po["currency"],
                     po["issued_date"], po["status"], po["description"],
                     po.get("source_file") or source_file, row_no),
                )

            with open(VENDOR_SEED) as f:
                vendors = json.load(f)
            cur.execute("DELETE FROM vendors")
            for v in vendors:
                cur.execute("INSERT INTO vendors VALUES (%s,%s,%s)",
                           (v["vendor_name"], v["vendor_id"], v["status"]))

            # Phase F reference data, same reload-on-startup contract. A
            # MISSING file is not an error: a deployment that has not decided
            # who it trusts yet gets an empty list, and the classifier's
            # behaviour with an empty list is well defined (it stops treating
            # allowlist membership as a requirement rather than failing every
            # sender). A malformed file IS an error, and rolls back the whole
            # of init_db with the other seed loads.
            cur.execute("DELETE FROM trusted_email_senders")
            if os.path.isfile(TRUSTED_SENDER_SEED):
                with open(TRUSTED_SENDER_SEED) as f:
                    senders = json.load(f)
                for s in senders:
                    sender = (s.get("sender") or "").strip().lower()
                    if not sender:
                        continue
                    cur.execute(
                        """INSERT INTO trusted_email_senders
                           (sender, kind, vendor_name, status, note)
                           VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (sender) DO NOTHING""",
                        (sender,
                         (s.get("kind") or ("address" if "@" in sender else "domain")).lower(),
                         s.get("vendor_name"), (s.get("status") or "trusted").lower(),
                         s.get("note")))


def list_purchase_orders():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM purchase_orders ORDER BY po_number")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_vendors():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vendors ORDER BY vendor_name")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_po(po_number: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM purchase_orders WHERE UPPER(po_number)=UPPER(%s)", (po_number,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


# Legal-form synonyms, mapped to a canonical token. Synonyms are CANONICALISED
# rather than deleted: dropping them entirely would collapse "Umbrella Cleaning
# Co" and "Umbrella Cleaning Ltd" into one vendor, which are different legal
# entities and may well have different bank details.
_LEGAL_FORMS = {
    "corporation": "corp", "corp": "corp",
    "incorporated": "inc", "inc": "inc",
    "limited": "ltd", "ltd": "ltd",
    "company": "co", "co": "co",
    "llc": "llc", "llp": "llp", "plc": "plc",
}


def normalize_vendor_name(name: str) -> str:
    """A comparable form of a company name. Deterministic, no fuzziness.

    Handles the differences that are genuinely cosmetic -- case, spacing,
    punctuation, "&" vs "and", and legal-form abbreviations -- and nothing else.
    "ABC Corp." and "ABC Corporation" become the same string; "ABC Supplies" and
    "XYZ Supplies" do not.

    Synonyms are mapped on every token, not only the last, so "ABC Corp. of
    America" normalises the same way "ABC Corporation of America" does. The
    mapping is between words that mean the same thing, so this is
    meaning-preserving wherever it applies.
    """
    if not name:
        return ""
    s = name.strip().lower()
    s = s.replace("&", " and ")
    # Apostrophes are DELETED, not spaced: "O'Brien" and "OBrien" are the same
    # name, whereas spacing it would give "o brien" and break the match. Every
    # other punctuation mark becomes a space, so "Smith-Jones" matches
    # "Smith Jones".
    s = re.sub(r"[’']", "", s)
    s = re.sub(r"[^\w\s]", " ", s)      # periods, commas, hyphens -> space
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    return " ".join(_LEGAL_FORMS.get(tok, tok) for tok in s.split())


def find_vendor_matches(name: str):
    """Every approved vendor whose normalised name equals this one.

    Returns a list so callers can tell the three cases apart, which is the whole
    point: exactly one is a confident match, zero is confidently not on the list,
    and more than one is ambiguous and belongs to a human.

    There is deliberately NO substring fallback. The previous implementation
    matched bidirectionally on raw substrings, which meant "Office", "Supplies"
    and even the single letter "s" all resolved to "Acme Office Supplies" -- while
    "Stark   Industrial Parts" with a double space matched nothing. Loose exactly
    where it was dangerous and strict exactly where it was not.
    """
    if not name or not name.strip():
        return []
    target = normalize_vendor_name(name)
    if not target:
        return []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vendors")
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return [v for v in rows if normalize_vendor_name(v["vendor_name"]) == target]


def find_vendor(name: str):
    """The single approved vendor this name resolves to, or None.

    None covers both "no such vendor" and "more than one candidate" -- callers
    that need to tell those apart use find_vendor_matches(). Kept at this
    signature so existing callers are unaffected.
    """
    matches = find_vendor_matches(name)
    return matches[0] if len(matches) == 1 else None


def consumed_amount_for_po(po_number: str, exclude_run_id=None):
    """Sum of totals from APPROVED runs already matched to this PO."""
    conn = get_conn()
    try:
        return _consumed(conn, po_number, exclude_run_id)
    finally:
        conn.close()


def remaining_for_po(po_number: str, exclude_run_id=None):
    """PO amount minus what approved runs have consumed. None if no such PO."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT amount FROM purchase_orders WHERE UPPER(po_number)=UPPER(%s)",
                       (po_number,))
            row = cur.fetchone()
        if row is None:
            return None
        return round(row["amount"] - _consumed(conn, po_number, exclude_run_id), 2)
    finally:
        conn.close()


def find_duplicate(vendor_name, invoice_number, total):
    if not invoice_number:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM runs WHERE invoice_number=%s AND ABS(COALESCE(total,-1) - %s) < 0.01
                   AND (vendor_name=%s OR vendor_name IS NULL) ORDER BY created_at ASC LIMIT 1""",
                (invoice_number, total if total is not None else -999999, vendor_name),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def save_run_checked(filename, status, extracted: dict, po_match: dict, stages: list,
                     reasons: list, tolerance_for=None, audit=None, uploaded_by=None):
    """Persist a run, re-verifying the PO balance under a row lock first.

    The pipeline computes its verdict outside any transaction -- it has to, since
    extraction can take seconds and holding a write lock across a model call
    would serialise the whole system. So the balance it decided against may be
    stale by the time it commits.

    This is optimistic concurrency with an authoritative final check: lock
    exactly the purchase_orders row(s) this invoice charges with `SELECT ... FOR
    UPDATE`, re-read the consumed total, and if the invoice no longer fits,
    downgrade APPROVED to NEEDS_REVIEW *before* inserting. Two concurrent
    invoices for one PO can no longer both approve past the balance -- whichever
    commits second sees the first and is held for a human. Invoices against
    DIFFERENT POs never contend for this lock at all.

    Returns (run_id, final_status, extra_reason_or_None).
    """
    po_number = po_match.get("po_number")
    total = extracted.get("total")
    extra = None
    allocations = allocations_from_match(po_match, total)

    with write_txn() as conn:
        with conn.cursor() as cur:
            # Re-check EVERY PO this invoice charges, not just the primary one. A
            # multi-PO invoice can be raced on any of them, and one that no longer
            # fits is enough to hold the whole invoice -- the allocations are a
            # package, and committing part of a split would charge a PO for an
            # invoice that was not approved.
            if status == "APPROVED" and total is not None:
                for alloc in allocations:
                    # FOR UPDATE locks this PO row until the transaction ends, so a
                    # second invoice racing the SAME PO blocks here until this one
                    # commits or rolls back -- the exact property BEGIN IMMEDIATE
                    # gave for free under SQLite, now scoped to just this row.
                    cur.execute(
                        "SELECT amount FROM purchase_orders WHERE UPPER(po_number)=UPPER(%s) FOR UPDATE",
                        (alloc["po_number"],))
                    row = cur.fetchone()
                    if row is None:
                        continue
                    remaining = round(row["amount"] - _consumed(conn, alloc["po_number"]), 2)
                    tol = tolerance_for(remaining if remaining > 0 else row["amount"]) \
                        if tolerance_for else 0.0
                    if round(alloc["amount"] - remaining, 2) <= tol:
                        continue

                    status = "NEEDS_REVIEW"
                    extra = {
                        "text": (
                            f"Balance changed while this invoice was being processed: "
                            f"${remaining:.2f} remained on {alloc['po_number']} at commit time, "
                            f"against ${alloc['amount']:.2f} charged to it by this invoice. "
                            f"Another invoice consumed the PO first, so this one was held rather "
                            f"than approved past the authorised amount."
                        ),
                        "level": "fail",
                    }
                    reasons = list(reasons) + [extra]
                    # Keep the stored snapshot honest about what was committed.
                    po_match = dict(po_match, remaining_before=remaining,
                                    remaining_after=remaining,
                                    diff=round(alloc["amount"] - remaining, 2),
                                    within_tolerance=False)
                    break

            # Keep the trail consistent with what was actually committed. If the
            # balance re-check above downgraded this run, the audit must say so --
            # a trail that still reads APPROVED beside a NEEDS_REVIEW row is worse
            # than no trail.
            if audit is not None and extra is not None:
                audit = dict(audit)
                audit["automated_decision"] = status
                audit["reason"] = extra["text"]
                audit["reasons"] = list(audit.get("reasons") or []) + [extra]
                audit["comparison"] = dict(audit.get("comparison") or {},
                                           po_remaining=po_match.get("remaining_before"),
                                           variance=po_match.get("diff"),
                                           remaining_after=po_match.get("remaining_after"))
                audit["rules"] = [
                    dict(c, passed=False,
                         detail="PO balance changed before commit; re-checked under a row lock",
                         reason="Invoice total exceeds PO remaining amount.")
                    if c.get("name") == "PO remaining check" else c
                    for c in (audit.get("rules") or [])
                ]
                audit["rules_passed"] = [c["name"] for c in audit["rules"] if c["passed"]]
                audit["rules_failed"] = [c["name"] for c in audit["rules"] if not c["passed"]]

            cur.execute(
                """INSERT INTO runs (filename, status, created_at, vendor_name, invoice_number, total,
                   po_number, extracted_json, po_match_json, stages_json, reasons_json, audit_json,
                   automated_decision, final_decision)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (filename, status, datetime.now(timezone.utc).isoformat(),
                 extracted.get("vendor_name"), extracted.get("invoice_number"),
                 extracted.get("total"), po_number, json.dumps(extracted),
                 json.dumps(po_match), json.dumps(stages), json.dumps(reasons),
                 json.dumps(audit) if audit is not None else None,
                 # The decision this process reached on its own, recorded once and
                 # never rewritten. `status` may later move; this must not.
                 status, status),
            )
            run_id = cur.fetchone()["id"]
            # Inside the same transaction as the run row. A run that exists without
            # its allocations would be an invoice charged to nothing, and the PO it
            # consumed would silently read as still available.
            _write_allocations(conn, run_id, allocations_from_match(po_match, total))
            # Activity, same transaction: a run that exists with no record of
            # having been processed would be a gap in the very history this
            # table exists to keep complete.
            _insert_activity(cur, run_id, "PROCESSING_COMPLETED", uploaded_by,
                             metadata={"status": status})
            if status == "NEEDS_REVIEW":
                _insert_activity(cur, run_id, "REVIEW_REQUIRED", None,
                                 metadata={"reason": (audit or {}).get("reason")})
        return run_id, status, extra


def _apply_status_transition(cur, run_id: int, old_status: str, new_status: str,
                             reasons_json: str, note: str, claim_release_reason: str):
    """Write a status transition the caller has already decided on and locked.

    Shared by set_run_status() and record_human_review() (Phase E) so both go
    through exactly one implementation of "what happens when a run's status
    moves" -- the reasons trail, final_decision, and the claim-release side
    effect -- instead of two copies that could drift apart. ASSUMES the caller
    already holds a `SELECT ... FOR UPDATE` lock on this run's row for the
    duration of the enclosing transaction; this function does no locking of
    its own and must never be called outside one.
    """
    reasons = json.loads(reasons_json or "[]")
    reasons.append({
        "text": note or f"Status changed from {old_status} to {new_status} by an operator.",
        "level": "info",
    })
    cur.execute("UPDATE runs SET status=%s, reasons_json=%s WHERE id=%s",
               (new_status, json.dumps(reasons), run_id))
    # An automated status change (a cascade re-evaluation, an operator
    # reversal) moves the final decision with it. A run a human has already
    # ruled on keeps its HUMAN_* outcome -- that verdict belongs to a person
    # and is not something a later automated pass gets to relabel.
    cur.execute("""UPDATE runs SET final_decision=%s
                  WHERE id=%s AND human_decision IS NULL""",
               (new_status, run_id))

    # A review claim exists to protect a NEEDS_REVIEW invoice from being
    # worked by two employees at once. The moment the run leaves that state
    # -- a human ruling, a cascade re-evaluation, an admin override -- there
    # is nothing left to protect, so any active claim is released
    # automatically here rather than sitting there until its lease happens
    # to expire on its own.
    if old_status == "NEEDS_REVIEW" and new_status != "NEEDS_REVIEW":
        cur.execute(
            """SELECT id, claimed_by FROM review_claims
               WHERE run_id=%s AND released_at IS NULL ORDER BY id DESC LIMIT 1""",
            (run_id,))
        active = cur.fetchone()
        if active:
            now_iso = datetime.now(timezone.utc).isoformat()
            cur.execute(
                "UPDATE review_claims SET released_at=%s, release_reason=%s WHERE id=%s",
                (now_iso, claim_release_reason, active["id"]))
            _insert_activity(
                cur, run_id, "REVIEW_RELEASED", None,
                note=f"Released automatically: the invoice left NEEDS_REVIEW "
                     f"({claim_release_reason}).",
                metadata={"claim_id": active["id"], "previous_holder": active["claimed_by"]})


def set_run_status(run_id: int, new_status: str, note: str = None,
                   claim_release_reason: str = "resolved"):
    """Change a run's status. This is the reversal mechanism.

    There is no balance to refund. Consumption is DERIVED -- `_consumed` sums
    APPROVED runs -- so flipping a run out of APPROVED removes it from that sum
    in the same instant, and flipping it back restores it. A stored counter would
    need an explicit refund here, and would be one missed code path away from a
    PO that can never be spent again.

    `SELECT ... FOR UPDATE` on the run row (Phase E) -- the same tool
    claim_review already uses to serialise two employees racing a claim, here
    serialising two concurrent status changes on the same run (an admin
    override racing a cascade re-evaluation, or either racing a human
    decision via record_human_review, which takes this same lock).

    Returns (ok, old_status, po_number).
    """
    if new_status not in ("APPROVED", "NEEDS_REVIEW", "REJECTED"):
        return False, None, None

    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, po_number, reasons_json FROM runs WHERE id=%s FOR UPDATE",
                       (run_id,))
            row = cur.fetchone()
            if row is None:
                return False, None, None
            old_status, po_number = row["status"], row["po_number"]
            if old_status == new_status:
                return True, old_status, po_number

            _apply_status_transition(cur, run_id, old_status, new_status,
                                     row["reasons_json"], note, claim_release_reason)
            return True, old_status, po_number


# A human ruling maps to a ledger status AND to a final decision label. The
# labels are kept distinct on purpose: "APPROVED" is what the ledger consumes
# against, "HUMAN_APPROVED" is how it came to be approved. Collapsing them would
# lose the difference between an invoice the process cleared and one a person
# cleared over the process's objection -- which is precisely what an auditor is
# looking for.
HUMAN_OUTCOMES = {
    "ACCEPTED": ("APPROVED", "HUMAN_APPROVED"),
    "REJECTED": ("REJECTED", "HUMAN_REJECTED"),
}


def record_human_review(run_id: int, decision: str, reviewer: str = None, note: str = None):
    """Record a person's ruling on a run the process held for review.

    The automated decision is NEVER overwritten. It stays in
    `automated_decision` exactly as the rules produced it, and the human ruling
    is recorded beside it, so the run reads as the full history:

        automated_decision  NEEDS_REVIEW
        human_decision      ACCEPTED
        final_decision      HUMAN_APPROVED

    `status` does move, because that is the column the ledger reads: an accepted
    invoice has to consume its PO budget, and a rejected one has to release it.
    The transition itself is applied by `_apply_status_transition` -- the exact
    same logic `set_run_status` uses -- so reversal and the claim-release side
    effect stay correct here too.

    Only a run whose AUTOMATED decision was NEEDS_REVIEW is eligible. An invoice
    the process rejected outright is not something to wave through from a review
    screen, and one it approved needs no ruling.

    ATOMICITY (Phase E). The eligibility check (not already reviewed, not
    claimed by someone else) and the write that records the ruling now happen
    under ONE `SELECT ... FOR UPDATE` lock on the run row, held for the whole
    transaction. Previously these were three separate transactions -- a bare
    read with no lock, set_run_status()'s own transaction, then a further
    UPDATE -- which left a real gap: two concurrent submissions (a
    double-clicked Accept, a retried request, or a genuine ACCEPT-vs-REJECT
    race) could both read `human_decision IS NULL` before either committed,
    and both would then go on to write -- corrupting the activity history
    with two conflicting rulings on one run, with the final `status` decided
    by commit order rather than either caller being told "no". Locking the
    row first means the second request necessarily waits for the first to
    commit, then re-reads `human_decision` and is correctly refused with
    "already been reviewed". Same row, same lock as claim_review() and
    set_run_status() -- consistent lock ordering (runs, then review_claims)
    rules out a deadlock between them.

    Returns a dict; `ok` is False with a `error` when the run is not eligible.
    """
    # str() first: the body is arbitrary JSON, so `decision` can arrive as an
    # int, a list or None. Calling .strip() on it directly turns a malformed
    # request into a 500, which is both a worse answer and more information than
    # the caller should get.
    decision = str(decision or "").strip().upper()
    if decision not in HUMAN_OUTCOMES:
        return {"ok": False, "error": "decision must be ACCEPTED or REJECTED"}
    new_status, final_decision = HUMAN_OUTCOMES[decision]
    reviewer = (reviewer or "").strip() or None    # never invent an identity

    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT status, automated_decision, human_decision, po_number, reasons_json
                   FROM runs WHERE id=%s FOR UPDATE""",
                (run_id,))
            row = cur.fetchone()
            if row is None:
                return {"ok": False, "error": "unknown run"}

            # Runs written before these columns existed fall back to their
            # status, which at that point was the automated decision.
            automated = row["automated_decision"] or row["status"]
            if automated != "NEEDS_REVIEW":
                return {"ok": False, "error":
                        f"only NEEDS_REVIEW runs can be reviewed (this one is {automated})"}

            # A run may be ruled on ONCE. Because `automated_decision`
            # deliberately stays NEEDS_REVIEW forever, this check is what
            # actually enforces that -- and now that it runs under the same
            # lock as the write below, a second, concurrent submission cannot
            # slip in between this check and the one that got here first.
            # Reversing a ruling is an administrative act and goes through
            # the status endpoint, which requires 'invoice:admin' and leaves
            # its own trail.
            if row["human_decision"]:
                return {"ok": False, "error":
                        f"this run has already been reviewed ({row['human_decision']})"}

            # A run actively claimed by someone else is off-limits to a review
            # submitted under a different identity -- the same ownership
            # guarantee claim_review() enforces at claim time, checked again
            # here so a second reviewer cannot simply skip the claim step and
            # submit anyway. Read from `review_claims` inside this SAME
            # transaction (not via get_active_claim(), which would open a
            # second connection) so the claim state examined here is exactly
            # the state the write below commits against -- no window for a
            # concurrent claim/release to land in between. Claiming is
            # optional in this application (an unclaimed NEEDS_REVIEW run has
            # nothing to check against, and every review submitted before
            # Phase D existed was exactly that), so this only ever refuses
            # when a claim is actually in force and belongs to someone else.
            cur.execute(
                """SELECT claimed_by, expires_at FROM review_claims
                   WHERE run_id=%s AND released_at IS NULL ORDER BY id DESC LIMIT 1""",
                (run_id,))
            claim_row = cur.fetchone()
            now_iso = datetime.now(timezone.utc).isoformat()
            active_claim = claim_row if (claim_row and claim_row["expires_at"] > now_iso) else None
            if active_claim and active_claim["claimed_by"] != reviewer:
                return {"ok": False, "error": "claimed",
                        "claimed_by": active_claim["claimed_by"],
                        "expires_at": active_claim["expires_at"]}

            old_status, po_number = row["status"], row["po_number"]
            review_note = (note
                          or f"Human review: {decision} by {reviewer or 'an unattributed reviewer'}.")
            _apply_status_transition(cur, run_id, old_status, new_status,
                                     row["reasons_json"], review_note, "completed")

            reviewed_at = now_iso
            cur.execute(
                """UPDATE runs SET human_decision=%s, final_decision=%s, reviewed_by=%s,
                   reviewed_at=%s, review_note=%s WHERE id=%s""",
                (decision, final_decision, reviewer, reviewed_at, note, run_id))
            # `decision` is exactly "ACCEPTED" or "REJECTED" -- the same
            # vocabulary the activity/event-type list uses, so no translation
            # step exists to drift out of sync with what was actually stored.
            _insert_activity(cur, run_id, decision, reviewer, note=note,
                             metadata={"final_decision": final_decision})

    return {
        "ok": True,
        "run_id": run_id,
        "automated_decision": automated,
        "human_decision": decision,
        "final_decision": final_decision,
        "status": new_status,
        "previous_status": old_status,
        "reviewed_by": reviewer,
        "reviewed_at": reviewed_at,
        "po_number": po_number,
    }


def runs_pending_on_po(po_number: str):
    """NEEDS_REVIEW runs against a PO, oldest first.

    Order matters: when balance frees up, the invoice that queued first should
    get it. Anything else is a race dressed up as a feature.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Found through allocations, not `runs.po_number`: an invoice can be
            # charged to a PO that is not its primary one, and freeing budget there
            # is just as relevant to it.
            cur.execute(
                """SELECT DISTINCT r.* FROM runs r JOIN run_allocations a ON a.run_id = r.id
                   WHERE a.po_number=%s AND r.status='NEEDS_REVIEW' ORDER BY r.id ASC""",
                (po_number,))
            return [_hydrate(dict(r)) for r in cur.fetchall()]
    finally:
        conn.close()


def save_run(filename, status, extracted: dict, po_match: dict, stages: list, reasons: list,
            uploaded_by=None):
    # write_txn(), not a bare get_conn(): the run row and its allocation rows
    # must land as one unit, or a failure between the two would leave a run
    # with no allocations -- an invoice charged to nothing, silently reading
    # as though no budget was ever consumed. See save_run_checked for the
    # commit-time PO recheck; this path (unreadable documents, see main.py's
    # _abort_unreadable) has no PO bound at all, so there is nothing to check,
    # only the same atomicity requirement.
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO runs (filename, status, created_at, vendor_name, invoice_number, total,
                   po_number, extracted_json, po_match_json, stages_json, reasons_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    filename,
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    extracted.get("vendor_name"),
                    extracted.get("invoice_number"),
                    extracted.get("total"),
                    po_match.get("po_number"),
                    json.dumps(extracted),
                    json.dumps(po_match),
                    json.dumps(stages),
                    json.dumps(reasons),
                ),
            )
            run_id = cur.fetchone()["id"]
            _write_allocations(conn, run_id, allocations_from_match(po_match, extracted.get("total")))
            _insert_activity(cur, run_id, "PROCESSING_COMPLETED", uploaded_by,
                             metadata={"status": status})
            if status == "NEEDS_REVIEW":
                _insert_activity(cur, run_id, "REVIEW_REQUIRED", None,
                                 metadata={"reason": reasons[0]["text"] if reasons else None})
        return run_id


def _hydrate(d: dict) -> dict:
    """Expand a run row's JSON columns. One definition, so a new column cannot be
    parsed in list_runs and forgotten in get_run."""
    for col, key in (("extracted_json", "extracted"), ("po_match_json", "po_match"),
                     ("stages_json", "stages"), ("reasons_json", "reasons"),
                     ("audit_json", "audit")):
        d[key] = json.loads(d.pop(col) or "null")
    return d


def list_runs(limit=200):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM runs ORDER BY id DESC LIMIT %s", (limit,))
            return [_hydrate(dict(r)) for r in cur.fetchall()]
    finally:
        conn.close()


def get_run(run_id):
    """A single run, hydrated, plus who (if anyone) currently holds its
    review claim -- so a caller opening one specific invoice (the exact
    scenario Phase D's collaborative review exists for) can see that state
    without a second round trip. Deliberately not done in list_runs(): that
    returns up to 200 rows in one call, and adding a claim lookup per row
    would turn a single query into a couple hundred for a bulk listing that
    does not need it."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
            if row is None:
                return None
            hydrated = _hydrate(dict(row))
    finally:
        conn.close()
    hydrated["current_claim"] = get_active_claim(run_id)
    return hydrated


# --------------------------------------------------------------------------
# Email security records (Phase F)
#
# Everything below stores what could be PROVEN about an incoming message and
# what people did about it. Nothing below stores the message.
# --------------------------------------------------------------------------
def list_trusted_senders():
    """The senders this business expects invoices from. Reference data,
    reloaded from JSON on startup -- there is deliberately no writer."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT sender, kind, vendor_name, status, note
                           FROM trusted_email_senders ORDER BY sender""")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _insert_email_activity(cur, email_id, event_type, actor, note=None, metadata=None):
    """Append one message-activity row on a caller's open cursor, so the event
    lands atomically with the write it describes. Same contract as
    `_insert_activity` for runs."""
    cur.execute(
        """INSERT INTO email_activity (email_id, event_type, actor, created_at, note, metadata_json)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
        (email_id, event_type, actor, datetime.now(timezone.utc).isoformat(), note,
         json.dumps(metadata) if metadata is not None else None),
    )
    return cur.fetchone()["id"]


def log_email_activity(email_id: int, event_type: str, actor: str = None, note: str = None,
                       metadata: dict = None):
    """Append one message-activity row in its own transaction, for callers
    with no open cursor of their own. Prefer `_insert_email_activity` when
    already inside a `write_txn()`."""
    with write_txn() as conn:
        with conn.cursor() as cur:
            return _insert_email_activity(cur, email_id, event_type, actor, note, metadata)


def list_email_activity(email_id: int):
    """The full chronological history for one message, oldest first."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, email_id, event_type, actor, created_at, note, metadata_json
                   FROM email_activity WHERE email_id=%s ORDER BY id ASC""", (email_id,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        r["metadata"] = json.loads(r.pop("metadata_json")) if r.get("metadata_json") else None
    return rows


def find_email_by_sha256(sha256: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM email_messages WHERE sha256=%s ORDER BY id ASC LIMIT 1",
                        (sha256,))
            row = cur.fetchone()
    finally:
        conn.close()
    return _hydrate_email(dict(row)) if row else None


def save_email_message(record: dict, submitted_by: str = None, source: str = "SUBMITTED"):
    """Persist one message's security record, with its arrival in the history.

    `record` is exactly what email_security.classify() returned. This function
    does not re-derive, re-check or second-guess any of it -- the evaluator
    owns the verdict, storage owns keeping it. Same division as
    rules.decide() and save_run_checked().
    """
    audit = record.get("audit") or {}
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO email_messages
                   (run_id, sha256, message_id, received_at, submitted_by, source,
                    from_address, from_domain, from_display_name, envelope_from, subject,
                    size_bytes, attachment_count, has_pdf_attachment,
                    spf_result, dkim_result, dmarc_result, dmarc_aligned,
                    signature_kind, signature_result, trusted_sender,
                    classification, status, reasons_json, auth_json)
                   VALUES (NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s)
                   RETURNING id""",
                (record["sha256"], record.get("message_id"),
                 datetime.now(timezone.utc).isoformat(), submitted_by, source,
                 record.get("from_address"), record.get("from_domain"),
                 record.get("from_display_name"), record.get("envelope_from"),
                 record.get("subject"), record.get("size_bytes"),
                 record.get("attachment_count"), bool(record.get("has_pdf_attachment")),
                 record.get("spf_result"), record.get("dkim_result"),
                 record.get("dmarc_result"), bool(record.get("dmarc_aligned")),
                 record.get("signature_kind"), record.get("signature_result"),
                 bool(record.get("trusted_sender")),
                 record["classification"], record["status"],
                 json.dumps(record.get("reasons") or []), json.dumps(audit)),
            )
            email_id = cur.fetchone()["id"]
            _insert_email_activity(
                cur, email_id, "MESSAGE_RECEIVED", submitted_by,
                metadata={"source": source, "sha256": record["sha256"],
                          "attachment_count": record.get("attachment_count")})
            # The evaluation is logged separately from the arrival because
            # they are separate facts: one day a message may be re-evaluated
            # (a trust anchor arrives, a resolver is configured) and the
            # history must be able to carry a second evaluation without
            # implying the message arrived twice.
            _insert_email_activity(
                cur, email_id, "AUTHENTICATION_EVALUATED", None,
                note="; ".join(record.get("reasons") or [])[:2000] or None,
                metadata={"classification": record["classification"],
                          "spf": record.get("spf_result"),
                          "dkim": record.get("dkim_result"),
                          "dmarc": record.get("dmarc_result"),
                          "signature": record.get("signature_result")})
            _insert_email_activity(
                cur, email_id,
                "QUARANTINED" if record["status"] == "QUARANTINED" else "ADMITTED", None,
                metadata={"classification": record["classification"]})
    return email_id


def _hydrate_email(row: dict) -> dict:
    row["reasons"] = json.loads(row.pop("reasons_json")) if row.get("reasons_json") else []
    row["audit"] = json.loads(row.pop("auth_json")) if row.get("auth_json") else None
    # Phase G columns. Popped the same way so a caller never has to know which
    # of these are stored as JSON text, and so `reasons_json`-style keys never
    # leak into an API response.
    row["triage"] = json.loads(row.pop("triage_json")) if row.get("triage_json") else None
    row["recipients"] = (json.loads(row.pop("recipients_json"))
                         if row.get("recipients_json") else [])
    row.pop("triage_json", None)
    row.pop("recipients_json", None)
    return row


def get_email_message(email_id: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM email_messages WHERE id=%s", (email_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    return _hydrate_email(dict(row)) if row else None


def list_email_messages(limit: int = 200, status: str = None):
    """Summary rows, newest first. `audit` is deliberately omitted -- it is
    the largest column by far and a list view never needs it, the same reason
    list_runs() does not carry each run's claim."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = """SELECT id, run_id, sha256, message_id, received_at, submitted_by, source,
                            from_address, from_domain, from_display_name, subject,
                            size_bytes, attachment_count, has_pdf_attachment,
                            spf_result, dkim_result, dmarc_result, dmarc_aligned,
                            signature_kind, signature_result, trusted_sender,
                            classification, status, reasons_json,
                            released_by, released_at, release_note
                     FROM email_messages"""
            params = []
            if status:
                sql += " WHERE status=%s"
                params.append(status)
            sql += " ORDER BY id DESC LIMIT %s"
            params.append(limit)
            cur.execute(sql, tuple(params))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        r["reasons"] = json.loads(r.pop("reasons_json")) if r.get("reasons_json") else []
    return rows


def set_email_status(email_id: int, new_status: str, actor: str = None, note: str = None):
    """Release or discard a quarantined message. One transaction, one lock.

    Built the way Phase E rebuilt record_human_review(), and for the same
    reason: the eligibility check and the write have to be one atomic step or
    two concurrent requests -- a double-clicked Release, a retry, or one
    reviewer releasing while another discards -- can both read "still
    quarantined" and both apply. `SELECT ... FOR UPDATE` on the message row
    means the second caller blocks until the first commits, then re-reads the
    status it is no longer allowed to change and is refused.

    A message may be ruled on ONCE. Releasing is a security decision with a
    consequence (the message becomes eligible to be processed), so it gets
    the same single-ruling discipline a human invoice review gets.
    """
    if new_status not in ("RELEASED", "DISCARDED"):
        return {"ok": False, "error": f"unsupported status {new_status!r}"}
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, status, classification FROM email_messages WHERE id=%s "
                        "FOR UPDATE", (email_id,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": "unknown message"}
            current = row["status"]
            if current == "ADMITTED":
                # Nothing to release: it was never held. Refused rather than
                # silently accepted, so a caller cannot record a "release"
                # that did not happen.
                return {"ok": False,
                        "error": "this message was admitted and is not quarantined"}
            if current != "QUARANTINED":
                return {"ok": False,
                        "error": f"this message has already been ruled on ({current.lower()})"}
            now = datetime.now(timezone.utc).isoformat()
            cur.execute(
                """UPDATE email_messages
                   SET status=%s, released_by=%s, released_at=%s, release_note=%s
                   WHERE id=%s""",
                (new_status, actor, now, note, email_id))
            _insert_email_activity(
                cur, email_id, new_status, actor, note=note,
                metadata={"from_status": current, "to_status": new_status,
                          "classification": row["classification"]})
    return {"ok": True, "id": email_id, "status": new_status, "by": actor}


def link_email_to_run(email_id: int, run_id: int):
    """Record that a run was produced from this message.

    Nothing in Phase F calls this -- creating a run from an email attachment
    is Phase G. It exists, and is tested, because the join it enables is the
    whole reason `email_messages.run_id` was added now rather than later: the
    security record and the invoice it produced have to be answerable
    together ("what did we know about the sender of the message this run came
    from?") without a schema migration to ask.
    """
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, status FROM email_messages WHERE id=%s FOR UPDATE",
                        (email_id,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": "unknown message"}
            if row["status"] not in ("ADMITTED", "RELEASED"):
                # A quarantined or discarded message must not be able to
                # acquire a run: that would mean it reached the pipeline
                # without passing the gate this phase exists to impose.
                return {"ok": False,
                        "error": f"a message with status {row['status'].lower()} may not be "
                                 "processed; release it first"}
            cur.execute("SELECT id FROM runs WHERE id=%s", (run_id,))
            if not cur.fetchone():
                return {"ok": False, "error": "unknown run"}
            cur.execute("UPDATE email_messages SET run_id=%s WHERE id=%s", (run_id, email_id))
            _insert_email_activity(cur, email_id, "LINKED_TO_RUN", None,
                                   metadata={"run_id": run_id})
    return {"ok": True, "id": email_id, "run_id": run_id}


# --------------------------------------------------------------------------
# Email ingestion state (Phase G)
#
# Phase F recorded what could be PROVEN about a message. This records what
# HAPPENED to it: whether we had seen it before, whether it was worth
# processing, and what each attachment became.
# --------------------------------------------------------------------------
def claim_incoming_message(provider: str, provider_message_id: str, record: dict,
                           triage: dict, ingest_status: str,
                           provider_received_at: str = None,
                           submitted_by: str = None):
    """Record a newly-arrived message, or report that it is a duplicate.

    THE IDEMPOTENCY CHOKE POINT. Returns
    `{"created": True, "id": n}` for a message never seen before, or
    `{"created": False, "id": n, "duplicate": True}` when this provider has
    already delivered this message id.

    The duplicate test is the UNIQUE INDEX, not a SELECT: two pollers racing
    the same message would both pass a check-then-insert, and both would write.
    Here the loser's INSERT is refused by the database however the race is
    timed, and it reads its own answer back. A savepoint is used so that
    refusal does not poison the caller's surrounding transaction.
    """
    audit = record.get("audit") or {}
    now = datetime.now(timezone.utc).isoformat()
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT claim_incoming")
            try:
                cur.execute(
                    """INSERT INTO email_messages
                       (run_id, sha256, message_id, received_at, submitted_by, source,
                        from_address, from_domain, from_display_name, envelope_from, subject,
                        size_bytes, attachment_count, has_pdf_attachment,
                        spf_result, dkim_result, dmarc_result, dmarc_aligned,
                        signature_kind, signature_result, trusted_sender,
                        classification, status, reasons_json, auth_json,
                        provider, provider_message_id, provider_received_at,
                        reply_to, recipients_json, sender_type, trust_status,
                        relevance, triage_json, ingest_status)
                       VALUES (NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING id""",
                    (record.get("sha256"), record.get("message_id"), now, submitted_by,
                     "EMAIL",
                     record.get("from_address"), record.get("from_domain"),
                     record.get("from_display_name"), record.get("envelope_from"),
                     record.get("subject"), record.get("size_bytes"),
                     record.get("attachment_count"), bool(record.get("has_pdf_attachment")),
                     record.get("spf_result"), record.get("dkim_result"),
                     record.get("dmarc_result"), bool(record.get("dmarc_aligned")),
                     record.get("signature_kind"), record.get("signature_result"),
                     bool(record.get("trusted_sender")),
                     record.get("classification") or "UNVERIFIED",
                     record.get("status") or "QUARANTINED",
                     json.dumps(record.get("reasons") or []), json.dumps(audit),
                     provider, provider_message_id, provider_received_at,
                     record.get("reply_to"), json.dumps(record.get("recipients") or []),
                     (triage.get("sender") or {}).get("sender_type"),
                     (triage.get("sender") or {}).get("trust_status"),
                     (triage.get("relevance") or {}).get("relevance"),
                     json.dumps(triage), ingest_status),
                )
                email_id = cur.fetchone()["id"]
            except psycopg2.IntegrityError:
                # Somebody got here first. Roll back only the failed INSERT.
                cur.execute("ROLLBACK TO SAVEPOINT claim_incoming")
                cur.execute(
                    """SELECT id FROM email_messages
                       WHERE provider=%s AND provider_message_id=%s""",
                    (provider, provider_message_id))
                row = cur.fetchone()
                if row:
                    _insert_email_activity(
                        cur, row["id"], "DUPLICATE_DELIVERY", submitted_by,
                        note="the provider delivered this message again; it was not reprocessed",
                        metadata={"provider": provider,
                                  "provider_message_id": provider_message_id})
                    return {"created": False, "duplicate": True, "id": row["id"]}
                raise
            cur.execute("RELEASE SAVEPOINT claim_incoming")
            _insert_email_activity(
                cur, email_id, "MESSAGE_RECEIVED", submitted_by,
                metadata={"provider": provider, "provider_message_id": provider_message_id,
                          "source": "EMAIL"})
            _insert_email_activity(
                cur, email_id, "TRIAGED", None,
                note="; ".join((triage.get("relevance") or {}).get("reasons") or [])[:2000] or None,
                metadata={"sender_type": (triage.get("sender") or {}).get("sender_type"),
                          "trust_status": (triage.get("sender") or {}).get("trust_status"),
                          "relevance": (triage.get("relevance") or {}).get("relevance"),
                          "proceed": triage.get("proceed")})
            if record.get("classification"):
                _insert_email_activity(
                    cur, email_id, "AUTHENTICATION_EVALUATED", None,
                    note="; ".join(record.get("reasons") or [])[:2000] or None,
                    metadata={"classification": record.get("classification"),
                              "spf": record.get("spf_result"),
                              "dkim": record.get("dkim_result"),
                              "dmarc": record.get("dmarc_result"),
                              "signature": record.get("signature_result")})
            _insert_email_activity(cur, email_id, ingest_status, None,
                                   metadata={"ingest_status": ingest_status})
    return {"created": True, "duplicate": False, "id": email_id}


def set_ingest_status(email_id: int, ingest_status: str, error: str = None,
                      actor: str = None, note: str = None, event: str = None):
    """Move a message's ingestion state, recording the move in its history.

    Separate from `set_email_status()`, which owns the SECURITY status
    (quarantine, release, discard). Those are different questions -- "may this
    be processed" versus "how far did processing get" -- and merging them would
    let an ingestion retry quietly overwrite a reviewer's ruling.
    """
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM email_messages WHERE id=%s FOR UPDATE", (email_id,))
            if not cur.fetchone():
                return {"ok": False, "error": "unknown message"}
            cur.execute(
                """UPDATE email_messages SET ingest_status=%s, ingest_error=%s,
                       processed_at=CASE WHEN %s IN ('PROCESSED','PARTIAL','FAILED')
                                         THEN %s ELSE processed_at END
                   WHERE id=%s""",
                (ingest_status, error, ingest_status,
                 datetime.now(timezone.utc).isoformat(), email_id))
            _insert_email_activity(cur, email_id, event or ingest_status, actor,
                                   note=note or error,
                                   metadata={"ingest_status": ingest_status})
    return {"ok": True, "id": email_id, "ingest_status": ingest_status}


def record_attachments(email_id: int, attachments: list):
    """Register what arrived with the message. Idempotent.

    `ON CONFLICT DO NOTHING` against the (email_id, sha256) unique index: a
    redelivery, or a retry after a partial failure, must not append a second
    copy of every attachment, and must not reset the status of one that has
    already been processed.
    """
    out = []
    with write_txn() as conn:
        with conn.cursor() as cur:
            now = datetime.now(timezone.utc).isoformat()
            for seq, att in enumerate(attachments or []):
                cur.execute(
                    """INSERT INTO email_attachments
                       (email_id, seq, filename, content_type, size_bytes, sha256,
                        is_invoice_candidate, status, skip_reason, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (email_id, sha256) WHERE sha256 IS NOT NULL
                       DO NOTHING
                       RETURNING id""",
                    (email_id, seq, att.get("filename"), att.get("content_type"),
                     att.get("size_bytes"), att.get("sha256"),
                     bool(att.get("is_invoice_candidate")),
                     att.get("status") or "PENDING", att.get("skip_reason"), now))
                row = cur.fetchone()
                out.append(row["id"] if row else None)
    return out


def list_email_attachments(email_id: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT * FROM email_attachments WHERE email_id=%s
                           ORDER BY seq, id""", (email_id,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def set_attachment_storage(attachment_id: int, storage_backend: str, storage_key: str):
    """Record where a held attachment's bytes are kept (or clear it)."""
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE email_attachments SET storage_backend=%s, storage_key=%s
                           WHERE id=%s""", (storage_backend, storage_key, attachment_id))
    return {"ok": True, "id": attachment_id}


def claim_attachment_for_processing(attachment_id: int):
    """Take an attachment for processing, or report that it is already done.

    Locks the row, so two concurrent ingestion passes over the same message
    cannot both start work on the same attachment and produce two runs from one
    PDF. Returns False when the row is already PROCESSED -- which is what makes
    a retry after a partial failure resume rather than duplicate.
    """
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, status, run_id FROM email_attachments WHERE id=%s "
                        "FOR UPDATE", (attachment_id,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": "unknown attachment"}
            if row["status"] == "PROCESSED":
                return {"ok": False, "already": True, "run_id": row["run_id"]}
            return {"ok": True, "id": attachment_id}


def complete_attachment(attachment_id: int, status: str, run_id: int = None,
                        run_status: str = None, error: str = None,
                        skip_reason: str = None):
    """Record what an attachment became. Also links the run back to the message.

    The FIRST run an email produces is also written to `email_messages.run_id`,
    so Phase F's single-run link keeps working unchanged; every run, including
    that first one, is on its own `email_attachments` row, which is what makes
    an email with three invoices representable at all.
    """
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE email_attachments
                   SET status=%s, run_id=%s, run_status=%s, error=%s,
                       skip_reason=COALESCE(%s, skip_reason), processed_at=%s
                   WHERE id=%s RETURNING email_id""",
                (status, run_id, run_status, error, skip_reason,
                 datetime.now(timezone.utc).isoformat(), attachment_id))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": "unknown attachment"}
            email_id = row["email_id"]
            if run_id is not None:
                cur.execute(
                    "UPDATE email_messages SET run_id=%s WHERE id=%s AND run_id IS NULL",
                    (run_id, email_id))
                _insert_email_activity(
                    cur, email_id, "INVOICE_RUN_CREATED", None,
                    metadata={"run_id": run_id, "run_status": run_status,
                              "attachment_id": attachment_id})
            elif status in ("SKIPPED", "FAILED"):
                _insert_email_activity(
                    cur, email_id,
                    "ATTACHMENT_SKIPPED" if status == "SKIPPED" else "ATTACHMENT_FAILED",
                    None, note=(skip_reason or error),
                    metadata={"attachment_id": attachment_id})
    return {"ok": True, "id": attachment_id, "email_id": email_id}


def email_for_provider_message(provider: str, provider_message_id: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT * FROM email_messages
                           WHERE provider=%s AND provider_message_id=%s""",
                        (provider, provider_message_id))
            row = cur.fetchone()
    finally:
        conn.close()
    return _hydrate_email(dict(row)) if row else None


def ingestion_summary():
    """Counts by ingestion state and by relevance, for the admin view.

    Aggregated in the database rather than by reading every row: this is the
    one query an operations screen refreshes, and it should stay cheap however
    many messages have accumulated.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(ingest_status,'(none)') k, COUNT(*) n
                           FROM email_messages GROUP BY 1 ORDER BY 1""")
            by_status = {r["k"]: r["n"] for r in cur.fetchall()}
            cur.execute("""SELECT COALESCE(relevance,'(none)') k, COUNT(*) n
                           FROM email_messages GROUP BY 1 ORDER BY 1""")
            by_relevance = {r["k"]: r["n"] for r in cur.fetchall()}
            cur.execute("""SELECT COALESCE(classification,'(none)') k, COUNT(*) n
                           FROM email_messages GROUP BY 1 ORDER BY 1""")
            by_classification = {r["k"]: r["n"] for r in cur.fetchall()}
            cur.execute("""SELECT COALESCE(status,'(none)') k, COUNT(*) n
                           FROM email_messages GROUP BY 1 ORDER BY 1""")
            by_security_status = {r["k"]: r["n"] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) n FROM email_attachments WHERE run_id IS NOT NULL")
            runs_created = cur.fetchone()["n"]
    finally:
        conn.close()
    return {"by_ingest_status": by_status, "by_relevance": by_relevance,
            "by_classification": by_classification,
            "by_security_status": by_security_status,
            "invoice_runs_created": runs_created}


def clear_run_history():
    """Delete every processed run, leaving reference data untouched.

    WHY THIS EXISTS

    The sample invoices are deliberately history-dependent: the split-PO story
    only works as 02 -> 03 -> 03b, and 06 is only a duplicate because 01 ran
    first. Since every run is recorded, a second pass through the samples makes
    01 a duplicate of itself and leaves PO-1001 with no budget -- the engine is
    still right, but the samples stop demonstrating what they were written to
    demonstrate. Clearing the history is what makes them repeatable.

    Deliberately narrow. Purchase orders, vendors and users are seed data owned
    by data/*.json and are re-seeded on startup; this touches only `runs` (and,
    since Phase C, the document rows/files that belong to those runs), so it
    cannot destroy anything that is not reproducible by re-running an invoice.

    Runs inside the same write transaction the ledger uses, so it cannot land
    half-applied beside an in-flight approval.
    """
    with write_txn() as conn:
        with conn.cursor() as cur:
            # Read what to delete from the store BEFORE the rows are gone --
            # once the transaction commits there is no way back to the keys.
            cur.execute("SELECT storage_backend, storage_key FROM documents")
            to_delete = [dict(r) for r in cur.fetchall()]
            # Allocations, documents, activity and claims are all rows ABOUT
            # runs, and leaving them behind would charge every PO against
            # invoices that no longer exist, point the document viewer or the
            # activity timeline at a run that is gone, or leave a claim
            # dangling on nothing. Deleted before `runs` itself for the same
            # reason: Postgres enforces the foreign key.
            cur.execute("DELETE FROM run_allocations")
            cur.execute("DELETE FROM documents")
            cur.execute("DELETE FROM review_claims")
            cur.execute("DELETE FROM invoice_activity")
            # Phase F: an email security record is NOT run history. It records
            # what could be proven about a sender, which stays true whether or
            # not the invoice it carried is still on file -- and deleting a
            # security finding because someone reset a demo would be the wrong
            # instinct to build in. So the link is dropped (the run it pointed
            # at is about to stop existing, and the foreign key would refuse
            # the delete otherwise) and the record itself is kept.
            cur.execute("UPDATE email_messages SET run_id=NULL WHERE run_id IS NOT NULL")
            # Phase G: same reasoning one level down. The attachment record --
            # what arrived, whether it was a usable PDF, why it was skipped --
            # is ingestion history, not run history, and survives. Only the
            # pointer to the vanishing run is dropped, and the row is put back
            # to PENDING so a demo replay can process it again rather than
            # believing it is already done.
            cur.execute("""UPDATE email_attachments
                           SET run_id=NULL, run_status=NULL,
                               status=CASE WHEN status='PROCESSED' THEN 'PENDING' ELSE status END
                           WHERE run_id IS NOT NULL""")
            cur.execute("""UPDATE email_messages
                           SET ingest_status=CASE WHEN ingest_status IN ('PROCESSED','PARTIAL')
                                                  THEN 'RECEIVED' ELSE ingest_status END
                           WHERE ingest_status IS NOT NULL""")
            cur.execute("DELETE FROM runs")
            deleted = cur.rowcount

    # Best-effort, outside the transaction and never fatal: a reset that fails
    # to remove a file on disk must not fail to clear the run history, which
    # is the actual guarantee reset-demo exists to provide (§9 issue 3).
    for doc in to_delete:
        try:
            if doc.get("storage_backend") == config.document_store_backend():
                documents.get_store().delete(doc["storage_key"])
        except Exception:
            pass
    return deleted
