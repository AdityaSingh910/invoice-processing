"""The allocation ledger: which PO each run charged, and how much.

WHAT CHANGED AND WHY IT IS SAFE

Consumption used to be `SUM(runs.total) WHERE po_number=?`, which assumed one PO
per invoice. It now sums `run_allocations` instead. The properties the old design
was chosen for are deliberately preserved, and this file exists to prove it:

* **Still derived, not stored.** An allocation is an immutable fact -- this run
  billed $X to PO-Y. Whether it COUNTS is decided at read time by joining to
  `runs.status='APPROVED'`. There is no balance column, so there is no refund
  step to forget.
* **Idempotency and reversal stay structural.** Nothing is deducted, so nothing
  can be deducted twice; moving a run out of APPROVED drops its allocations from
  the sum in the same instant.
* **The migration moves no money.** A run written before this table existed
  carries its charge in (po_number, total) and is backfilled to the single
  allocation row it always implied.

The invariant every test here leans on: **the allocations for a run sum to that
run's total.** If that breaks, the ledger is describing money nobody billed.
"""
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import matching    # noqa: E402
import storage     # noqa: E402

VENDOR = "Globex Logistics"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "alloc.db"))
    storage.init_db(reset_runs=True)
    return storage.DB_PATH


def _save(po_number, total, status="APPROVED", filename="x.pdf", allocations=None):
    """Commit a run through the real path the pipeline uses."""
    extracted = {"vendor_name": VENDOR, "invoice_number": filename, "total": total}
    po_match = dict(matching.empty_match(total), po_number=po_number)
    if allocations is not None:
        po_match["allocations"] = allocations
    run_id, final, _ = storage.save_run_checked(
        filename, status, extracted, po_match, [], [],
        tolerance_for=matching.tolerance_for, audit={})
    return run_id


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(
        "SELECT run_id, po_number, amount, seq FROM run_allocations ORDER BY run_id, seq")]
    conn.close()
    return out


# --------------------------------------------------------------------------
# a single-PO run is just a run with one allocation
# --------------------------------------------------------------------------

def test_a_single_po_run_writes_exactly_one_allocation_for_its_full_total(db):
    run_id = _save("PO-1002", 3000.0)
    allocs = storage.allocations_for_run(run_id)
    assert len(allocs) == 1
    assert allocs[0]["po_number"] == "PO-1002"
    assert allocs[0]["amount"] == 3000.0


def test_allocations_sum_to_the_run_total(db):
    """The invariant the whole ledger rests on."""
    run_id = _save("PO-1002", 3000.0)
    assert sum(a["amount"] for a in storage.allocations_for_run(run_id)) == 3000.0


def test_a_run_with_no_po_allocates_nothing(db):
    """An invoice bound to no PO consumes no budget anywhere -- it must not
    quietly charge itself to something."""
    run_id = _save(None, 500.0, status="NEEDS_REVIEW")
    assert storage.allocations_for_run(run_id) == []
    assert _rows(db) == []


def test_consumption_reads_allocations_not_run_totals(db):
    _save("PO-1002", 3000.0)
    _save("PO-1002", 2000.0)
    assert storage.consumed_amount_for_po("PO-1002") == 5000.0
    assert storage.remaining_for_po("PO-1002") == 0.0


# --------------------------------------------------------------------------
# only APPROVED runs consume -- the join, not the row, decides
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["NEEDS_REVIEW", "REJECTED"])
def test_an_unapproved_run_has_allocations_that_do_not_count(db, status):
    """The row is written regardless. That is what lets a later human approval
    consume the budget without re-deriving anything."""
    run_id = _save("PO-1002", 2000.0, status=status)
    assert len(storage.allocations_for_run(run_id)) == 1
    assert storage.consumed_amount_for_po("PO-1002") == 0.0


def test_reversal_still_refunds_structurally(db):
    run_id = _save("PO-1002", 3000.0)
    assert storage.consumed_amount_for_po("PO-1002") == 3000.0

    storage.set_run_status(run_id, "NEEDS_REVIEW", "reversed")
    assert storage.consumed_amount_for_po("PO-1002") == 0.0
    # The allocation row survives -- it is a fact about the run, not a balance.
    assert len(storage.allocations_for_run(run_id)) == 1

    storage.set_run_status(run_id, "APPROVED", "restored")
    assert storage.consumed_amount_for_po("PO-1002") == 3000.0


def test_excluding_a_run_excludes_all_of_its_allocations(db):
    run_id = _save("PO-1002", 3000.0)
    _save("PO-1002", 1000.0)
    assert storage.consumed_amount_for_po("PO-1002", exclude_run_id=run_id) == 1000.0


# --------------------------------------------------------------------------
# migration: a database written before this table existed
# --------------------------------------------------------------------------

def _legacy_db(path):
    """A database in the pre-allocation shape: charges live in (po_number, total)."""
    storage.DB_PATH = path
    storage.init_db(reset_runs=True)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE run_allocations")
    for fn, st, po, total in [
        ("a.pdf", "APPROVED", "PO-1002", 3000.0),
        ("b.pdf", "APPROVED", "PO-1002", 2000.0),
        ("c.pdf", "NEEDS_REVIEW", "PO-1002", 2500.0),
        ("d.pdf", "REJECTED", "PO-1001", 1240.0),
        ("e.pdf", "NEEDS_REVIEW", None, 999.0),
    ]:
        conn.execute("INSERT INTO runs (filename, status, created_at, total, po_number) "
                     "VALUES (?,?,?,?,?)", (fn, st, "2026-08-01T00:00:00Z", total, po))
    conn.commit()
    conn.close()


def test_migration_backfills_legacy_runs_without_moving_a_balance(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(storage, "DB_PATH", path)
    _legacy_db(path)

    storage.init_db()

    # Exactly what the old query would have said.
    assert storage.consumed_amount_for_po("PO-1002") == 5000.0
    assert storage.consumed_amount_for_po("PO-1001") == 0.0     # the run was REJECTED
    # One row per legacy run that named a PO; the PO-less run gets none.
    assert len(_rows(path)) == 4


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """It runs on every startup. A second pass must not top up a run's charge."""
    path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(storage, "DB_PATH", path)
    _legacy_db(path)

    storage.init_db()
    once = _rows(path)
    storage.init_db()
    storage.init_db()

    assert _rows(path) == once
    assert storage.consumed_amount_for_po("PO-1002") == 5000.0


def test_migration_never_tops_up_a_run_that_already_has_allocations(db):
    """A genuine multi-PO run must survive a restart untouched -- its allocations
    do not sum to `runs.total` for any single PO, so a naive backfill would
    double-charge it."""
    run_id = _save("PO-1002", 5000.0, allocations=[
        {"po_number": "PO-1002", "amount": 3000.0},
        {"po_number": "PO-1003", "amount": 2000.0},
    ])
    storage.init_db()
    storage.init_db()

    allocs = storage.allocations_for_run(run_id)
    assert len(allocs) == 2
    assert storage.consumed_amount_for_po("PO-1002") == 3000.0
    assert storage.consumed_amount_for_po("PO-1003") == 2000.0


# --------------------------------------------------------------------------
# housekeeping
# --------------------------------------------------------------------------

def test_clearing_run_history_clears_allocations(db):
    _save("PO-1002", 3000.0)
    _save("PO-1003", 1000.0)
    assert _rows(db)

    storage.clear_run_history()

    assert _rows(db) == []
    assert storage.consumed_amount_for_po("PO-1002") == 0.0
    assert storage.remaining_for_po("PO-1002") == 5000.0


def test_rewriting_a_runs_allocations_replaces_rather_than_appends(db):
    """Belt and braces: the writer deletes before inserting, so a repeated write
    cannot double-charge a PO."""
    run_id = _save("PO-1002", 3000.0)
    with storage.write_txn() as conn:
        storage._write_allocations(conn, run_id, [{"po_number": "PO-1002", "amount": 3000.0}])

    assert len(storage.allocations_for_run(run_id)) == 1
    assert storage.consumed_amount_for_po("PO-1002") == 3000.0
