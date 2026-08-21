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
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import psycopg2.pool

import config

PO_SEED = os.path.join(os.path.dirname(__file__), "..", "data", "purchase_orders.json")
VENDOR_SEED = os.path.join(os.path.dirname(__file__), "..", "data", "approved_vendors.json")

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
            # Read heavily by find_duplicate() (invoice_number equality) and by every
            # ledger query that filters on status -- neither existed as an index under
            # SQLite because a single-file database at this volume never needed one;
            # a shared server under concurrent load benefits from both.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_invoice_number ON runs(invoice_number)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)")

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
                     reasons: list, tolerance_for=None, audit=None):
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
        return run_id, status, extra


def set_run_status(run_id: int, new_status: str, note: str = None):
    """Change a run's status. This is the reversal mechanism.

    There is no balance to refund. Consumption is DERIVED -- `_consumed` sums
    APPROVED runs -- so flipping a run out of APPROVED removes it from that sum
    in the same instant, and flipping it back restores it. A stored counter would
    need an explicit refund here, and would be one missed code path away from a
    PO that can never be spent again.

    Returns (ok, old_status, po_number).
    """
    if new_status not in ("APPROVED", "NEEDS_REVIEW", "REJECTED"):
        return False, None, None

    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, po_number, reasons_json FROM runs WHERE id=%s",
                       (run_id,))
            row = cur.fetchone()
            if row is None:
                return False, None, None
            old_status, po_number = row["status"], row["po_number"]
            if old_status == new_status:
                return True, old_status, po_number

            reasons = json.loads(row["reasons_json"] or "[]")
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
    Moving it through `set_run_status` rather than with a bare UPDATE is
    deliberate -- that path already handles reversal correctly and is what the
    cascade re-evaluation hangs off.

    Only a run whose AUTOMATED decision was NEEDS_REVIEW is eligible. An invoice
    the process rejected outright is not something to wave through from a review
    screen, and one it approved needs no ruling.

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

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, automated_decision, human_decision, po_number FROM runs WHERE id=%s",
                (run_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": "unknown run"}

    # Runs written before these columns existed fall back to their status, which
    # at that point was the automated decision.
    automated = row["automated_decision"] or row["status"]
    if automated != "NEEDS_REVIEW":
        return {"ok": False,
                "error": f"only NEEDS_REVIEW runs can be reviewed (this one is {automated})"}

    # A run may be ruled on ONCE. Because `automated_decision` deliberately stays
    # NEEDS_REVIEW forever, the eligibility check above keeps passing after a
    # ruling -- which would let a caller post again and quietly rewrite who
    # decided, what they decided and when. That is precisely the audit record
    # that must not be editable from a client. Reversing a ruling is an
    # administrative act and goes through the status endpoint, which requires
    # 'invoice:admin' and leaves its own trail.
    if row["human_decision"]:
        return {"ok": False,
                "error": f"this run has already been reviewed ({row['human_decision']})"}

    ok, old_status, po_number = set_run_status(
        run_id, new_status,
        note or f"Human review: {decision} by {reviewer or 'an unattributed reviewer'}.")
    if not ok:
        return {"ok": False, "error": "could not update run status"}

    reviewer = (reviewer or "").strip() or None    # never invent an identity
    reviewed_at = datetime.now(timezone.utc).isoformat()
    with write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE runs SET human_decision=%s, final_decision=%s, reviewed_by=%s,
                   reviewed_at=%s, review_note=%s WHERE id=%s""",
                (decision, final_decision, reviewer, reviewed_at, note, run_id))

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


def save_run(filename, status, extracted: dict, po_match: dict, stages: list, reasons: list):
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
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM runs WHERE id=%s", (run_id,))
            row = cur.fetchone()
            return _hydrate(dict(row)) if row else None
    finally:
        conn.close()


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
    by data/*.json and are re-seeded on startup; this touches only `runs`, so it
    cannot destroy anything that is not reproducible by re-running an invoice.

    Runs inside the same write transaction the ledger uses, so it cannot land
    half-applied beside an in-flight approval.
    """
    with write_txn() as conn:
        with conn.cursor() as cur:
            # Allocations first: they are rows ABOUT runs, and leaving them behind
            # would charge every PO against invoices that no longer exist.
            cur.execute("DELETE FROM run_allocations")
            cur.execute("DELETE FROM runs")
            deleted = cur.rowcount
    return deleted
