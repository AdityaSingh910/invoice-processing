"""Currency mismatch between an invoice and the PO it matched.

THE GAP THIS CLOSES

Extraction has always produced a currency, and the purchase_orders table has
always had a currency column. Nothing downstream read either one. Every
comparison in matching is a bare number:

    diff = total - remaining_before

which says nothing about what unit the two sides are in. A EUR 3,000 invoice
against a USD 5,000 PO therefore read as a comfortable partial invoice and could
auto-approve.

THREE OUTCOMES, NOT ONE

A mismatch used to force NEEDS_REVIEW unconditionally, on the grounds that a
verdict depending on a rate fetched at run time is not reproducible by an
auditor. That objection does not apply to a table that is PINNED and stamped
with a version -- config.FX_RATES / config.FX_RATES_VERSION -- so conversion was
added, with the audit trail recording exactly which version priced it:

  1. SAME RAW NUMBER, different currency ("1500" billed as EUR against a
     "1500" USD PO). No correct conversion produces identical digits, so this
     is not an ordinary discrepancy -- REJECTED outright, not held.
  2. The pinned rate resolves the conversion within tolerance. APPROVED.
  3. No pinned rate is available for the pair, or the converted amount still
     does not fit. Held for a human, exactly as before this existed.
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

VENDOR = "Globex Logistics"    # V-002, approved in the seed data


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    conn = storage.get_conn()
    conn.execute("DELETE FROM purchase_orders")
    conn.commit()
    conn.close()
    yield schema
    pg_schema.drop_schema(schema)


def add_po(po_number, amount, currency, vendor=VENDOR, status="open"):
    conn = storage.get_conn()
    conn.execute("""INSERT INTO purchase_orders
           (po_number, vendor, amount, currency, issued_date, status, description)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                 (po_number, vendor, amount, currency, "2026-01-01", status, "test"))
    conn.commit()
    conn.close()


def invoice(total, currency, po="PO-4001", number="INV-C1"):
    return {"vendor_name": VENDOR, "invoice_number": number, "total": total,
            "po_references": [po], "currency": currency}


def verdict(po_match, dup_row=None, dup_detail="No duplicate.", audit=None, extracted=None):
    status, reasons = rules.decide(
        {"route": "regex", "notes": [], "security_flags": []},
        [], True, "Vendor approved.", dup_row, dup_detail, po_match,
        audit=audit, extracted=extracted)
    return status, reasons


# --------------------------------------------------------------------------
# A. matching currencies -> nothing changes
# --------------------------------------------------------------------------

def test_same_currency_still_approves(db):
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(3_000.00, "USD"))
    assert m["currency_mismatch"] is False
    assert m["fx"] is None
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
# B. a pinned rate resolves the mismatch -> APPROVED
# --------------------------------------------------------------------------

def test_a_pinned_rate_that_resolves_within_tolerance_approves(db):
    """EUR 2,000 converts to exactly USD 2,160 at the pinned rate, matching a
    2,160 USD PO precisely. This is the case the old design refused to look
    at; the pinned, versioned table is what makes it safe to look at now."""
    add_po("PO-4001", 2_160.00, "USD")
    m = matching.match_po(invoice(2_000.00, "EUR"))

    assert m["currency_mismatch"] is True
    assert m["fx"]["applied"] is True
    assert m["fx"]["rate"] == pytest.approx(config.FX_RATES["EUR"])
    assert m["fx"]["rate_version"] == config.FX_RATES_VERSION
    assert m["fx"]["converted_total"] == pytest.approx(2_160.00)
    assert m["diff"] == pytest.approx(0.0)
    assert m["within_tolerance"] is True

    status, reasons = verdict(m)
    assert status == "APPROVED"
    assert any(r["text"].startswith("Currency converted:") for r in reasons)
    assert any("reproducible by an auditor" in r["text"] for r in reasons)


def test_the_raw_invoice_total_is_reported_unconverted(db):
    """`invoice_total` is always what was printed on the document, in its own
    currency -- a UI pairing it with `invoice_currency` must never show a
    number silently swapped for its converted value."""
    add_po("PO-4001", 2_160.00, "USD")
    m = matching.match_po(invoice(2_000.00, "EUR"))
    assert m["invoice_total"] == 2_000.00
    assert m["fx"]["converted_total"] == 2_160.00


def test_fx_resolved_invoice_consumes_the_po_at_the_converted_amount(db):
    """The ledger must consume what the invoice is actually worth in the PO's
    currency, not the raw foreign-currency digits."""
    add_po("PO-4001", 2_160.00, "USD")
    ex = invoice(2_000.00, "EUR")
    m = matching.match_po(ex)
    status, reasons = verdict(m)
    storage.save_run_checked("fx-match.pdf", status, ex, m, [], reasons,
                             tolerance_for=matching.tolerance_for)
    assert status == "APPROVED"
    assert storage.remaining_for_po("PO-4001") == 0.00


def test_a_converted_partial_invoice_still_reads_as_a_partial(db):
    """EUR 3,000 converts to USD 3,240 against a 5,000 USD PO -- a normal
    partial invoice once expressed in the same currency, not a discrepancy."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(3_000.00, "EUR"))
    assert m["fx"]["converted_total"] == pytest.approx(3_240.00)
    assert m["is_partial"] is True
    assert verdict(m)[0] == "APPROVED"


def test_fx_conversion_is_recorded_in_the_audit_trail(db):
    add_po("PO-4001", 2_160.00, "USD")
    ex = invoice(2_000.00, "EUR")
    m = matching.match_po(ex)
    audit = {}
    status, _ = verdict(m, audit=audit, extracted=ex)

    assert status == "APPROVED"
    assert audit["currency"]["mismatch"] is True
    assert audit["currency"]["fx"]["rate_version"] == config.FX_RATES_VERSION
    assert audit["comparison"]["invoice_total"] == 2_000.00          # raw, EUR
    assert audit["comparison"]["invoice_total_converted"] == 2_160.00  # USD


# --------------------------------------------------------------------------
# C. same raw number, different currency -> REJECTED outright
# --------------------------------------------------------------------------

def test_the_same_raw_number_in_a_different_currency_is_rejected(db):
    """The dangerous case: the digits line up exactly, so a naive check would
    wave it through -- and correct conversion never produces identical digits
    in a different currency, so this is treated as a currency-code error
    rather than an ordinary discrepancy for a human to puzzle over."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(5_000.00, "EUR"))

    assert m["currency_same_number_suspected"] is True
    status, reasons = verdict(m)
    assert status == "REJECTED"
    assert any("exact same figure" in r["text"] for r in reasons)


def test_the_rejection_names_the_correctly_converted_figure(db):
    """A reviewer should not have to do the multiplication themselves to see
    why "same number" is actually wrong."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(5_000.00, "EUR"))
    _, reasons = verdict(m)
    text = next(r["text"] for r in reasons if "exact same figure" in r["text"])
    assert "5400.00" in text  # 5000 * 1.08, the rate actually converts to


def test_same_number_is_rejected_even_against_a_remaining_balance(db):
    """The collision is checked against remaining balance too, not just the
    PO's original amount, since that is what a partial invoice would compare
    against."""
    add_po("PO-4001", 8_000.00, "USD")
    ex1 = invoice(3_000.00, "USD", number="INV-FIRST")
    m1 = matching.match_po(ex1)
    storage.save_run_checked("first.pdf", "APPROVED", ex1, m1, [], [],
                             tolerance_for=matching.tolerance_for)
    assert storage.remaining_for_po("PO-4001") == 5_000.00

    m2 = matching.match_po(invoice(5_000.00, "EUR", number="INV-SECOND"))
    assert m2["currency_same_number_suspected"] is True
    assert verdict(m2)[0] == "REJECTED"


def test_same_number_rejection_does_not_consume_po_budget(db):
    add_po("PO-4001", 5_000.00, "USD")
    ex = invoice(5_000.00, "EUR")
    m = matching.match_po(ex)
    status, reasons = verdict(m)
    storage.save_run_checked("same-number.pdf", status, ex, m, [], reasons,
                             tolerance_for=matching.tolerance_for)
    assert status == "REJECTED"
    assert storage.remaining_for_po("PO-4001") == 5_000.00


def test_same_number_suspected_survives_even_without_a_pinned_rate(db):
    """The collision is a pure digit comparison -- it does not need a rate to
    detect, only to explain."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(5_000.00, "JPY"))       # JPY has no pinned rate
    assert m["fx"]["applied"] is False
    assert m["currency_same_number_suspected"] is True
    assert verdict(m)[0] == "REJECTED"


# --------------------------------------------------------------------------
# D. no pinned rate, or the converted amount still doesn't fit -> NEEDS_REVIEW
# --------------------------------------------------------------------------

def test_no_pinned_rate_holds_for_review(db):
    """JPY is not in config.FX_RATES -- unconvertible, not disguised as a
    match, so held exactly as a mismatch always was before this feature."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(3_000.00, "JPY"))
    assert m["currency_mismatch"] is True
    assert m["fx"]["applied"] is False
    assert m["fx"]["rate_version"] is None

    status, reasons = verdict(m)
    assert status == "NEEDS_REVIEW"
    assert any("No pinned exchange rate is available" in r["text"] for r in reasons)


def test_a_pinned_rate_that_still_does_not_fit_holds_for_review(db):
    """EUR 4,800 converts to USD 5,184 -- correctly converted, and still $184
    over a 5,000 USD PO with only a $50 tolerance. FX resolved the UNIT, not
    the fact that this is genuinely over budget."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(4_800.00, "EUR"))
    assert m["fx"]["applied"] is True
    assert m["fx"]["converted_total"] == pytest.approx(5_184.00)
    assert m["within_tolerance"] is False

    status, reasons = verdict(m)
    assert status == "NEEDS_REVIEW"
    assert any("does not fit the remaining balance" in r["text"] for r in reasons)


def test_mismatch_produces_exactly_one_currency_finding(db):
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(4_800.00, "EUR"))
    _, reasons = verdict(m)
    hits = [r for r in reasons if r["text"].startswith("Currency mismatch:")]
    assert len(hits) == 1
    assert hits[0]["level"] == "fail"


def test_an_unresolved_mismatch_does_not_consume_po_budget(db):
    """Held for review means held: it must not silently eat the balance."""
    add_po("PO-4001", 5_000.00, "USD")
    ex = invoice(4_800.00, "EUR")
    m = matching.match_po(ex)
    status, reasons = verdict(m)
    storage.save_run_checked("mismatch.pdf", status, ex, m, [], reasons,
                             tolerance_for=matching.tolerance_for)
    assert status == "NEEDS_REVIEW"
    assert storage.remaining_for_po("PO-4001") == 5_000.00


# --------------------------------------------------------------------------
# E. the decision hierarchy is preserved
# --------------------------------------------------------------------------

def test_duplicate_outranks_an_unresolved_currency_mismatch(db):
    """REJECTED must still beat NEEDS_REVIEW when both fire."""
    add_po("PO-4001", 5_000.00, "USD")
    m = matching.match_po(invoice(4_800.00, "EUR"))
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


def test_fx_approval_does_not_bypass_other_blocking_checks(db):
    """A clean currency conversion must not override a missing required field
    or any other independent reason to hold the invoice."""
    add_po("PO-4001", 2_160.00, "USD")
    m = matching.match_po(invoice(2_000.00, "EUR"))
    status, _ = rules.decide({"route": "regex", "notes": [], "security_flags": []},
                             ["invoice_number"], True, "Vendor approved.", None,
                             "No duplicate.", m)
    assert status == "NEEDS_REVIEW"


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
    assert m["fx"] is None
    assert verdict(m)[0] == "APPROVED", "existing behaviour must be preserved"


def test_unknown_po_currency_does_not_flag_a_mismatch(db):
    add_po("PO-4001", 5_000.00, None)
    m = matching.match_po(invoice(3_000.00, "EUR"))
    assert m["po_currency"] is None
    assert m["currency_mismatch"] is False
    assert m["fx"] is None


def test_no_po_match_carries_the_currency_keys(db):
    """empty_match must expose the same keys, or consumers hit KeyError on the
    one path where nothing matched."""
    m = matching.empty_match(100.0)
    assert m["currency_mismatch"] is False
    assert m["invoice_currency"] is None and m["po_currency"] is None
    assert m["fx"] is None
    assert m["currency_same_number_suspected"] is False


# --------------------------------------------------------------------------
# fx_convert() itself
# --------------------------------------------------------------------------

def test_fx_convert_returns_none_for_an_unpinned_currency():
    assert matching.fx_convert(100.0, "JPY", "USD") is None
    assert matching.fx_convert(100.0, "USD", "JPY") is None


def test_fx_convert_same_currency_is_identity():
    assert matching.fx_convert(123.45, "USD", "USD") == 123.45


def test_fx_convert_missing_currency_returns_none():
    assert matching.fx_convert(100.0, None, "USD") is None
    assert matching.fx_convert(100.0, "USD", None) is None


# --------------------------------------------------------------------------
# F. nothing else moved
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
