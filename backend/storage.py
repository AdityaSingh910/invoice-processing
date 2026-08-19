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
            description TEXT
        )"""
    )
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
            reasons_json TEXT
        )"""
    )
    if reset_runs:
        cur.execute("DELETE FROM runs")

    # (re)load seed reference data every start so edits to the JSON files take effect
    with open(PO_SEED) as f:
        pos = json.load(f)
    cur.execute("DELETE FROM purchase_orders")
    for po in pos:
        cur.execute(
            "INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?)",
            (po["po_number"], po["vendor"], po["amount"], po["currency"], po["issued_date"], po["status"], po["description"]),
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
                     reasons: list, tolerance_for=None):
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

        cur = conn.execute(
            """INSERT INTO runs (filename, status, created_at, vendor_name, invoice_number, total,
               po_number, extracted_json, po_match_json, stages_json, reasons_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (filename, status, datetime.now(timezone.utc).isoformat(),
             extracted.get("vendor_name"), extracted.get("invoice_number"),
             extracted.get("total"), po_number, json.dumps(extracted),
             json.dumps(po_match), json.dumps(stages), json.dumps(reasons)),
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
        return True, old_status, po_number


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
                     ("stages_json", "stages"), ("reasons_json", "reasons")):
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
