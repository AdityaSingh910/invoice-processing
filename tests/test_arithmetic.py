"""Invoice arithmetic consistency: subtotal + tax must equal the stated total.

THE GAP THIS CLOSES

Extraction produced subtotal, tax and total; nothing ever checked they agreed.
An invoice reading

    Subtotal: $4,000
    Tax:        $800
    Total:    $6,000

was processed as an ordinary $6,000 invoice. Every downstream check -- vendor,
PO balance, tolerance, duplicate -- operates on `total`, so a total that
contradicts its own components is a figure the whole decision rests on and
nobody had verified.

WHAT THIS IS NOT

It does not recompute the invoice or "correct" the total. It reports that the
document disagrees with itself and hands it to a person. Deciding which figure is
right is exactly the judgement a human is for.
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

VENDOR = "Initech Consulting"     # V-003, approved in the seed data


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    conn = storage.get_conn()
    conn.execute("DELETE FROM purchase_orders")
    conn.execute("""INSERT INTO purchase_orders
           (po_number, vendor, amount, currency, issued_date, status, description)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                 ("PO-5001", VENDOR, 10_000.00, "USD", "2026-01-01", "open", "test"))
    conn.commit()
    conn.close()
    yield schema
    pg_schema.drop_schema(schema)


def invoice(subtotal, tax, total, number="INV-A1"):
    return {"vendor_name": VENDOR, "invoice_number": number, "po_references": ["PO-5001"],
            "currency": "USD", "subtotal": subtotal, "tax": tax, "total": total}


def verdict(extracted, dup_row=None, dup_detail="No duplicate."):
    """Full decision path, with arithmetic wired in exactly as the pipeline does."""
    po_match = matching.match_po(extracted)
    status, reasons = rules.decide(
        {"route": "regex", "notes": [], "security_flags": []},
        rules.validate_required_fields(extracted),
        True, "Vendor approved.", dup_row, dup_detail, po_match,
        arithmetic=rules.validate_arithmetic(extracted))
    return status, reasons


# --------------------------------------------------------------------------
# A. valid arithmetic -> unchanged
# --------------------------------------------------------------------------

def test_consistent_invoice_still_approves(db):
    ex = invoice(4_000.00, 800.00, 4_800.00)
    assert rules.validate_arithmetic(ex) is None
    assert verdict(ex)[0] == "APPROVED"


def test_zero_tax_is_checked_not_skipped(db):
    """A tax of 0.00 is a present value, not a missing one.

    Testing truthiness instead of `is None` would skip every zero-rated invoice
    -- the population most worth checking, since a wrong total hides best where
    there is no tax line to notice.
    """
    assert rules.validate_arithmetic(invoice(4_000.00, 0.00, 4_000.00)) is None
    bad = rules.validate_arithmetic(invoice(4_000.00, 0.00, 5_000.00))
    assert bad is not None, "zero-tax invoices must still be checked"
    assert bad["diff"] == 1_000.00


# --------------------------------------------------------------------------
# B. rounding slack
# --------------------------------------------------------------------------

@pytest.mark.parametrize("total", [4_800.01, 4_799.99, 4_800.05, 4_799.95])
def test_normal_rounding_does_not_trigger_review(db, total):
    """Cent-level drift is how real invoices round per-line tax."""
    ex = invoice(4_000.00, 800.00, total)
    assert rules.validate_arithmetic(ex) is None, f"{total} should be within rounding"
    assert verdict(ex)[0] == "APPROVED"


def test_the_rounding_boundary_bites(db):
    """One cent past the allowance flips it, so the threshold is real."""
    tol = config.ARITHMETIC_TOLERANCE_DOLLARS
    assert rules.validate_arithmetic(invoice(4_000.00, 800.00, 4_800.00 + tol)) is None
    assert rules.validate_arithmetic(invoice(4_000.00, 800.00, 4_800.00 + tol + 0.01)) is not None


def test_float_representation_does_not_create_a_false_failure(db):
    """0.1 + 0.2 != 0.3 in binary floating point. Rounding the comparison, not
    just the inputs, is what keeps that out of an AP clerk's queue."""
    assert rules.validate_arithmetic(invoice(0.10, 0.20, 0.30)) is None
    assert rules.validate_arithmetic(invoice(1_234.56, 98.76, 1_333.32)) is None


# --------------------------------------------------------------------------
# C. clear mismatch -> NEEDS_REVIEW with a clear finding
# --------------------------------------------------------------------------

def test_clear_mismatch_forces_review(db):
    """The brief's example: 4,000 + 800 stated as 6,000."""
    ex = invoice(4_000.00, 800.00, 6_000.00)
    bad = rules.validate_arithmetic(ex)
    assert bad is not None
    assert bad["expected"] == 4_800.00 and bad["diff"] == 1_200.00

    status, reasons = verdict(ex)
    assert status == "NEEDS_REVIEW"
    hits = [r for r in reasons if r["text"].startswith("Invoice arithmetic mismatch:")]
    assert len(hits) == 1
    assert hits[0]["level"] == "fail"
    for fragment in ["subtotal + tax does not equal total", "$4000.00", "$800.00",
                     "$4800.00", "$6000.00", "$1200.00"]:
        assert fragment in hits[0]["text"], f"finding should state {fragment}"


def test_understated_total_is_also_caught(db):
    """A total BELOW its components is equally inconsistent. Only bounding the
    over side would miss an invoice whose subtotal was misread upward."""
    bad = rules.validate_arithmetic(invoice(4_000.00, 800.00, 3_000.00))
    assert bad is not None and bad["diff"] == -1_800.00
    assert verdict(invoice(4_000.00, 800.00, 3_000.00))[0] == "NEEDS_REVIEW"


def test_mismatch_is_caught_even_when_everything_else_is_perfect(db):
    """Approved vendor, explicit PO, comfortably within balance, no duplicate --
    the arithmetic is the only objection, and it must be enough."""
    ex = invoice(4_000.00, 800.00, 6_000.00)
    po_match = matching.match_po(ex)
    assert po_match["within_tolerance"] is True
    status, reasons = verdict(ex)
    assert status == "NEEDS_REVIEW"
    assert len([r for r in reasons if r["level"] == "fail"]) == 1


def test_a_mismatched_invoice_does_not_consume_po_budget(db):
    ex = invoice(4_000.00, 800.00, 6_000.00)
    po_match = matching.match_po(ex)
    status, reasons = verdict(ex)
    storage.save_run_checked("bad.pdf", status, ex, po_match, [], reasons,
                             tolerance_for=matching.tolerance_for)
    assert storage.remaining_for_po("PO-5001") == 10_000.00


# --------------------------------------------------------------------------
# D. not enough information -> preserve existing behaviour
# --------------------------------------------------------------------------

@pytest.mark.parametrize("subtotal,tax,total", [
    (None, 800.00, 4_800.00),      # no subtotal
    (4_000.00, None, 4_800.00),    # no tax  -- very common; must not fail
    (4_000.00, 800.00, None),      # no total
    (None, None, 4_800.00),
    (None, None, None),
])
def test_missing_components_do_not_fabricate_a_failure(db, subtotal, tax, total):
    """A missing tax line is evidence of a missing tax line, not bad arithmetic.

    Comparing subtotal to total when tax is absent would flag every invoice whose
    tax the extractor did not pick up -- the fastest possible way to make the
    check untrustworthy.
    """
    assert rules.validate_arithmetic(invoice(subtotal, tax, total)) is None


def test_non_numeric_values_are_not_an_arithmetic_failure(db):
    """Garbage in a numeric field is an extraction problem; the required-field
    check owns it. This guard must not raise, either."""
    assert rules.validate_arithmetic(
        {"subtotal": "n/a", "tax": 800.00, "total": 4_800.00}) is None


def test_invoice_with_only_a_total_is_unaffected(db):
    """The common shape for the existing sample fixtures."""
    ex = {"vendor_name": VENDOR, "invoice_number": "INV-T", "po_references": ["PO-5001"],
          "currency": "USD", "total": 4_800.00}
    assert rules.validate_arithmetic(ex) is None
    assert verdict(ex)[0] == "APPROVED"


# --------------------------------------------------------------------------
# E. decision hierarchy preserved
# --------------------------------------------------------------------------

def test_duplicate_outranks_arithmetic_mismatch(db):
    ex = invoice(4_000.00, 800.00, 6_000.00)
    dup = {"id": 1, "created_at": "2026-01-01T00:00:00", "status": "APPROVED"}
    status, reasons = verdict(ex, dup_row=dup, dup_detail="Duplicate of run #1.")
    assert status == "REJECTED"
    assert any(r["text"].startswith("Invoice arithmetic mismatch:") for r in reasons), \
        "both findings should still be reported"


def test_unapproved_vendor_outranks_arithmetic_mismatch(db):
    ex = invoice(4_000.00, 800.00, 6_000.00)
    po_match = matching.match_po(ex)
    status, _ = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                             [], False, "Vendor not approved.", None, "No duplicate.",
                             po_match, arithmetic=rules.validate_arithmetic(ex))
    assert status == "REJECTED"


def test_decide_still_works_without_the_new_argument(db):
    """`arithmetic` is optional, so existing callers are unaffected."""
    ex = invoice(4_000.00, 800.00, 4_800.00)
    status, _ = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                             [], True, "Vendor approved.", None, "No duplicate.",
                             matching.match_po(ex))
    assert status == "APPROVED"
