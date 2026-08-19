"""Invoice totals must be positive.

THE GAP THIS CLOSES

Measured against the code before this change:

    total =  1000.00  ->  APPROVED       (correct)
    total =     0.00  ->  NEEDS_REVIEW   (right verdict, WRONG reason)
    total =  -500.00  ->  APPROVED       (the hole)
    total = -5000.00  ->  APPROVED

A negative total sailed straight through. Matching compares `total - remaining`,
so a negative total makes that comfortably negative -- which reads as a small
partial invoice against a healthy PO, the most innocuous shape there is. A
negative total is a credit note; approving it as a payable moves money the wrong
way.

Zero was caught, but by accident and with a misleading message: the required-field
check used `not extracted.get("total")`, and `not 0.0` is True, so a printed
$0.00 was reported as a *missing* total. Right verdict, wrong story -- and it
sends a clerk hunting for a figure that is on the page.

Presence is now tested with `_is_missing` (None or blank string) so a zero total
is present-but-invalid, and `validate_amount` owns it with its own message.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import matching    # noqa: E402
import rules       # noqa: E402
import storage     # noqa: E402

VENDOR = "Stark Industrial Parts"     # V-005, approved in the seed data


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "amount.db"))
    storage.init_db(reset_runs=True)
    conn = storage.get_conn()
    conn.execute("DELETE FROM purchase_orders")
    conn.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?)",
                 ("PO-7001", VENDOR, 50_000.00, "USD", "2026-01-01", "open", "test"))
    conn.commit()
    conn.close()
    return storage.DB_PATH


def invoice(total, number="INV-M1"):
    return {"vendor_name": VENDOR, "invoice_number": number, "total": total,
            "po_references": ["PO-7001"], "currency": "USD"}


def verdict(extracted, dup_row=None, dup_detail="No duplicate.", vendor_ok=True):
    """Full decision path, wired exactly as the pipeline wires it."""
    po_match = matching.match_po(extracted)
    return rules.decide(
        {"route": "regex", "notes": [], "security_flags": []},
        rules.validate_required_fields(extracted),
        vendor_ok, "Vendor approved.", dup_row, dup_detail, po_match,
        arithmetic=rules.validate_arithmetic(extracted),
        amount=rules.validate_amount(extracted))


# --------------------------------------------------------------------------
# A. normal positive amount -> unchanged
# --------------------------------------------------------------------------

def test_positive_total_still_approves(db):
    ex = invoice(1_000.00)
    assert rules.validate_amount(ex) is None
    assert verdict(ex)[0] == "APPROVED"


def test_small_positive_total_is_valid(db):
    """One cent is a real invoice. The rule is 'greater than zero', not 'large'."""
    assert rules.validate_amount(invoice(0.01)) is None
    assert verdict(invoice(0.01))[0] == "APPROVED"


# --------------------------------------------------------------------------
# B. zero -> NEEDS_REVIEW, and reported honestly
# --------------------------------------------------------------------------

def test_zero_total_requires_review(db):
    ex = invoice(0.00)
    bad = rules.validate_amount(ex)
    assert bad is not None and bad["kind"] == "zero"
    assert verdict(ex)[0] == "NEEDS_REVIEW"


def test_zero_total_is_not_reported_as_a_missing_field(db):
    """The figure is on the page. Calling it missing sends a clerk hunting.

    This is the regression guard for `_is_missing`: reverting it to `not value`
    makes this fail.
    """
    ex = invoice(0.00)
    assert rules.validate_required_fields(ex) == [], "0.00 is present, not missing"

    status, reasons = verdict(ex)
    assert status == "NEEDS_REVIEW"
    texts = [r["text"] for r in reasons]
    assert not any("Missing required field" in t for t in texts)
    assert sum(t.startswith("Invalid invoice amount:") for t in texts) == 1, \
        "exactly one finding should describe the amount"


def test_a_genuinely_absent_total_is_still_a_missing_field(db):
    """`_is_missing` must not have turned None into 'present'."""
    ex = invoice(None)
    assert rules.validate_required_fields(ex) == ["total"]
    assert rules.validate_amount(ex) is None, "absence is the required-field check's business"
    status, reasons = verdict(ex)
    assert status == "NEEDS_REVIEW"
    assert any("Missing required field" in r["text"] for r in reasons)


def test_blank_string_fields_are_still_missing(db):
    """`_is_missing` keeps string semantics: "" and "   " are absent."""
    ex = dict(invoice(100.00), vendor_name="", invoice_number="   ")
    assert set(rules.validate_required_fields(ex)) == {"vendor_name", "invoice_number"}


# --------------------------------------------------------------------------
# C. negative -> NEEDS_REVIEW
# --------------------------------------------------------------------------

@pytest.mark.parametrize("total", [-0.01, -500.00, -5_000.00, -1_000_000.00])
def test_negative_total_requires_review(db, total):
    ex = invoice(total)
    bad = rules.validate_amount(ex)
    assert bad is not None and bad["kind"] == "negative"
    assert verdict(ex)[0] == "NEEDS_REVIEW", f"{total} must not auto-approve"


def test_negative_total_is_caught_even_though_every_other_check_passes(db):
    """The exact shape of the old hole.

    A negative total makes `total - remaining` comfortably negative, which the
    tolerance logic reads as a small partial invoice against a healthy PO --
    the most innocuous shape in the system.
    """
    ex = invoice(-500.00)
    po_match = matching.match_po(ex)
    assert po_match["within_tolerance"] is True, "the PO check is happy -- that is the point"
    assert po_match["is_partial"] is True

    status, reasons = verdict(ex)
    assert status == "NEEDS_REVIEW"
    fails = [r for r in reasons if r["level"] == "fail"]
    assert len(fails) == 1 and fails[0]["text"].startswith("Invalid invoice amount:")


def test_the_finding_names_the_amount_and_the_rule(db):
    _, reasons = verdict(invoice(-500.00))
    hit = next(r for r in reasons if r["text"].startswith("Invalid invoice amount:"))
    assert "total must be greater than zero" in hit["text"]
    assert "$-500.00" in hit["text"]
    assert hit["level"] == "fail"

    _, zero_reasons = verdict(invoice(0.00, number="INV-M2"))
    zero_hit = next(r for r in zero_reasons if r["text"].startswith("Invalid invoice amount:"))
    assert "$0.00" in zero_hit["text"]


def test_an_invalid_amount_consumes_no_po_budget(db):
    ex = invoice(-500.00)
    po_match = matching.match_po(ex)
    status, reasons = verdict(ex)
    storage.save_run_checked("bad.pdf", status, ex, po_match, [], reasons,
                             tolerance_for=matching.tolerance_for)
    assert storage.remaining_for_po("PO-7001") == 50_000.00


# --------------------------------------------------------------------------
# D. decision hierarchy preserved
# --------------------------------------------------------------------------

@pytest.mark.parametrize("total", [0.00, -500.00])
def test_duplicate_outranks_an_invalid_amount(db, total):
    dup = {"id": 1, "created_at": "2026-01-01T00:00:00", "status": "APPROVED"}
    status, reasons = verdict(invoice(total), dup_row=dup, dup_detail="Duplicate of run #1.")
    assert status == "REJECTED"
    assert any(r["text"].startswith("Invalid invoice amount:") for r in reasons), \
        "both findings should still be reported"


def test_unapproved_vendor_outranks_an_invalid_amount(db):
    status, _ = verdict(invoice(-500.00), vendor_ok=False)
    assert status == "REJECTED"


def test_an_invalid_amount_alone_never_rejects(db):
    """Review, not reject: a negative total is more often a credit note filed as
    an invoice than fraud, and rejecting outright removes the human judgement
    that decides which."""
    for total in (0.00, -500.00):
        assert verdict(invoice(total))[0] == "NEEDS_REVIEW"


# --------------------------------------------------------------------------
# E. no arbitrary ceiling
# --------------------------------------------------------------------------

def test_a_large_valid_amount_is_not_flagged_by_this_rule(db):
    """Size is not a validity question. A big invoice is judged by the PO."""
    assert rules.validate_amount(invoice(50_000.00)) is None
    assert rules.validate_amount(invoice(9_999_999.00)) is None


def test_a_large_amount_is_judged_by_the_po_not_by_a_ceiling(db):
    """Within the PO it approves; beyond it, the ledger holds it -- not a limit."""
    assert verdict(invoice(50_000.00))[0] == "APPROVED", "fits the $50,000 PO"

    status, reasons = verdict(invoice(500_000.00, number="INV-BIG"))
    assert status == "NEEDS_REVIEW"
    assert any("over the remaining PO balance" in r["text"] for r in reasons), \
        "the PO/tolerance rule should decide, not an invented maximum"
    assert not any(r["text"].startswith("Invalid invoice amount:") for r in reasons)


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------

def test_non_numeric_total_is_not_an_amount_failure(db):
    """Garbage in a numeric field is an extraction problem; this must not raise."""
    assert rules.validate_amount({"total": "n/a"}) is None
    assert rules.validate_amount({"total": None}) is None
    assert rules.validate_amount({}) is None


def test_decide_still_works_without_the_new_argument(db):
    """`amount` is optional, so existing callers are unaffected."""
    ex = invoice(1_000.00)
    status, _ = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                             [], True, "Vendor approved.", None, "No duplicate.",
                             matching.match_po(ex))
    assert status == "APPROVED"
