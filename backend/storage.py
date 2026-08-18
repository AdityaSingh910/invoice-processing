"""SQLite persistence: seed data (POs/vendors) + run history / dashboard."""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "app.db")
PO_SEED = os.path.join(os.path.dirname(__file__), "..", "data", "purchase_orders.json")
VENDOR_SEED = os.path.join(os.path.dirname(__file__), "..", "data", "approved_vendors.json")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(reset_runs: bool = False):
    conn = get_conn()
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


def find_vendor(name: str):
    """Loose match: exact, then case-insensitive, then substring both ways."""
    if not name:
        return None
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM vendors").fetchall()]
    conn.close()
    name_norm = name.strip().lower()
    for v in rows:
        if v["vendor_name"].strip().lower() == name_norm:
            return v
    for v in rows:
        vn = v["vendor_name"].strip().lower()
        if vn in name_norm or name_norm in vn:
            return v
    return None


def consumed_amount_for_po(po_number: str, exclude_run_id=None):
    """Sum of totals from APPROVED runs already matched to this PO."""
    conn = get_conn()
    q = "SELECT id, total FROM runs WHERE po_number=? AND status='APPROVED'"
    params = [po_number]
    if exclude_run_id is not None:
        q += " AND id != ?"
        params.append(exclude_run_id)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return sum(r["total"] or 0 for r in rows)


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


def list_runs(limit=200):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["extracted"] = json.loads(d.pop("extracted_json"))
        d["po_match"] = json.loads(d.pop("po_match_json"))
        d["stages"] = json.loads(d.pop("stages_json"))
        d["reasons"] = json.loads(d.pop("reasons_json"))
        out.append(d)
    return out


def get_run(run_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["extracted"] = json.loads(d.pop("extracted_json"))
    d["po_match"] = json.loads(d.pop("po_match_json"))
    d["stages"] = json.loads(d.pop("stages_json"))
    d["reasons"] = json.loads(d.pop("reasons_json"))
    return d
