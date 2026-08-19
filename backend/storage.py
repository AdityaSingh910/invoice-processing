"""SQLite persistence: seed data (POs/vendors) + run history / dashboard."""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "app.db")
PO_SEED = os.path.join(os.path.dirname(__file__), "..", "data", "purchase_orders.json")
VENDOR_SEED = os.path.join(os.path.dirname(__file__), "..", "data", "approved_vendors.json")


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def write_txn():
    """A serialised read-modify-write against the PO ledger.

    `BEGIN IMMEDIATE` takes the write lock at the START of the transaction rather
    than at first write. That is the whole point: the balance check and the row
    that consumes the balance have to be inside one lock, or two invoices for the
    same PO can each read the same remaining balance and both approve, committing
    more than the PO authorised.

    Without this, every storage call opened its own connection, so a transaction
    could not span read-balance -> decide -> write. This was documented as the one
    known defect that produces a wrong NUMBER rather than a wrong routing.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
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
    copy -- including `data/app.db`, which carries real run history. SQLite has
    no `ADD COLUMN IF NOT EXISTS`, so the existing columns are read first.
    """
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _consumed(conn, po_number, exclude_run_id=None):
    """Balance consumed on a PO, read on a caller-supplied connection.

    Deliberately derived from run history rather than read off a stored counter.
    Nothing to deduct means nothing to deduct twice, so re-evaluating a run can
    never double-count it, and reversing one refunds the balance by definition.
    """
    q = "SELECT COALESCE(SUM(total), 0) AS c FROM runs WHERE po_number=? AND status='APPROVED'"
    params = [po_number]
    if exclude_run_id is not None:
        q += " AND id != ?"
        params.append(exclude_run_id)
    return conn.execute(q, params).fetchone()["c"] or 0.0


def init_db(reset_runs: bool = False):
    conn = get_conn()
    # WAL lets readers proceed while a writer holds the lock, so serialising the
    # ledger write does not serialise the whole app.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass   # e.g. a filesystem that cannot support WAL; correctness is unaffected
    cur = conn.cursor()
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
    # Existing databases predate the provenance columns.
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    conn.execute("""UPDATE runs SET automated_decision = status
                    WHERE automated_decision IS NULL""")
    conn.execute("""UPDATE runs SET final_decision = status
                    WHERE final_decision IS NULL""")
    if reset_runs:
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
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (po["po_number"], po["vendor"], po["amount"], po["currency"],
             po["issued_date"], po["status"], po["description"],
             po.get("source_file") or source_file, row_no),
        )

    with open(VENDOR_SEED) as f:
        vendors = json.load(f)
    cur.execute("DELETE FROM vendors")
    for v in vendors:
        cur.execute("INSERT INTO vendors VALUES (?,?,?)", (v["vendor_name"], v["vendor_id"], v["status"]))

    conn.commit()
    conn.close()


def list_purchase_orders():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM purchase_orders ORDER BY po_number").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_vendors():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM vendors ORDER BY vendor_name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_po(po_number: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM purchase_orders WHERE UPPER(po_number)=UPPER(?)", (po_number,)).fetchone()
    conn.close()
    return dict(row) if row else None


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
    rows = [dict(r) for r in conn.execute("SELECT * FROM vendors").fetchall()]
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
        row = conn.execute("SELECT amount FROM purchase_orders WHERE UPPER(po_number)=UPPER(?)",
                           (po_number,)).fetchone()
        if row is None:
            return None
        return round(row["amount"] - _consumed(conn, po_number, exclude_run_id), 2)
    finally:
        conn.close()


def find_duplicate(vendor_name, invoice_number, total):
    if not invoice_number:
        return None
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM runs WHERE invoice_number=? AND ABS(COALESCE(total,-1) - ?) < 0.01
           AND (vendor_name=? OR vendor_name IS NULL) ORDER BY created_at ASC LIMIT 1""",
        (invoice_number, total if total is not None else -999999, vendor_name),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_run_checked(filename, status, extracted: dict, po_match: dict, stages: list,
                     reasons: list, tolerance_for=None, audit=None):
    """Persist a run, re-verifying the PO balance under the write lock first.

    The pipeline computes its verdict outside any transaction -- it has to, since
    extraction can take seconds and holding a write lock across a model call
    would serialise the whole system. So the balance it decided against may be
    stale by the time it commits.

    This is optimistic concurrency with an authoritative final check: re-read the
    consumed total inside BEGIN IMMEDIATE, and if the invoice no longer fits,
    downgrade APPROVED to NEEDS_REVIEW *before* inserting. Two concurrent
    invoices for one PO can no longer both approve past the balance -- whichever
    commits second sees the first and is held for a human.

    Returns (run_id, final_status, extra_reason_or_None).
    """
    po_number = po_match.get("po_number")
    total = extracted.get("total")
    extra = None

    with write_txn() as conn:
        if status == "APPROVED" and po_number and total is not None:
            row = conn.execute(
                "SELECT amount FROM purchase_orders WHERE UPPER(po_number)=UPPER(?)",
                (po_number,)).fetchone()
            if row is not None:
                remaining = round(row["amount"] - _consumed(conn, po_number), 2)
                tol = tolerance_for(remaining if remaining > 0 else row["amount"]) \
                    if tolerance_for else 0.0
                if round(total - remaining, 2) > tol:
                    status = "NEEDS_REVIEW"
                    extra = {
                        "text": (
                            f"Balance changed while this invoice was being processed: "
                            f"${remaining:.2f} remained on {po_number} at commit time, against a "
                            f"${total:.2f} invoice. Another invoice consumed the PO first, so this "
                            f"one was held rather than approved past the authorised amount."
                        ),
                        "level": "fail",
                    }
                    reasons = list(reasons) + [extra]
                    # Keep the stored snapshot honest about what was committed.
                    po_match = dict(po_match, remaining_before=remaining,
                                    remaining_after=remaining,
                                    diff=round(total - remaining, 2),
                                    within_tolerance=False)

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
                     detail="PO balance changed before commit; re-checked under the write lock",
                     reason="Invoice total exceeds PO remaining amount.")
                if c.get("name") == "PO remaining check" else c
                for c in (audit.get("rules") or [])
            ]
            audit["rules_passed"] = [c["name"] for c in audit["rules"] if c["passed"]]
            audit["rules_failed"] = [c["name"] for c in audit["rules"] if not c["passed"]]

        cur = conn.execute(
            """INSERT INTO runs (filename, status, created_at, vendor_name, invoice_number, total,
               po_number, extracted_json, po_match_json, stages_json, reasons_json, audit_json,
               automated_decision, final_decision)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (filename, status, datetime.now(timezone.utc).isoformat(),
             extracted.get("vendor_name"), extracted.get("invoice_number"),
             extracted.get("total"), po_number, json.dumps(extracted),
             json.dumps(po_match), json.dumps(stages), json.dumps(reasons),
             json.dumps(audit) if audit is not None else None,
             # The decision this process reached on its own, recorded once and
             # never rewritten. `status` may later move; this must not.
             status, status),
        )
        return cur.lastrowid, status, extra


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
        row = conn.execute("SELECT status, po_number, reasons_json FROM runs WHERE id=?",
                           (run_id,)).fetchone()
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
        conn.execute("UPDATE runs SET status=?, reasons_json=? WHERE id=?",
                     (new_status, json.dumps(reasons), run_id))
        # An automated status change (a cascade re-evaluation, an operator
        # reversal) moves the final decision with it. A run a human has already
        # ruled on keeps its HUMAN_* outcome -- that verdict belongs to a person
        # and is not something a later automated pass gets to relabel.
        conn.execute("""UPDATE runs SET final_decision=?
                        WHERE id=? AND human_decision IS NULL""",
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
    decision = (decision or "").strip().upper()
    if decision not in HUMAN_OUTCOMES:
        return {"ok": False, "error": "decision must be ACCEPTED or REJECTED"}
    new_status, final_decision = HUMAN_OUTCOMES[decision]

    conn = get_conn()
    row = conn.execute(
        "SELECT status, automated_decision, human_decision, po_number FROM runs WHERE id=?",
        (run_id,)).fetchone()
    conn.close()
    if row is None:
        return {"ok": False, "error": "unknown run"}

    # Runs written before these columns existed fall back to their status, which
    # at that point was the automated decision.
    automated = row["automated_decision"] or row["status"]
    if automated != "NEEDS_REVIEW":
        return {"ok": False,
                "error": f"only NEEDS_REVIEW runs can be reviewed (this one is {automated})"}

    ok, old_status, po_number = set_run_status(
        run_id, new_status,
        note or f"Human review: {decision} by {reviewer or 'an unattributed reviewer'}.")
    if not ok:
        return {"ok": False, "error": "could not update run status"}

    reviewer = (reviewer or "").strip() or None    # never invent an identity
    reviewed_at = datetime.now(timezone.utc).isoformat()
    with write_txn() as conn:
        conn.execute(
            """UPDATE runs SET human_decision=?, final_decision=?, reviewed_by=?,
               reviewed_at=?, review_note=? WHERE id=?""",
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
        rows = conn.execute(
            "SELECT * FROM runs WHERE po_number=? AND status='NEEDS_REVIEW' ORDER BY id ASC",
            (po_number,)).fetchall()
        return [_hydrate(dict(r)) for r in rows]
    finally:
        conn.close()


def save_run(filename, status, extracted: dict, po_match: dict, stages: list, reasons: list):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO runs (filename, status, created_at, vendor_name, invoice_number, total,
           po_number, extracted_json, po_match_json, stages_json, reasons_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
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
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
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
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [_hydrate(dict(r)) for r in rows]


def get_run(run_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    return _hydrate(dict(row)) if row else None
