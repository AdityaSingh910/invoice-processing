"""The deterministic audit trail: what the process compared, and why it decided.

WHAT THIS FILE DEFENDS

The trail is not a report generated *about* a decision. It is emitted BY the
evaluation that produces the decision -- `rules.decide(audit={})` fills the dict
as it walks its checks, next to the branches that set `reject` / `review`.

That distinction is the whole point, and it is what most of these tests assert.
A trail assembled by a second pass over the result can disagree with the verdict
it claims to explain: someone changes a threshold in `decide()`, the explainer
keeps its own copy, and the trail now confidently describes a decision the system
did not make. Here there is one evaluation and one set of numbers.

No model is involved at any point. Every sentence in the trail is written by
Python from values Python computed, which is what makes it reproducible for an
auditor.
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

VENDOR = "Globex Logistics"     # approved, holds PO-1002 at $5,000
PO = "PO-1002"


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    yield schema
    pg_schema.drop_schema(schema)


def evaluate(total, invoice_number="INV-1", po=PO, vendor=VENDOR, **over):
    """Run one invoice through the real evaluation and return (status, audit).

    Deliberately the same calls the pipeline makes, in the same order.
    """
    extracted = {
        "vendor_name": vendor,
        "invoice_number": invoice_number,
        "total": total,
        "subtotal": total,
        "tax": 0.0,
        "po_references": [po] if po else [],
        "currency": "USD",
        "extraction_method": "groq (text)",
    }
    extracted.update(over)
    info = {"route": "groq-text", "provider": "groq", "notes": [], "security_flags": []}

    po_match = matching.match_po(extracted)
    vendor_ok, _, vendor_detail = rules.vendor_check(extracted)
    dup_row, dup_detail = rules.duplicate_check(extracted)

    audit = {}
    status, reasons = rules.decide(
        info, rules.validate_required_fields(extracted), vendor_ok, vendor_detail,
        dup_row, dup_detail, po_match,
        arithmetic=rules.validate_arithmetic(extracted),
        amount=rules.validate_amount(extracted),
        audit=audit, extracted=extracted,
    )
    return status, audit, extracted, po_match, reasons


def commit(status, extracted, po_match, reasons, audit, filename="inv.pdf"):
    return storage.save_run_checked(filename, status, extracted, po_match, [], reasons,
                                    tolerance_for=matching.tolerance_for, audit=audit)


def rule(audit, name):
    return next(c for c in audit["rules"] if c["name"] == name)


# --------------------------------------------------------------------------
# the trail comes from the decision, not from a second opinion
# --------------------------------------------------------------------------

def test_audit_decision_always_matches_the_returned_status(db):
    """The one invariant that makes the trail trustworthy."""
    for total in (3000.00, 99_000.00):
        status, audit, *_ = evaluate(total)
        assert audit["automated_decision"] == status


def test_decide_still_works_without_an_audit_dict(db):
    """The audit is opt-in. Sixteen existing call sites pass no such argument."""
    status, reasons = rules.decide(
        {"route": "regex", "notes": [], "security_flags": []}, [], True, "ok",
        None, "", matching.empty_match(100.0))
    assert status == "NEEDS_REVIEW"
    assert reasons


def test_rules_failed_is_consistent_with_the_rule_list(db):
    status, audit, *_ = evaluate(99_000.00)
    assert audit["rules_failed"] == [c["name"] for c in audit["rules"] if not c["passed"]]
    assert audit["rules_passed"] == [c["name"] for c in audit["rules"] if c["passed"]]
    assert set(audit["rules_passed"]) & set(audit["rules_failed"]) == set()


# --------------------------------------------------------------------------
# APPROVED
# --------------------------------------------------------------------------

def test_audit_trail_for_an_approved_invoice(db):
    status, audit, *_ = evaluate(3000.00)

    assert status == "APPROVED"
    assert audit["automated_decision"] == "APPROVED"
    assert audit["reason"] == "All checks passed."
    assert audit["rules_failed"] == []
    for name in ("Vendor approved", "PO matched", "PO remaining check", "Duplicate check"):
        assert rule(audit, name)["passed"] is True


def test_approved_trail_still_shows_the_numbers_it_compared(db):
    """An approval has to be auditable too -- 'it passed' is not evidence."""
    _, audit, *_ = evaluate(3000.00)
    c = audit["comparison"]
    assert c["invoice_total"] == 3000.00
    assert c["po_remaining"] == 5000.00
    assert c["variance"] == -2000.00
    assert c["tolerance"] == 50.00


# --------------------------------------------------------------------------
# NEEDS_REVIEW
# --------------------------------------------------------------------------

def test_audit_trail_for_a_needs_review_invoice(db):
    """The worked example: an invoice over the remaining PO balance."""
    status, audit, extracted, po_match, reasons = evaluate(3000.00)
    commit(status, extracted, po_match, reasons, audit, "a.pdf")

    status, audit, *_ = evaluate(4000.00, invoice_number="INV-2")

    assert status == "NEEDS_REVIEW"
    assert audit["reason"] == "Invoice total exceeds PO remaining amount."
    assert audit["rules_failed"] == ["PO remaining check"]
    assert rule(audit, "Vendor approved")["passed"] is True
    assert rule(audit, "PO matched")["passed"] is True
    assert rule(audit, "Duplicate check")["passed"] is True


def test_needs_review_trail_reports_the_real_variance(db):
    status, audit, extracted, po_match, reasons = evaluate(3000.00)
    commit(status, extracted, po_match, reasons, audit, "a.pdf")

    _, audit, *_ = evaluate(4000.00, invoice_number="INV-2")
    c = audit["comparison"]

    assert c["invoice_total"] == 4000.00
    assert c["po_remaining"] == 2000.00        # 5000 - 3000 consumed
    assert c["variance"] == 2000.00            # 4000 - 2000
    assert c["tolerance"] == 50.00
    assert c["consumed_before"] == 3000.00
    # The variance is the arithmetic the decision actually turned on.
    assert c["variance"] > c["tolerance"]


def test_the_reason_is_the_first_failing_rule(db):
    """Document integrity is established before anything is compared to a PO, so
    the first failure is the one closest to the root of the problem."""
    _, audit, *_ = evaluate(4000.00, vendor="Nobody Ltd", invoice_number="INV-9")
    assert "Vendor approved" in audit["rules_failed"]
    assert audit["reason"] == "Vendor is not on the approved list."


def test_every_failing_rule_carries_a_deterministic_reason(db):
    _, audit, *_ = evaluate(0.0, invoice_number="INV-Z")
    for c in audit["rules"]:
        if not c["passed"]:
            assert c["reason"], f"{c['name']} failed without a reason"
            assert isinstance(c["reason"], str)


def test_rejected_invoice_names_the_duplicate_rule(db):
    status, audit, extracted, po_match, reasons = evaluate(3000.00, invoice_number="INV-DUP")
    commit(status, extracted, po_match, reasons, audit, "first.pdf")

    status, audit, *_ = evaluate(3000.00, invoice_number="INV-DUP")
    assert status == "REJECTED"
    assert audit["automated_decision"] == "REJECTED"
    assert rule(audit, "Duplicate check")["passed"] is False
    assert audit["reason"] == "Invoice duplicates an earlier submission."


# --------------------------------------------------------------------------
# traceability: PO, source file, row
# --------------------------------------------------------------------------

def test_audit_cites_the_po_and_where_its_record_came_from(db):
    _, audit, *_ = evaluate(3000.00)
    po = audit["purchase_order"]

    assert po["po_number"] == PO
    assert po["matched_via"] == "explicit"
    assert po["source_file"] == "purchase_orders.json"
    assert po["source_row"] == 2, "PO-1002 is the second record in the procurement file"
    assert po["po_amount"] == 5000.00


def test_source_row_matches_the_records_real_position(db):
    """Read against the seed file itself, so the row cannot quietly drift."""
    import json
    with open(os.path.join(ROOT, "data", "purchase_orders.json"), encoding="utf-8") as f:
        seed = json.load(f)
    for i, record in enumerate(seed):
        stored = storage.get_po(record["po_number"])
        assert stored["source_row"] == i + 1
        assert stored["source_file"] == "purchase_orders.json"


def test_row_number_is_never_fabricated(db):
    """A PO inserted without provenance reports none, rather than a plausible number."""
    conn = storage.get_conn()
    conn.execute(
        """INSERT INTO purchase_orders
           (po_number, vendor, amount, currency, issued_date, status, description)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        ("PO-NOSRC", VENDOR, 4000.0, "USD", "2026-01-01", "open", "no provenance"))
    conn.commit()
    conn.close()

    _, audit, *_ = evaluate(1000.00, po="PO-NOSRC")
    assert audit["purchase_order"]["po_number"] == "PO-NOSRC"
    assert audit["purchase_order"]["source_row"] is None
    assert audit["purchase_order"]["source_file"] is None


def test_no_po_match_reports_no_source(db):
    _, audit, *_ = evaluate(3000.00, po=None, invoice_number="INV-NOPO")
    assert audit["purchase_order"]["po_number"] is None
    assert audit["purchase_order"]["source_row"] is None
    assert rule(audit, "PO matched")["passed"] is False


# --------------------------------------------------------------------------
# identity and provenance of the reading itself
# --------------------------------------------------------------------------

def test_audit_records_which_provider_read_the_invoice(db):
    _, audit, *_ = evaluate(3000.00)
    assert audit["extraction"]["route"] == "groq-text"
    assert audit["extraction"]["provider"] == "groq"
    assert audit["extraction"]["method"] == "groq (text)"


def test_audit_records_the_invoice_identity(db):
    _, audit, *_ = evaluate(3000.00, invoice_number="INV-777")
    assert audit["invoice"] == {
        "invoice_number": "INV-777", "vendor": VENDOR,
        "total": 3000.00, "currency": "USD",
    }


def test_audit_does_not_invent_invoice_identity(db):
    """Called without `extracted`, the trail leaves identity null rather than guessing."""
    audit = {}
    rules.decide({"route": "regex", "notes": [], "security_flags": []}, [], True, "ok",
                 None, "", matching.empty_match(100.0), audit=audit)
    assert audit["invoice"]["invoice_number"] is None
    assert audit["invoice"]["vendor"] is None


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def test_audit_is_stored_with_the_run_and_read_back(db):
    status, audit, extracted, po_match, reasons = evaluate(3000.00)
    run_id, final_status, _ = commit(status, extracted, po_match, reasons, audit)

    stored = storage.get_run(run_id)
    assert stored["audit"]["automated_decision"] == final_status
    assert stored["audit"]["purchase_order"]["source_row"] == 2
    assert stored["audit"]["rules_failed"] == []


def test_stored_audit_follows_a_commit_time_downgrade(db):
    """`save_run_checked` can downgrade APPROVED under the write lock when another
    invoice took the balance first. The trail must describe what was COMMITTED."""
    s1, a1, e1, p1, r1 = evaluate(3000.00, invoice_number="INV-A")
    commit(s1, e1, p1, r1, a1, "a.pdf")

    # Evaluated against a stale $2,000 balance, so it decides APPROVED...
    s2, a2, e2, p2, r2 = evaluate(2000.00, invoice_number="INV-B")
    assert s2 == "APPROVED"
    # ...but another invoice lands before it commits.
    s3, a3, e3, p3, r3 = evaluate(2000.00, invoice_number="INV-C")
    commit(s3, e3, p3, r3, a3, "c.pdf")

    run_id, final_status, extra = commit(s2, e2, p2, r2, a2, "b.pdf")
    assert final_status == "NEEDS_REVIEW" and extra is not None

    stored = storage.get_run(run_id)["audit"]
    assert stored["automated_decision"] == "NEEDS_REVIEW"
    assert "PO remaining check" in stored["rules_failed"]
    assert "Balance changed" in stored["reason"]


def test_a_run_without_an_audit_hydrates_to_none(db):
    """Runs written before the trail existed must still load."""
    status, _, extracted, po_match, reasons = evaluate(3000.00)
    run_id, _, _ = storage.save_run_checked(
        "legacy.pdf", status, extracted, po_match, [], reasons,
        tolerance_for=matching.tolerance_for)
    assert storage.get_run(run_id)["audit"] is None


# --------------------------------------------------------------------------
# no model anywhere near it
# --------------------------------------------------------------------------

def test_the_trail_is_built_without_any_model_call(db, monkeypatch):
    """Blow up if extraction is touched during evaluation."""
    import extraction

    def explode(*a, **k):
        raise AssertionError("the audit trail must not call a model")

    monkeypatch.setattr(extraction, "_groq_client", explode)
    monkeypatch.setattr(extraction, "_client", explode)

    status, audit, *_ = evaluate(99_000.00)
    assert audit["reason"] == "Invoice total exceeds PO remaining amount."


def test_rules_module_imports_no_model_sdk():
    src = open(os.path.join(BACKEND, "rules.py"), encoding="utf-8").read()
    for sdk in ("import groq", "from groq", "google.genai", "import extraction"):
        assert sdk not in src, f"rules.py should not depend on {sdk}"
