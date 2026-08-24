"""PO balance edge cases: split invoices, idempotency, reversal, cascade, tolerance.

A NOTE ON THE ARCHITECTURE THESE TESTS ASSERT AGAINST

There is no `po.remaining_amount` column, and deliberately so. Consumption is
DERIVED on every read:

    remaining = po.amount - SUM(total of runs WHERE po_number=? AND status='APPROVED')

That choice is what makes most of this file cheap to satisfy:

* **Idempotency is structural.** Nothing is ever deducted, so nothing can be
  deducted twice. Re-evaluating an approved invoice recomputes the same sum.
  A stored counter would need a guard flag and would be one missed code path
  away from double-spending a PO.
* **Reversal is structural.** Moving a run out of APPROVED drops it from the
  SUM in the same instant. There is no refund step to forget.

What is NOT free, and is implemented rather than assumed:

* **Atomicity.** Deriving the balance does not serialise anything. Two invoices
  can still read the same balance concurrently and both approve. Fixed by
  re-checking under `BEGIN IMMEDIATE` at commit time (`save_run_checked`).
* **Cascade re-evaluation.** Freed budget does not re-open held invoices by
  itself; `rules.reevaluate_po_queue` does that.

These tests drive the rules and storage layers directly rather than through PDFs
-- the scenarios are about the ledger, and inventing $1,000,000 fixture invoices
would test reportlab, not the balance logic.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import config      # noqa: E402
import matching    # noqa: E402
import rules       # noqa: E402
import storage     # noqa: E402
import pg_schema   # noqa: E402

BIG_PO = "PO-9001"
VENDOR = "Acme Office Supplies"     # already on the approved seed list


@pytest.fixture
def db(monkeypatch):
    """An isolated ledger with one $1,000,000 PO on top of the normal seed data."""
    schema = pg_schema.fresh_schema(monkeypatch)
    conn = storage.get_conn()
    # Columns named rather than positional: a bare VALUES(...) breaks the moment
    # the schema grows, which is exactly what happened when PO provenance was
    # added. A fixture should not be the reason a schema change looks like a bug.
    conn.execute(
        """INSERT INTO purchase_orders
           (po_number, vendor, amount, currency, issued_date, status, description)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (BIG_PO, VENDOR, 1_000_000.0, "USD", "2026-01-01", "open", "Large works order"))
    conn.commit()
    conn.close()
    yield schema
    pg_schema.drop_schema(schema)


def submit(total, invoice_number, po=BIG_PO, filename=None):
    """Run one invoice through match -> decide -> atomic commit.

    Mirrors what the pipeline does after extraction, which is the part these
    scenarios are about.
    """
    extracted = {
        "vendor_name": VENDOR,
        "invoice_number": invoice_number,
        "total": total,
        "po_references": [po],
        "currency": "USD",
    }
    po_match = matching.match_po(extracted)
    info = {"route": "regex", "notes": [], "security_flags": []}
    missing = rules.validate_required_fields(extracted)
    vendor_ok, _, vendor_detail = rules.vendor_check(extracted)
    dup_row, dup_detail = rules.duplicate_check(extracted)
    status, reasons = rules.decide(info, missing, vendor_ok, vendor_detail,
                                   dup_row, dup_detail, po_match)
    run_id, final_status, _ = storage.save_run_checked(
        filename or f"{invoice_number}.pdf", status, extracted, po_match, [], reasons,
        tolerance_for=matching.tolerance_for)
    return run_id, final_status


def remaining(po=BIG_PO):
    return storage.remaining_for_po(po)


# --------------------------------------------------------------------------
# 1. split PO execution
# --------------------------------------------------------------------------

def test_split_po_three_invoices(db):
    """$1M PO, invoices of $400k / $400k / $300k.

    The first two fit and approve. The third does not: $300k against $200k
    remaining is $100k over, far outside the tolerance allowance.
    """
    assert remaining() == 1_000_000.00

    _, s1 = submit(400_000.00, "INV-A")
    assert s1 == "APPROVED"
    assert remaining() == 600_000.00

    _, s2 = submit(400_000.00, "INV-B")
    assert s2 == "APPROVED"
    assert remaining() == 200_000.00

    _, s3 = submit(300_000.00, "INV-C")
    assert s3 == "NEEDS_REVIEW", "an invoice $100k over the balance must not auto-approve"

    # The held invoice must not consume budget -- that is what lets the queue
    # keep moving behind it.
    assert remaining() == 200_000.00


# --------------------------------------------------------------------------
# 2. idempotency
# --------------------------------------------------------------------------

def test_reevaluating_an_approved_invoice_never_double_deducts(db):
    """Re-run the rules five times over an approved invoice; balance must not move.

    With a derived balance this is structural rather than defended: there is no
    deduction to repeat. The test exists to keep it that way -- if anyone later
    introduces a stored counter, this fails immediately.
    """
    submit(400_000.00, "INV-A")
    assert remaining() == 600_000.00

    for i in range(5):
        run = storage.list_runs()[0]
        po_match = matching.match_po(run["extracted"], exclude_run_id=run["id"])
        rules.decide({"route": "regex", "notes": [], "security_flags": []}, [],
                     True, "Vendor approved.", None, "No dup.", po_match)
        rules.reevaluate_po_queue(BIG_PO)
        assert remaining() == 600_000.00, f"balance drifted on re-evaluation {i + 1}"


def test_setting_the_same_status_twice_is_a_no_op(db):
    run_id, _ = submit(400_000.00, "INV-A")
    assert remaining() == 600_000.00
    for _ in range(3):
        storage.set_run_status(run_id, "APPROVED")
        assert remaining() == 600_000.00


# --------------------------------------------------------------------------
# 3. reversal and cascade
# --------------------------------------------------------------------------

def test_reversal_refunds_and_cascades(db):
    """Rejecting the first $400k invoice frees budget, which releases the held one.

    Inv1 $400k APPROVED, Inv2 $400k APPROVED, Inv3 $300k held at $200k remaining.
    Reject Inv1 -> $600k available -> Inv3 fits and is auto-approved.
    """
    id1, s1 = submit(400_000.00, "INV-A")
    id2, s2 = submit(400_000.00, "INV-B")
    id3, s3 = submit(300_000.00, "INV-C")
    assert (s1, s2, s3) == ("APPROVED", "APPROVED", "NEEDS_REVIEW")
    assert remaining() == 200_000.00

    ok, old, po = storage.set_run_status(id1, "REJECTED", "Reversed: goods returned.")
    assert (ok, old, po) == (True, "APPROVED", BIG_PO)

    # The refund is immediate and needs no explicit step: id1 has left the SUM.
    assert remaining() == 600_000.00

    cascaded = rules.reevaluate_po_queue(BIG_PO, triggered_by=id1)
    assert [c["run_id"] for c in cascaded] == [id3]
    assert cascaded[0]["from"] == "NEEDS_REVIEW" and cascaded[0]["to"] == "APPROVED"

    assert storage.get_run(id3)["status"] == "APPROVED"
    assert remaining() == 300_000.00          # 1M - 400k(id2) - 300k(id3)
    assert storage.get_run(id1)["status"] == "REJECTED"


def test_cascade_leaves_invoices_held_for_other_reasons_alone(db):
    """Freeing budget says nothing about a missing invoice number.

    Without this, a reversal becomes a way to launder a substantively-blocked
    invoice into APPROVED.
    """
    id1, _ = submit(400_000.00, "INV-A")

    # Held because it has no invoice number, not because of the balance.
    extracted = {"vendor_name": VENDOR, "invoice_number": None,
                 "total": 1_000.00, "po_references": [BIG_PO], "currency": "USD"}
    po_match = matching.match_po(extracted)
    status, reasons = rules.decide(
        {"route": "regex", "notes": [], "security_flags": []},
        rules.validate_required_fields(extracted),
        True, "Vendor approved.", None, "No dup.", po_match)
    assert status == "NEEDS_REVIEW"
    held_id, _, _ = storage.save_run_checked("no-number.pdf", status, extracted, po_match,
                                             [], reasons, tolerance_for=matching.tolerance_for)

    storage.set_run_status(id1, "REJECTED", "Reversed.")
    cascaded = rules.reevaluate_po_queue(BIG_PO, triggered_by=id1)

    assert held_id not in [c["run_id"] for c in cascaded]
    assert storage.get_run(held_id)["status"] == "NEEDS_REVIEW"


def test_cascade_respects_submission_order(db):
    """When freed budget covers only one of two held invoices, the earlier wins."""
    id1, _ = submit(500_000.00, "INV-A")
    id2, _ = submit(500_000.00, "INV-B")
    assert remaining() == 0.00

    id3, s3 = submit(300_000.00, "INV-C")
    id4, s4 = submit(300_000.00, "INV-D")
    assert (s3, s4) == ("NEEDS_REVIEW", "NEEDS_REVIEW")

    storage.set_run_status(id1, "REJECTED", "Reversed.")   # frees 500k
    cascaded = rules.reevaluate_po_queue(BIG_PO, triggered_by=id1)

    # 500k free: INV-C takes 300k, leaving 200k -- not enough for INV-D.
    assert [c["run_id"] for c in cascaded] == [id3]
    assert storage.get_run(id3)["status"] == "APPROVED"
    assert storage.get_run(id4)["status"] == "NEEDS_REVIEW"
    assert remaining() == 200_000.00


# --------------------------------------------------------------------------
# 4. tolerance
# --------------------------------------------------------------------------

def test_over_budget_within_tolerance_auto_approves(db):
    """$200,010 against $200,000 remaining is $10 over -- inside the allowance.

    Tax and freight get added after a PO is raised, so a small overage is normal
    and blocking it would bury AP in noise.
    """
    submit(800_000.00, "INV-A")
    assert remaining() == 200_000.00

    _, status = submit(200_010.00, "INV-B")
    assert status == "APPROVED"
    assert remaining() == -10.00, "the overage is real and must show in the ledger"


def test_tolerance_approval_is_never_silent(db):
    """An invoice approved for more than the PO authorised must say so."""
    submit(800_000.00, "INV-A")
    extracted = {"vendor_name": VENDOR, "invoice_number": "INV-B",
                 "total": 200_010.00, "po_references": [BIG_PO], "currency": "USD"}
    po_match = matching.match_po(extracted)
    assert po_match["over_within_tolerance"] is True

    status, reasons = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                                   [], True, "Vendor approved.", None, "No dup.", po_match)
    assert status == "APPROVED"
    assert any("tolerance" in r["text"] and r["level"] == "warn" for r in reasons), \
        "approving over the PO balance must leave an audit note"


def test_beyond_tolerance_is_held(db):
    """Just past the allowance flips to review -- the threshold actually bites."""
    submit(800_000.00, "INV-A")
    tol = matching.tolerance_for(200_000.00)
    _, status = submit(200_000.00 + tol + 1.00, "INV-B")
    assert status == "NEEDS_REVIEW"


def test_tolerance_reads_from_config(db):
    """Policy lives in config, not in the code."""
    assert matching.tolerance_for(200_000.00) == max(
        200_000.00 * config.PO_TOLERANCE_PERCENT, config.PO_TOLERANCE_DOLLARS)
    # Small POs get the dollar floor, large ones the percentage.
    assert matching.tolerance_for(100.00) == config.PO_TOLERANCE_DOLLARS
    assert matching.tolerance_for(1_000_000.00) == 1_000_000.00 * config.PO_TOLERANCE_PERCENT


# --------------------------------------------------------------------------
# 5. atomicity -- the documented race
# --------------------------------------------------------------------------

def test_stale_approval_is_rejected_at_commit(db):
    """Two invoices decided against the same balance must not both approve.

    Simulates the race directly: compute a verdict for the second invoice while
    the balance still looks free, let the first one commit, then commit the
    second. The re-check under BEGIN IMMEDIATE must catch it -- and, since B
    was correctly APPROVED against the balance it saw and lost only because A
    consumed it first, the loser is REJECTED outright rather than held: the
    PO genuinely no longer has the money, so there is nothing left for a
    reviewer to decide.
    """
    extracted_b = {"vendor_name": VENDOR, "invoice_number": "INV-B",
                   "total": 700_000.00, "po_references": [BIG_PO], "currency": "USD"}

    # B is decided while the full $1M still appears available.
    po_match_b = matching.match_po(extracted_b)
    assert po_match_b["remaining_before"] == 1_000_000.00
    status_b, reasons_b = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                                       [], True, "Vendor approved.", None, "No dup.", po_match_b)
    assert status_b == "APPROVED"

    # A commits first and takes $600k.
    _, status_a = submit(600_000.00, "INV-A")
    assert status_a == "APPROVED"

    # Now B commits against a stale view. $700k no longer fits in $400k.
    _, final_b, extra = storage.save_run_checked(
        "INV-B.pdf", status_b, extracted_b, po_match_b, [], reasons_b,
        tolerance_for=matching.tolerance_for)

    assert final_b == "REJECTED", "stale approval overspent the PO"
    assert extra and "Balance changed" in extra["text"]
    assert "duplicate" not in extra["text"].lower(), \
        "this must be a balance rejection, not a duplicate finding"
    assert remaining() == 400_000.00, "the PO must not be overspent"


def test_concurrent_invoices_cannot_overspend_a_po(db):
    """The real race, under real threads -- not a simulation.

    Eight $2,000 invoices are decided against an untouched $10,000 PO (so every
    one of them believes it can approve), then all commit simultaneously. At most
    five can be funded. Before the transaction boundary existed, all eight
    approved and the PO was overspent by $6,000.

    Under Postgres, `save_run_checked` takes `SELECT ... FOR UPDATE` on the
    PO-RACE row, so a thread that loses the race simply BLOCKS until the
    winner commits or rolls back, then proceeds against the now-current
    balance -- it does not raise. The retry loop below is inherited from the
    SQLite version, where a busy connection could raise "database is locked"
    rather than queue past its timeout; it is kept because it is still
    correct (a loop that never needs to retry is a harmless no-op) and because
    a self-hosted Postgres under real contention CAN still raise on rare
    occasions (a deadlock across multiple locked rows, a statement timeout) --
    this asserts the OUTCOME (never over $10,000, exactly five approved), not
    the mechanism by which each thread got there.

    A thread that loses is REJECTED, not held. It was correctly APPROVED
    against the balance it saw and lost only because five other invoices
    against the same PO committed first, under the same lock -- the PO does
    not have $2,000 left for it, full stop, so there is nothing left for a
    human to adjudicate.
    """
    import threading

    conn = storage.get_conn()
    conn.execute("""INSERT INTO purchase_orders
           (po_number, vendor, amount, currency, issued_date, status, description)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                 ("PO-RACE", VENDOR, 10_000.0, "USD", "2026-01-01", "open", "race"))
    conn.commit()
    conn.close()

    n = 8
    results, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker(i):
        extracted = {"vendor_name": VENDOR, "invoice_number": f"R-{i}", "total": 2_000.0,
                     "po_references": ["PO-RACE"], "currency": "USD"}
        po_match = matching.match_po(extracted)          # every thread reads first...
        status, reasons = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                                       [], True, "ok", None, "no dup", po_match)
        barrier.wait()                                   # ...then all commit together
        for attempt in range(20):
            try:
                _, final, _ = storage.save_run_checked(
                    f"r{i}.pdf", status, extracted, po_match, [], reasons,
                    tolerance_for=matching.tolerance_for)
                break
            except Exception as exc:
                if "locked" not in str(exc).lower() or attempt == 19:
                    raise
        with lock:
            results.append(final)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    consumed = storage.consumed_amount_for_po("PO-RACE")
    assert consumed <= 10_000.0, f"PO overspent: ${consumed:,.2f} committed against $10,000"
    assert results.count("APPROVED") == 5
    assert results.count("REJECTED") == 3
    assert results.count("NEEDS_REVIEW") == 0
    assert storage.remaining_for_po("PO-RACE") == 0.00


def test_concurrency_demo_two_invoices_one_rejected(db):
    """The exact scenario the concurrency demo fixtures (`PO-7000-CONC`,
    `sample_invoices/12_concurrency_race_keyboard_a.pdf` / `_b.pdf`, see
    CLAUDE.md sec7o) are built to show in a browser: two INDIVIDUALLY VALID
    $4,000 invoices racing a single $7,000 PO, submitted at the same instant
    with real threads, not a simulated ordering.

    Exactly one must be APPROVED and exactly one REJECTED -- never both
    APPROVED (which would overspend the PO by $1,000), never both held, and
    never NEEDS_REVIEW at all: the loser was correctly approved against the
    balance it saw and only lost because the other invoice consumed the PO
    first, which this ledger can now express directly as a rejection rather
    than a hold. Either invoice may win; this test does not, and must not,
    force an ordering.
    """
    import threading

    conn = storage.get_conn()
    conn.execute("""INSERT INTO purchase_orders
           (po_number, vendor, amount, currency, issued_date, status, description)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                 ("PO-7000-DEMO", VENDOR, 7_000.0, "USD", "2026-01-01", "open", "concurrency demo"))
    conn.commit()
    conn.close()

    results, lock, barrier = [], threading.Lock(), threading.Barrier(2)

    def worker(label):
        extracted = {"vendor_name": VENDOR, "invoice_number": f"INV-CONC-4000-{label}",
                     "total": 4_000.0, "po_references": ["PO-7000-DEMO"], "currency": "USD"}
        po_match = matching.match_po(extracted)          # both read the same $7,000...
        status, reasons = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                                       [], True, "ok", None, "no dup", po_match)
        assert status == "APPROVED", "each $4,000 invoice is individually valid on its own"
        barrier.wait()                                   # ...then both commit together
        for attempt in range(20):
            try:
                _, final, extra = storage.save_run_checked(
                    f"conc-{label}.pdf", status, extracted, po_match, [], reasons,
                    tolerance_for=matching.tolerance_for)
                break
            except Exception as exc:
                if "locked" not in str(exc).lower() or attempt == 19:
                    raise
        with lock:
            results.append((label, final, extra))

    threads = [threading.Thread(target=worker, args=(label,)) for label in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    outcomes = {label: final for label, final, _ in results}
    assert sorted(outcomes.values()) == ["APPROVED", "REJECTED"], (
        f"expected exactly one APPROVED and one REJECTED, got {outcomes}"
    )

    loser_label = next(label for label, final, _ in results if final == "REJECTED")
    loser_extra = next(extra for label, final, extra in results if final == "REJECTED")
    assert loser_extra is not None
    assert "duplicate" not in loser_extra["text"].lower(), \
        "the loser must be rejected for the PO balance, never for looking like a duplicate"
    assert "balance" in loser_extra["text"].lower() or "exceeds" in loser_extra["text"].lower()

    assert storage.consumed_amount_for_po("PO-7000-DEMO") == 4_000.0
    assert storage.remaining_for_po("PO-7000-DEMO") == 3_000.0
