"""Recognising that a document is not an invoice at all.

Everything else in the rule engine assumes the input IS an invoice and asks
whether it may be paid. This check asks the prior question. It rejects rather
than holds, because a hold means "a human must decide whether to pay this" and
there is nothing to decide about a CV.

The safety property under test is the gate: it must fire ONLY when a model read
the document cleanly. If extraction was degraded or failed, an empty result is
evidence about the extractor, and a real invoice must never be rejected for it.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import rules   # noqa: E402


def _no_po():
    return {"po_number": None, "matched_via": None, "within_tolerance": False}


def _matched_po():
    """A cleanly matched PO, shaped as matching.match_po() returns it."""
    return {
        "po_number": "PO-1001", "po_vendor": "Acme Office Supplies", "po_status": "open",
        "matched_via": "explicit", "po_amount": 1240.00, "invoice_total": 1234.28,
        "consumed_before": 0.0, "remaining_before": 1240.00, "remaining_after": 5.72,
        "within_tolerance": True, "is_partial": True, "diff": -5.72, "tolerance": 50.0,
        "currency_mismatch": False,
    }


def _decide(extracted, route="groq-text", missing=None):
    """Run the real decision function the way the pipeline does."""
    return rules.decide(
        {"route": route},
        missing if missing is not None else rules.validate_required_fields(extracted),
        True, "vendor approved",
        None, "",
        _no_po(),
        extracted=extracted,
    )


EMPTY = {"vendor_name": None, "invoice_number": None, "invoice_date": None,
         "total": None, "po_references": [], "line_items": []}


# --------------------------------------------------------------------------
# the classifier itself
# --------------------------------------------------------------------------

def test_a_document_with_no_invoice_fields_is_not_an_invoice():
    assert rules.is_not_an_invoice(EMPTY, {"route": "groq-text"}) is True


@pytest.mark.parametrize("field,value", [
    ("vendor_name", "Acme Office Supplies"),
    ("invoice_number", "INV-2201"),
    ("total", 1234.28),
    ("invoice_date", "2026-07-12"),
])
def test_any_single_identifying_field_is_enough(field, value):
    """Invoice formats vary enormously, so the bar is one signal, not a set."""
    doc = dict(EMPTY, **{field: value})
    assert rules.is_not_an_invoice(doc, {"route": "groq-text"}) is False


@pytest.mark.parametrize("field,value", [
    ("po_references", ["PO-1001"]),
    ("line_items", [{"description": "Paper", "amount": 20.0}]),
])
def test_a_po_reference_or_line_item_also_counts(field, value):
    doc = dict(EMPTY, **{field: value})
    assert rules.is_not_an_invoice(doc, {"route": "groq-text"}) is False


# --------------------------------------------------------------------------
# the gate — the part that protects real invoices
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["none", "regex", None, ""])
def test_it_never_fires_when_extraction_was_degraded(route):
    """An empty result from a failed extractor says nothing about the document.
    A scanned invoice must be held for a human, never rejected."""
    assert rules.is_not_an_invoice(EMPTY, {"route": route}) is False


def test_an_unreadable_scan_is_still_held_not_rejected():
    status, reasons = _decide(EMPTY, route="none")
    assert status == "NEEDS_REVIEW"
    assert not any("does not appear to be an invoice" in r["text"] for r in reasons)


def test_a_regex_fallback_is_still_held_not_rejected():
    """Route 'regex' means the model route failed. A real invoice can land here."""
    status, _ = _decide(EMPTY, route="regex")
    assert status == "NEEDS_REVIEW"


# --------------------------------------------------------------------------
# end-to-end through decide()
# --------------------------------------------------------------------------

def test_a_non_invoice_is_rejected():
    status, reasons = _decide(EMPTY)
    assert status == "REJECTED"
    assert any("does not appear to be an invoice" in r["text"] for r in reasons)


def test_the_reason_says_what_was_observed():
    """The old message was 'missing required field(s)', which reads as a
    defective invoice. It has to name the real finding."""
    _, reasons = _decide(EMPTY)
    text = " ".join(r["text"] for r in reasons)
    assert "read successfully" in text
    assert "right file was submitted" in text


def test_a_partially_read_invoice_is_still_only_held():
    """One field found means it IS an invoice, just an incomplete one — that is
    a review, not a rejection."""
    partial = dict(EMPTY, vendor_name="Acme Office Supplies")
    status, reasons = _decide(partial)
    assert status == "NEEDS_REVIEW"
    assert not any("does not appear to be an invoice" in r["text"] for r in reasons)


def test_a_complete_invoice_is_unaffected():
    good = {"vendor_name": "Acme Office Supplies", "invoice_number": "INV-2201",
            "invoice_date": "2026-07-12", "total": 1234.28,
            "po_references": ["PO-1001"], "line_items": [{"amount": 1234.28}]}
    status, _ = rules.decide(
        {"route": "groq-text"},
        rules.validate_required_fields(good),
        True, "vendor approved",
        None, "",
        _matched_po(),
        extracted=good,
    )
    assert status == "APPROVED"


def test_the_audit_trail_records_the_check():
    audit = {}
    _decide(EMPTY, missing=[])
    rules.decide({"route": "groq-text"}, [], True, "ok", None, "", _no_po(),
                 audit=audit, extracted=EMPTY)
    names = [r["name"] for r in audit.get("rules", [])]
    assert "Document is an invoice" in names
    failed = [r for r in audit["rules"] if r["name"] == "Document is an invoice"]
    assert failed and failed[0]["passed"] is False


def test_absent_extraction_data_never_classifies():
    """`None` means the caller supplied no extraction data — which is NOT the
    same as data that came back empty. decide() is called without `extracted`
    on several paths, and treating that as "not an invoice" would hard-reject
    valid invoices: absence of evidence read as evidence of absence."""
    assert rules.is_not_an_invoice(None, {"route": "groq-text"}) is False
    status, _ = rules.decide({"route": "none"}, ["total"], True, "ok", None, "", _no_po())
    assert status in {"APPROVED", "NEEDS_REVIEW", "REJECTED"}
