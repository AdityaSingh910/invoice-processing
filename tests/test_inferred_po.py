"""Inferred PO matching: the distance cap, the ambiguity guard, and the verdict.

THE GAP THIS CLOSES

When an invoice carries no PO reference, matching falls back to inferring one
from vendor + amount. That fallback had three defects at once:

  1. No distance cap -- `min(pos, key=nearest)` always returned something, so a
     $9,000 invoice bound to a $200 PO if that was the vendor's only one.
  2. No ambiguity handling -- two equally plausible POs, and it silently took
     whichever the list happened to yield first.
  3. A decorative severity -- `decide()` added a "warn" reason that set neither
     `reject` nor `review`, so the verdict was unaffected.

Together: an invoice that never named a PO could be auto-approved against a PO
the process guessed. These tests pin each guard independently, so a regression in
one is not masked by another.

The rule now: an explicit reference is authoritative; an inferred one is a
suggestion for a human to confirm.
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

import matching    # noqa: E402
import rules       # noqa: E402
import storage     # noqa: E402
import pg_schema   # noqa: E402

VENDOR = "Acme Office Supplies"     # V-001, approved in the seed data


@pytest.fixture
def db(monkeypatch):
    """Isolated ledger. Seed POs load normally; tests add their own as needed."""
    schema = pg_schema.fresh_schema(monkeypatch)
    yield schema
    pg_schema.drop_schema(schema)


def add_po(po_number, amount, vendor=VENDOR, status="open"):
    conn = storage.get_conn()
    conn.execute("""INSERT INTO purchase_orders
           (po_number, vendor, amount, currency, issued_date, status, description)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                 (po_number, vendor, amount, "USD", "2026-01-01", status, "test"))
    conn.commit()
    conn.close()


def clear_pos():
    """Drop the seed POs so a test controls exactly what is on file."""
    conn = storage.get_conn()
    conn.execute("DELETE FROM purchase_orders")
    conn.commit()
    conn.close()


def invoice(total, refs=None, number="INV-T1"):
    return {"vendor_name": VENDOR, "invoice_number": number, "total": total,
            "po_references": refs or [], "currency": "USD"}


def verdict(po_match, missing=None):
    """Run decide() with everything except the PO check passing."""
    status, reasons = rules.decide(
        {"route": "regex", "notes": [], "security_flags": []},
        missing or [], True, "Vendor approved.", None, "No duplicate.", po_match)
    return status, reasons


# --------------------------------------------------------------------------
# A. explicit reference is unchanged and authoritative
# --------------------------------------------------------------------------

def test_explicit_reference_still_matches(db):
    clear_pos()
    add_po("PO-2001", 1_000.00)
    m = matching.match_po(invoice(1_000.00, refs=["PO-2001"]))
    assert m["matched_via"] == "explicit"
    assert m["po_number"] == "PO-2001"
    assert verdict(m)[0] == "APPROVED"


def test_explicit_reference_wins_even_when_the_amount_is_nowhere_near(db):
    """The invoice named the PO. A mismatched amount is a tolerance question,
    not a reason to go looking for a better-fitting PO."""
    clear_pos()
    add_po("PO-2001", 1_000.00)
    add_po("PO-2002", 9_000.00)
    m = matching.match_po(invoice(9_000.00, refs=["PO-2001"]))
    assert m["matched_via"] == "explicit"
    assert m["po_number"] == "PO-2001", "an explicit reference must never be second-guessed"


# --------------------------------------------------------------------------
# B. close + unambiguous -> binds, but still hands to a human
# --------------------------------------------------------------------------

def test_close_unambiguous_inference_binds_but_requires_review(db):
    """The safe behaviour: suggest the PO, show the balance, do not auto-approve.

    Everything else about this invoice is clean -- approved vendor, no duplicate,
    amount within tolerance. The ONLY thing holding it is that the invoice never
    named the PO, which is exactly what must now bite.
    """
    clear_pos()
    add_po("PO-2001", 1_000.00)
    m = matching.match_po(invoice(1_000.00))
    assert m["matched_via"] == "inferred"
    assert m["po_number"] == "PO-2001"
    assert m["within_tolerance"] is True

    status, reasons = verdict(m)
    assert status == "NEEDS_REVIEW", "an inferred PO must not auto-approve"
    assert any("inferred" in r["text"] and r["level"] == "warn" for r in reasons)


def test_the_inferred_warning_is_what_drives_the_verdict(db):
    """Proves the warn is load-bearing rather than decorative.

    Same invoice, same amount, same PO -- the only difference is whether the
    reference was on the document. Explicit approves; inferred does not.
    """
    clear_pos()
    add_po("PO-2001", 1_000.00)
    explicit = matching.match_po(invoice(1_000.00, refs=["PO-2001"]))
    inferred = matching.match_po(invoice(1_000.00))
    assert verdict(explicit)[0] == "APPROVED"
    assert verdict(inferred)[0] == "NEEDS_REVIEW"


# --------------------------------------------------------------------------
# C. distant candidate -> no binding at all
# --------------------------------------------------------------------------

def test_distant_candidate_is_not_inferred(db):
    """A $9,000 invoice must not bind to the vendor's only PO of $200.

    This is the headline defect: `min()` always returned something, so "nearest"
    meant "nearest of the wrong answers".
    """
    clear_pos()
    add_po("PO-2001", 200.00)
    m = matching.match_po(invoice(9_000.00))
    assert m["po_number"] is None, "a distant PO must not be inferred"
    assert m["matched_via"] == "none"
    assert m["inference"] == "no_close_candidate"

    status, reasons = verdict(m)
    assert status == "NEEDS_REVIEW"
    assert any("close enough in amount" in r["text"] for r in reasons)


def test_the_distance_cap_uses_the_configured_tolerance(db):
    """Just inside binds, just outside does not -- the threshold actually bites."""
    clear_pos()
    add_po("PO-2001", 10_000.00)
    tol = matching.tolerance_for(10_000.00)

    inside = matching.match_po(invoice(10_000.00 + tol - 1.00))
    assert inside["matched_via"] == "inferred"

    outside = matching.match_po(invoice(10_000.00 + tol + 1.00))
    assert outside["po_number"] is None
    assert outside["inference"] == "no_close_candidate"


# --------------------------------------------------------------------------
# D. ambiguous candidates -> no binding, and say so
# --------------------------------------------------------------------------

def test_ambiguous_candidates_are_not_inferred(db):
    """Two POs equally plausible: choosing either is a guess, so choose neither."""
    clear_pos()
    add_po("PO-2001", 1_000.00)
    add_po("PO-2002", 1_000.00)
    m = matching.match_po(invoice(1_000.00))
    assert m["po_number"] is None
    assert m["inference"] == "ambiguous"

    status, reasons = verdict(m)
    assert status == "NEEDS_REVIEW"
    assert any("more than one purchase order" in r["text"] for r in reasons)


def test_near_miss_ambiguity_is_still_ambiguous(db):
    """Both within tolerance but not identical -- still a guess, still refused."""
    clear_pos()
    add_po("PO-2001", 10_000.00)
    add_po("PO-2002", 10_040.00)
    m = matching.match_po(invoice(10_020.00))
    assert m["inference"] == "ambiguous"
    assert m["po_number"] is None


def test_one_close_and_one_distant_is_not_ambiguous(db):
    """The cap runs first, so a far-off PO cannot manufacture ambiguity."""
    clear_pos()
    add_po("PO-2001", 1_000.00)
    add_po("PO-2002", 90_000.00)
    m = matching.match_po(invoice(1_000.00))
    assert m["matched_via"] == "inferred"
    assert m["po_number"] == "PO-2001"


def test_vendor_with_no_purchase_orders_reports_plainly(db):
    """Nothing to infer from is different from 'nothing close enough'."""
    clear_pos()
    add_po("PO-3001", 500.00, vendor="Globex Logistics")
    m = matching.match_po(invoice(500.00))
    assert m["po_number"] is None
    assert m["inference"] is None, "no candidates at all is not a failed inference"
    assert verdict(m)[0] == "NEEDS_REVIEW"


# --------------------------------------------------------------------------
# E/F. nothing else moved
# --------------------------------------------------------------------------

def test_split_po_behaviour_is_unchanged(db):
    """Explicit split-PO invoices still approve and still consume balance."""
    clear_pos()
    add_po("PO-2002", 5_000.00)

    def submit(total, number):
        ex = invoice(total, refs=["PO-2002"], number=number)
        m = matching.match_po(ex)
        status, reasons = verdict(m)
        _, final, _ = storage.save_run_checked(f"{number}.pdf", status, ex, m, [], reasons,
                                               tolerance_for=matching.tolerance_for)
        return final

    assert submit(3_000.00, "INV-S1") == "APPROVED"
    assert storage.remaining_for_po("PO-2002") == 2_000.00
    assert submit(2_000.00, "INV-S2") == "APPROVED"
    assert storage.remaining_for_po("PO-2002") == 0.00
    assert submit(2_500.00, "INV-S3") == "NEEDS_REVIEW"


def test_duplicate_rejection_is_unchanged(db):
    """A duplicate still REJECTs, and an inferred match must not soften that."""
    clear_pos()
    add_po("PO-2001", 1_000.00)
    m = matching.match_po(invoice(1_000.00))          # inferred -> would be review
    dup = {"id": 1, "created_at": "2026-01-01T00:00:00", "status": "APPROVED"}
    status, _ = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                             [], True, "Vendor approved.", dup, "Duplicate of run #1.", m)
    assert status == "REJECTED", "reject must still outrank an inferred-match review"


def test_unapproved_vendor_still_rejects_under_inference(db):
    clear_pos()
    add_po("PO-2001", 1_000.00)
    m = matching.match_po(invoice(1_000.00))
    status, _ = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                             [], False, "Vendor not approved.", None, "No duplicate.", m)
    assert status == "REJECTED"
