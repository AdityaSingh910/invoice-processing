"""Per-field confidence, source and evidence -- and the confidence gate.

WHAT THIS CLOSES

Extracted fields used to be bare values: a total read cleanly off the page was
indistinguishable from one a shaky OCR guessed at. This was Phase 2, explicitly
not started until asked for directly -- confidence must be genuine per-instance
data, not a fabricated percentage next to a dollar figure.

Two independent halves:

1. PROVENANCE -- every route now records, per field, a confidence score, a
   source location, and a quoted piece of evidence. LLM routes (Groq/Gemini)
   get the score from the MODEL ITSELF, self-reported alongside the value.
   Regex has no self-assessment, so it gets a deterministic heuristic instead:
   an explicitly labelled match scores high, a positional guess (vendor name)
   scores lower, a value computed rather than printed scores lower still.

2. THE GATE -- config.CONFIDENCE_GATED_FIELDS (the same fields
   REQUIRED_FIELDS already treats as central) can hold up the decision if the
   extractor itself is not confident in them. Deliberately narrow: it never
   REJECTS, only ever holds for review, and it only fires when a field IS
   present but uncertain -- a field that is missing entirely is a different,
   already-covered failure class (validate_required_fields), not double-
   counted here.

Honest limitation, asserted here rather than assumed: model self-reported
confidence is known to skew high and is not independently calibrated. That is
why the gate's own reason text says so, and why it is one finding among many,
not a hard veto.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import config       # noqa: E402
import extraction   # noqa: E402
import rules        # noqa: E402


# --------------------------------------------------------------------------
# regex heuristic provenance
# --------------------------------------------------------------------------

TEXT_EXPLICIT = """Acme Office Supplies
123 Commerce Way

INVOICE
Invoice #: INV-2201
Invoice Date: 2026-07-12
PO Number: PO-1001

Description        Qty   Unit Price   Amount
Copy paper           10        42.50     425.00

Subtotal: $1175.50
Tax (5%): $58.78
Total Due: $1234.28
"""

TEXT_NO_PRINTED_TOTAL = """Acme Office Supplies
INVOICE
Invoice #: INV-9999
Invoice Date: 2026-07-12
Subtotal: $500.00
Tax (5%): $25.00
"""


def test_explicit_matches_score_high_with_verified_evidence():
    inv = extraction.regex_extract(TEXT_EXPLICIT)
    for field_name in ("invoice_number", "subtotal", "tax", "total"):
        p = inv.provenance[field_name]
        assert p["confidence"] == 0.9
        assert p["evidence"]
        assert p["evidence_verified"] is True
        assert "line" in p["source"]


def test_vendor_name_scores_lower_than_an_explicit_match():
    """`_guess_vendor` is a positional heuristic, not an anchored reading --
    it must never claim the same confidence as a labelled field."""
    inv = extraction.regex_extract(TEXT_EXPLICIT)
    assert inv.provenance["vendor_name"]["confidence"] == 0.72
    assert inv.provenance["vendor_name"]["confidence"] < inv.provenance["invoice_number"]["confidence"]
    # And it must clear the gate on a clean sample -- this is not meant to
    # flag every regex-extracted invoice, only genuinely uncertain ones.
    assert inv.provenance["vendor_name"]["confidence"] >= config.CONFIDENCE_THRESHOLD


def test_a_computed_total_scores_low_and_carries_no_evidence():
    """Nothing on the document states this figure -- it was synthesised as
    subtotal + tax. That is a genuinely different, lower-trust case than a
    total the document actually printed, and the score must say so."""
    inv = extraction.regex_extract(TEXT_NO_PRINTED_TOTAL)
    assert inv.total == 525.00
    p = inv.provenance["total"]
    assert p["confidence"] == 0.55
    assert p["confidence"] < config.CONFIDENCE_THRESHOLD
    assert p["evidence"] is None
    assert p["evidence_verified"] is None
    assert p["source"] == "computed"


def test_currency_defaulted_with_no_signal_scores_lower_than_detected():
    detected = extraction.regex_extract(TEXT_EXPLICIT)  # has "$"
    defaulted = extraction.regex_extract("Some invoice with no currency marker at all.")
    assert detected.currency == "USD"
    assert defaulted.currency == "USD"
    assert detected.provenance["currency"]["confidence"] > defaulted.provenance["currency"]["confidence"]
    assert defaulted.provenance["currency"]["evidence"] is None


def test_a_missing_field_carries_no_provenance_entry_at_all():
    """Absence of a claim is different from a low claim -- a field the
    extractor never found must not appear in provenance at all."""
    inv = extraction.regex_extract("Nothing useful in this document.")
    assert inv.invoice_number is None
    assert "invoice_number" not in inv.provenance


# --------------------------------------------------------------------------
# LLM self-reported provenance (_build_provenance / _invoice_from_payload)
# --------------------------------------------------------------------------

BASE_PAYLOAD = {
    "vendor_name": "Acme Office Supplies",
    "invoice_number": "INV-2201",
    "invoice_date": "2026-07-12",
    "po_references": ["PO-1001"],
    "line_items": [],
    "subtotal": 1175.50,
    "tax": 58.78,
    "total": 1234.28,
    "currency": "USD",
}

RAW_TEXT = "Acme Office Supplies\nInvoice #: INV-2201\nTotal Due: $1234.28"


def test_llm_confidence_and_evidence_pass_through():
    payload = dict(BASE_PAYLOAD, confidence={"vendor_name": 0.97, "invoice_number": 0.99},
                   evidence={"vendor_name": "Acme Office Supplies", "invoice_number": "INV-2201"})
    inv = extraction._invoice_from_payload(payload, RAW_TEXT, "groq (text)")
    assert inv.provenance["vendor_name"]["confidence"] == 0.97
    assert inv.provenance["invoice_number"]["evidence"] == "INV-2201"


def test_confidence_is_clamped_to_zero_one():
    payload = dict(BASE_PAYLOAD, confidence={"vendor_name": 1.4, "invoice_number": -0.3})
    inv = extraction._invoice_from_payload(payload, RAW_TEXT, "groq (text)")
    assert inv.provenance["vendor_name"]["confidence"] == 1.0
    assert inv.provenance["invoice_number"]["confidence"] == 0.0


def test_evidence_verified_true_when_the_quote_is_actually_in_the_text():
    payload = dict(BASE_PAYLOAD, confidence={"invoice_number": 0.95},
                   evidence={"invoice_number": "INV-2201"})
    inv = extraction._invoice_from_payload(payload, RAW_TEXT, "groq (text)")
    assert inv.provenance["invoice_number"]["evidence_verified"] is True


def test_evidence_verified_false_when_the_model_hallucinated_the_quote():
    """A model can invent a quote as easily as a value. Presenting it as
    verified without checking would be worse than not checking at all."""
    payload = dict(BASE_PAYLOAD, confidence={"invoice_number": 0.95},
                   evidence={"invoice_number": "this text is not in the document"})
    inv = extraction._invoice_from_payload(payload, RAW_TEXT, "groq (text)")
    assert inv.provenance["invoice_number"]["evidence_verified"] is False


def test_a_field_with_neither_confidence_nor_evidence_gets_no_entry():
    payload = dict(BASE_PAYLOAD, confidence={"invoice_number": 0.9}, evidence={})
    inv = extraction._invoice_from_payload(payload, RAW_TEXT, "groq (text)")
    assert "invoice_date" not in inv.provenance
    assert "invoice_number" in inv.provenance


def test_missing_confidence_and_evidence_keys_do_not_crash():
    """Every mocked test elsewhere in this suite posts a payload with no
    confidence/evidence keys at all -- this must degrade to empty provenance,
    not raise."""
    inv = extraction._invoice_from_payload(dict(BASE_PAYLOAD), RAW_TEXT, "groq (text)")
    assert inv.provenance == {}


def test_source_says_page_one_for_a_single_page_document():
    inv = extraction._invoice_from_payload(
        dict(BASE_PAYLOAD, confidence={"total": 0.9}, evidence={"total": "1234.28"}),
        RAW_TEXT, "groq (text)", page_label=extraction._page_label(1))
    assert inv.provenance["total"]["source"] == "page 1"


def test_source_is_honest_about_multi_page_documents():
    """The text route hands the model ONE flattened string spanning every
    page -- there is no real per-page boundary to attribute a field to.
    Claiming "page 2" would fabricate precision that does not exist."""
    inv = extraction._invoice_from_payload(
        dict(BASE_PAYLOAD, confidence={"total": 0.9}, evidence={"total": "1234.28"}),
        RAW_TEXT, "groq (text)", page_label=extraction._page_label(4))
    assert "not tracked" in inv.provenance["total"]["source"]
    assert "4-page" in inv.provenance["total"]["source"]


# --------------------------------------------------------------------------
# the gate: rules.validate_confidence
# --------------------------------------------------------------------------

def _extracted(**overrides):
    base = {"vendor_name": "Acme", "invoice_number": "INV-1", "total": 100.0,
            "provenance": {}}
    base.update(overrides)
    return base


def test_a_gated_field_below_threshold_is_flagged():
    ex = _extracted(provenance={"total": {"confidence": 0.4, "source": "s", "evidence": "e"}})
    low = rules.validate_confidence(ex)
    assert len(low) == 1
    assert low[0]["field"] == "total"
    assert low[0]["confidence"] == 0.4


def test_a_gated_field_at_or_above_threshold_is_not_flagged():
    ex = _extracted(provenance={
        "total": {"confidence": config.CONFIDENCE_THRESHOLD, "source": "s", "evidence": "e"}})
    assert rules.validate_confidence(ex) == []


def test_a_field_with_no_confidence_signal_is_not_flagged():
    """Absence of a score is not evidence of a low one."""
    ex = _extracted(provenance={"total": {"confidence": None, "source": "s", "evidence": None}})
    assert rules.validate_confidence(ex) == []


def test_a_missing_field_is_not_flagged_here_even_with_a_low_score():
    """validate_required_fields() owns absence -- reporting the same fact
    through two different checks would double-count one problem."""
    ex = _extracted(total=None, provenance={"total": {"confidence": 0.1, "source": "s", "evidence": None}})
    assert rules.validate_confidence(ex) == []


def test_a_field_outside_the_gated_list_never_gates():
    ex = _extracted(provenance={"currency": {"confidence": 0.1, "source": "s", "evidence": None}})
    assert "currency" not in config.CONFIDENCE_GATED_FIELDS
    assert rules.validate_confidence(ex) == []


def test_multiple_low_confidence_fields_are_all_reported():
    ex = _extracted(provenance={
        "vendor_name": {"confidence": 0.3, "source": "s1", "evidence": "e1"},
        "total": {"confidence": 0.2, "source": "s2", "evidence": "e2"},
    })
    fields = {f["field"] for f in rules.validate_confidence(ex)}
    assert fields == {"vendor_name", "total"}


# --------------------------------------------------------------------------
# the gate wired into decide()
# --------------------------------------------------------------------------

def _clean_po_match(total):
    """A PO match that would, on its own, approve -- so a test isolating the
    confidence gate's effect isn't also tripping the unrelated "no PO found"
    review path that an empty match always produces."""
    import matching
    m = matching.empty_match(total)
    m.update(
        po_number="PO-TEST", po_vendor="Acme", po_amount=total, po_status="open",
        matched_via="explicit", consumed_before=0.0, remaining_before=total,
        remaining_after=0.0, tolerance=50.0, diff=0.0, within_tolerance=True,
        is_partial=False, over_within_tolerance=False, po_currency="USD",
        po_numbers=["PO-TEST"], allocations=[{"po_number": "PO-TEST", "amount": total}],
    )
    return m


def _decide(extracted, low_confidence, po_match=None):
    po_match = po_match if po_match is not None else _clean_po_match(extracted.get("total"))
    audit = {}
    status, reasons = rules.decide(
        {"route": "groq-text", "notes": [], "security_flags": []},
        [], True, "Vendor approved.", None, "No duplicate.", po_match,
        audit=audit, extracted=extracted, low_confidence=low_confidence)
    return status, reasons, audit


def test_low_confidence_forces_review_even_when_everything_else_is_clean():
    ex = _extracted(provenance={"vendor_name": {"confidence": 0.3, "source": "s", "evidence": "e"}})
    low = rules.validate_confidence(ex)
    assert low   # sanity: the fixture actually triggers the gate
    status, reasons, audit = _decide(ex, low)
    assert status == "NEEDS_REVIEW"
    assert any("Low extraction confidence" in r["text"] for r in reasons)
    assert "Extraction confidence" in audit["rules_failed"]


def test_low_confidence_never_rejects():
    """Uncertainty about a READING is not evidence the invoice is wrong --
    this must only ever hold, never reject, matching every other extraction-
    uncertainty signal in the pipeline."""
    ex = _extracted(provenance={"total": {"confidence": 0.0, "source": "s", "evidence": None}})
    status, _, _ = _decide(ex, rules.validate_confidence(ex))
    assert status == "NEEDS_REVIEW"


def test_high_confidence_does_not_force_review():
    ex = _extracted(provenance={
        "vendor_name": {"confidence": 0.95, "source": "s", "evidence": "e"},
        "invoice_number": {"confidence": 0.95, "source": "s", "evidence": "e"},
        "total": {"confidence": 0.95, "source": "s", "evidence": "e"},
    })
    status, _, _ = _decide(ex, rules.validate_confidence(ex))
    assert status == "APPROVED"


def test_no_confidence_signal_at_all_does_not_force_review():
    """A route that never populates provenance (or a field it was fully
    certain of) must not be penalised for silence."""
    ex = _extracted(provenance={})
    status, _, _ = _decide(ex, rules.validate_confidence(ex))
    assert status == "APPROVED"


def test_the_reason_and_suggestion_describe_confidence_when_it_is_the_only_failure():
    ex = _extracted(provenance={"total": {"confidence": 0.3, "source": "s", "evidence": "e"}})
    status, _, audit = _decide(ex, rules.validate_confidence(ex))
    assert "confidence" in audit["reason"].lower()
    assert audit["suggested_resolution"]
    assert "original document" in audit["suggested_resolution"].lower()


def test_problematic_fields_names_the_low_confidence_field():
    ex = _extracted(provenance={"vendor_name": {"confidence": 0.3, "source": "s", "evidence": "e"}})
    _, _, audit = _decide(ex, rules.validate_confidence(ex))
    assert "vendor_name" in audit["problematic_fields"]


def test_provenance_is_carried_into_the_audit_trail():
    ex = _extracted(provenance={"total": {"confidence": 0.88, "source": "page 1", "evidence": "100.00"}})
    _, _, audit = _decide(ex, rules.validate_confidence(ex))
    assert audit["provenance"]["total"]["confidence"] == 0.88
    assert audit["low_confidence_fields"] == []   # 0.88 clears the threshold


def test_low_confidence_fields_recorded_in_the_audit_trail():
    ex = _extracted(provenance={"total": {"confidence": 0.2, "source": "s", "evidence": None}})
    low = rules.validate_confidence(ex)
    _, _, audit = _decide(ex, low)
    assert audit["low_confidence_fields"] == low


# --------------------------------------------------------------------------
# problematic fields and suggested resolutions for OTHER failures
# (proves the mapping is general, not confidence-specific)
# --------------------------------------------------------------------------

def test_missing_required_fields_are_named_as_problematic():
    audit = {}
    status, _ = rules.decide(
        {"route": "regex", "notes": [], "security_flags": []},
        ["invoice_number"], True, "Vendor approved.", None, "No duplicate.",
        _clean_po_match(100.0), audit=audit,
        extracted={"vendor_name": "Acme", "total": 100.0, "provenance": {}})
    assert status == "NEEDS_REVIEW"
    assert "invoice_number" in audit["problematic_fields"]
    assert audit["suggested_resolution"]


def test_vendor_not_approved_suggests_confirming_vendor_status():
    audit = {}
    status, _ = rules.decide(
        {"route": "regex", "notes": [], "security_flags": []},
        [], False, "Vendor \"X\" is not on the approved vendor list.", None,
        "No duplicate.", _clean_po_match(100.0), audit=audit,
        extracted={"vendor_name": "X", "invoice_number": "I-1", "total": 100.0, "provenance": {}})
    assert status == "REJECTED"
    assert "vendor_name" in audit["problematic_fields"]
    assert "vendor" in audit["suggested_resolution"].lower()


def test_approved_run_has_no_problematic_fields_or_suggestion():
    audit = {}
    status, _ = rules.decide(
        {"route": "regex", "notes": [], "security_flags": []},
        [], True, "Vendor approved.", None, "No duplicate.",
        _clean_po_match(100.0), audit=audit,
        extracted={"vendor_name": "Acme", "invoice_number": "I-1", "total": 100.0, "provenance": {}})
    assert status == "APPROVED"
    assert audit["problematic_fields"] == []
    assert audit["suggested_resolution"] is None


# --------------------------------------------------------------------------
# end to end: a mocked LLM response, through real extraction, into a verdict
#
# Everything above tests the pieces (extraction's provenance, rules' gate) in
# isolation with hand-built inputs. This proves the WIRING between them: a
# model self-reporting low confidence on a real extraction call actually
# reaches rules.decide() and changes the outcome, through the same code path
# main.py uses.
# --------------------------------------------------------------------------

class _Msg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _FakeGroqClient:
    def __init__(self, payload):
        outer = self

        class Completions:
            def create(self, **kw):
                return type("R", (), {"choices": [_Msg(json.dumps(payload))]})()

        self.chat = type("C", (), {"completions": Completions()})()


def test_a_low_confidence_llm_reading_reaches_needs_review_end_to_end(monkeypatch):
    payload = {
        "vendor_name": "Acme Office Supplies", "invoice_number": "INV-2201",
        "invoice_date": "2026-07-12", "po_references": [], "line_items": [],
        "subtotal": 1175.50, "tax": 58.78, "total": 1234.28, "currency": "USD",
        # The model is genuinely unsure about the vendor name -- e.g. a
        # smudged letterhead.
        "confidence": {"vendor_name": 0.35, "invoice_number": 0.98, "total": 0.97},
        "evidence": {"vendor_name": "Acme Off?ce Supplies", "invoice_number": "INV-2201",
                     "total": "1234.28"},
    }
    monkeypatch.setattr(config, "has_groq_key", lambda: True)
    monkeypatch.setattr(config, "groq_api_key", lambda: "test-key")
    monkeypatch.setattr(extraction, "_groq_client", lambda: _FakeGroqClient(payload))

    text = "Acme Office Supplies\nInvoice #: INV-2201\nTotal Due: $1234.28"
    inv, info = extraction.extract_invoice(text.encode(), pre=(text, 1, True))
    assert info["route"] == "groq-text"
    assert inv.provenance["vendor_name"]["confidence"] == 0.35

    extracted = inv.to_dict()
    missing = rules.validate_required_fields(extracted)
    low_confidence = rules.validate_confidence(extracted)
    audit = {}
    status, reasons = rules.decide(
        info, missing, True, "Vendor approved.", None, "No duplicate.",
        _clean_po_match(extracted["total"]), audit=audit, extracted=extracted,
        low_confidence=low_confidence)

    assert status == "NEEDS_REVIEW"
    assert audit["problematic_fields"] == ["vendor_name"]
    assert any(r["text"].startswith("Low extraction confidence") for r in reasons)
    assert audit["provenance"]["invoice_number"]["confidence"] == 0.98   # unaffected
