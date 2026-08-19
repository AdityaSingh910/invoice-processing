"""Currency mismatch between an invoice and the PO it matched.

THE GAP THIS CLOSES

Extraction has always produced a currency, and the purchase_orders table has
always had a currency column. Nothing downstream read either one. Every
comparison in matching is a bare number:

    diff = total - remaining_before

which says nothing about what unit the two sides are in. A EUR 3,000 invoice
against a USD 5,000 PO therefore read as a comfortable partial invoice and could
auto-approve.

The rule is deterministic and lives in Python: matching sets `currency_mismatch`,
`decide()` turns it into NEEDS_REVIEW. No conversion, no rate lookup, no third
party -- a verdict that depended on an exchange rate fetched at run time would
not be reproducible by an auditor, which is the property the whole design exists
to protect.
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

VENDOR = "Globex Logistics"    # V-002, approved in the seed data


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "currency.db"))
    storage.init_db(reset_runs=True)
    conn = storage.get_conn()
    conn.execute("DELETE FROM purchase_orders")
    conn.commit()
    conn.close()
    return storage.DB_PATH


def add_po(po_number, amount, currency, vendor=VENDOR, status="open"):
    conn = storage.get_conn()
    conn.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?)",
                 (po_number, vendor, amount, currency, "2026-01-01", status, "test"))
    conn.commit()
    conn.close()


def invoice(total, currency, po="PO-4001", number="INV-C1"):
    return {"vendor_name": VENDOR, "invoice_number": number, "total": total,
            "po_references": [po], "currency": currency}


def verdict(po_match, dup_row=None, dup_detail="No duplicate."):
    status, reasons = rules.decide(
        {"route": "regex", "notes": [], "security_flags": []},
        [], True, "Vendor approved.", dup_row, dup_detail, po_match)
    return status, reasons


# --------------------------------------------------------------------------
# A. matching currencies -> nothing changes
# --------------------------------------------------------------------------

def test_same_currency_still_approves(db):
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(3_000.00, "USD"))
    assert m["currency_mismatch"] is False
    assert m["invoice_currency"] == "USD" and m["po_currency"] == "USD"
    assert verdict(m)[0] == "APPROVED"


def test_matching_non_usd_currencies_also_approve(db):
    """The rule is 'they differ', not 'anything that is not USD'."""
    add_po("PO-4001", 5_000.00, "EUR")
    m = matching.match_po(invoice(3_000.00, "EUR"))
    assert m["currency_mismatch"] is False
    assert verdict(m)[0] == "APPROVED"


def test_currency_comparison_ignores_case_and_whitespace(db):
    """`eur` and `EUR ` are the same currency; a formatting difference must not
    manufacture a mismatch and send a clean invoice to a human."""
    add_po("PO-4001", 5_000.00, "EUR")
    m = matching.match_po(invoice(3_000.00, " eur "))
    assert m["currency_mismatch"] is False
    assert verdict(m)[0] == "APPROVED"


# --------------------------------------------------------------------------
# B/C. mismatch -> NEEDS_REVIEW, with a clear finding
# --------------------------------------------------------------------------

def test_currency_mismatch_forces_review(db):
    """Everything else about this invoice is clean; only the unit differs."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(3_000.00, "EUR"))
    assert m["currency_mismatch"] is True
    assert m["invoice_currency"] == "EUR" and m["po_currency"] == "USD"

    status, _ = verdict(m)
    assert status == "NEEDS_REVIEW", "a EUR invoice must not auto-approve against a USD PO"


def test_mismatch_is_flagged_even_when_the_amount_looks_perfect(db):
    """The dangerous case: the numbers line up exactly, so nothing else objects."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(5_000.00, "EUR"))
    assert m["within_tolerance"] is True, "the numeric comparison is happy -- that is the point"
    assert verdict(m)[0] == "NEEDS_REVIEW"


def test_mismatch_produces_a_clear_audit_finding(db):
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(3_000.00, "EUR"))
    _, reasons = verdict(m)

    hits = [r for r in reasons if r["text"].startswith("Currency mismatch:")]
    assert len(hits) == 1, "exactly one currency finding expected"
    text, level = hits[0]["text"], hits[0]["level"]
    assert "invoice is EUR" in text
    assert "PO-4001 is USD" in text
    assert level == "fail", "the finding must be tagged as one that drove the verdict"


def test_no_conversion_is_attempted(db):
    """No FX. The amounts must pass through untouched."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(3_000.00, "EUR"))
    assert m["invoice_total"] == 3_000.00
    assert m["remaining_before"] == 5_000.00
    assert m["diff"] == -2_000.00, "amounts must not be rescaled by any rate"


# --------------------------------------------------------------------------
# D. the decision hierarchy is preserved
# --------------------------------------------------------------------------

def test_duplicate_outranks_currency_mismatch(db):
    """REJECTED must still beat NEEDS_REVIEW when both fire."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(3_000.00, "EUR"))
    assert m["currency_mismatch"] is True

    dup = {"id": 1, "created_at": "2026-01-01T00:00:00", "status": "APPROVED"}
    status, reasons = verdict(m, dup_row=dup, dup_detail="Duplicate of run #1.")
    assert status == "REJECTED"
    # Both findings are still reported -- the reviewer sees the whole picture.
    assert any(r["text"].startswith("Currency mismatch:") for r in reasons)


def test_unapproved_vendor_outranks_currency_mismatch(db):
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(3_000.00, "EUR"))
    status, _ = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                             [], False, "Vendor not approved.", None, "No duplicate.", m)
    assert status == "REJECTED"


# --------------------------------------------------------------------------
# missing / unknown currency -- do not invent one
# --------------------------------------------------------------------------

@pytest.mark.parametrize("missing", [None, "", "   "])
def test_unknown_invoice_currency_does_not_flag_a_mismatch(db, missing):
    """An absent currency is not evidence of a different currency.

    Inventing one would either fabricate a mismatch on a clean invoice or
    fabricate agreement on a suspect one. Neither is safe, so no comparison is
    made and the existing behaviour is preserved untouched.
    """
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(3_000.00, missing))
    assert m["invoice_currency"] is None
    assert m["currency_mismatch"] is False
    assert verdict(m)[0] == "APPROVED", "existing behaviour must be preserved"


def test_unknown_po_currency_does_not_flag_a_mismatch(db):
    add_po("PO-4001", 5_000.00, None)
    m = matching.match_po(invoice(3_000.00, "EUR"))
    assert m["po_currency"] is None
    assert m["currency_mismatch"] is False


def test_no_po_match_carries_the_currency_keys(db):
    """empty_match must expose the same keys, or consumers hit KeyError on the
    one path where nothing matched."""
    m = matching.empty_match(100.0)
    assert m["currency_mismatch"] is False
    assert m["invoice_currency"] is None and m["po_currency"] is None


# --------------------------------------------------------------------------
# E/F. nothing else moved
# --------------------------------------------------------------------------

def test_split_po_still_works_within_one_currency(db):
    add_po("PO-4001", 5_000.00, "USD")

    def submit(total, number):
        ex = invoice(total, "USD", number=number)
        m = matching.match_po(ex)
        status, reasons = verdict(m)
        _, final, _ = storage.save_run_checked(f"{number}.pdf", status, ex, m, [], reasons,
                                               tolerance_for=matching.tolerance_for)
        return final

    assert submit(3_000.00, "INV-S1") == "APPROVED"
    assert storage.remaining_for_po("PO-4001") == 2_000.00
    assert submit(2_000.00, "INV-S2") == "APPROVED"
    assert storage.remaining_for_po("PO-4001") == 0.00
    assert submit(2_500.00, "INV-S3") == "NEEDS_REVIEW"


def test_a_mismatched_invoice_does_not_consume_po_budget(db):
    """Held for review means held: it must not silently eat the balance."""
    add_po("PO-4001", 5_000.00, "USD")
    ex = invoice(3_000.00, "EUR")
    m = matching.match_po(ex)
    status, reasons = verdict(m)
    storage.save_run_checked("mismatch.pdf", status, ex, m, [], reasons,
                             tolerance_for=matching.tolerance_for)
    assert status == "NEEDS_REVIEW"
    assert storage.remaining_for_po("PO-4001") == 5_000.00
