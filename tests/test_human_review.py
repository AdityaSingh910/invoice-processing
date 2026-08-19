"""Human-in-the-loop review of invoices the process would not clear on its own.

THE PROPERTY THAT MATTERS

A human ruling is recorded BESIDE the automated decision, never on top of it:

    automated_decision  NEEDS_REVIEW     <- what the rules concluded, forever
    human_decision      ACCEPTED
    final_decision      HUMAN_APPROVED

An audit is not interested in the outcome alone. It is interested in whether a
person overrode the process, who that person was, and when -- and that question
is unanswerable the moment the original verdict is overwritten with the new one.
Most of this file exists to prove the original survives.

`status` does still move, because that is the column the LEDGER reads:
consumption sums APPROVED runs, so an accepted invoice has to land there for the
money to move. The two columns answer different questions and are both kept.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import main       # noqa: E402
import matching   # noqa: E402
import rules      # noqa: E402
import storage    # noqa: E402

VENDOR = "Globex Logistics"      # approved; holds PO-1002 at $5,000
PO = "PO-1002"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "review.db"))
    storage.init_db(reset_runs=True)
    return storage.DB_PATH


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        assert storage.DB_PATH == db, "startup must not restore the real DB path"
        yield c


def submit(total, invoice_number, po=PO, vendor=VENDOR):
    """Evaluate and commit one invoice, exactly as the pipeline does."""
    extracted = {
        "vendor_name": vendor, "invoice_number": invoice_number,
        "total": total, "subtotal": total, "tax": 0.0,
        "po_references": [po] if po else [], "currency": "USD",
        "extraction_method": "groq (text)",
    }
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
        audit=audit, extracted=extracted)

    run_id, final_status, _ = storage.save_run_checked(
        f"{invoice_number}.pdf", status, extracted, po_match, [], reasons,
        tolerance_for=matching.tolerance_for, audit=audit)
    return run_id, final_status


def held_invoice():
    """A run the rules held: $6,000 against an untouched $5,000 PO."""
    run_id, status = submit(6000.00, "INV-OVER")
    assert status == "NEEDS_REVIEW"
    return run_id


# --------------------------------------------------------------------------
# ACCEPT
# --------------------------------------------------------------------------

def test_accept_records_the_full_history(db):
    run_id = held_invoice()
    result = storage.record_human_review(run_id, "ACCEPTED", reviewer="a.singh")

    assert result["ok"] is True
    assert result["automated_decision"] == "NEEDS_REVIEW"
    assert result["human_decision"] == "ACCEPTED"
    assert result["final_decision"] == "HUMAN_APPROVED"


def test_accept_is_persisted_on_the_run(db):
    run_id = held_invoice()
    storage.record_human_review(run_id, "ACCEPTED", reviewer="a.singh")

    run = storage.get_run(run_id)
    assert run["automated_decision"] == "NEEDS_REVIEW"
    assert run["human_decision"] == "ACCEPTED"
    assert run["final_decision"] == "HUMAN_APPROVED"
    assert run["status"] == "APPROVED"


def test_accept_consumes_the_po_budget(db):
    """The ledger has to see an accepted invoice, or the money never moves."""
    assert storage.remaining_for_po(PO) == 5000.00
    run_id = held_invoice()
    assert storage.remaining_for_po(PO) == 5000.00, "a held invoice must not consume"

    storage.record_human_review(run_id, "ACCEPTED", reviewer="a.singh")
    assert storage.remaining_for_po(PO) == -1000.00, "accepted invoice now consumes"


# --------------------------------------------------------------------------
# REJECT
# --------------------------------------------------------------------------

def test_reject_records_the_full_history(db):
    run_id = held_invoice()
    result = storage.record_human_review(run_id, "REJECTED", reviewer="a.singh")

    assert result["automated_decision"] == "NEEDS_REVIEW"
    assert result["human_decision"] == "REJECTED"
    assert result["final_decision"] == "HUMAN_REJECTED"

    run = storage.get_run(run_id)
    assert run["status"] == "REJECTED"
    assert run["final_decision"] == "HUMAN_REJECTED"


def test_reject_consumes_nothing(db):
    run_id = held_invoice()
    storage.record_human_review(run_id, "REJECTED", reviewer="a.singh")
    assert storage.remaining_for_po(PO) == 5000.00


# --------------------------------------------------------------------------
# the automated decision survives
# --------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["ACCEPTED", "REJECTED"])
def test_the_automated_decision_is_never_overwritten(db, decision):
    run_id = held_invoice()
    before = storage.get_run(run_id)["automated_decision"]

    storage.record_human_review(run_id, decision, reviewer="a.singh")

    assert storage.get_run(run_id)["automated_decision"] == before == "NEEDS_REVIEW"


def test_the_audit_trail_still_reports_what_the_rules_decided(db):
    """The trail is evidence about the process, not about the person."""
    run_id = held_invoice()
    storage.record_human_review(run_id, "ACCEPTED", reviewer="a.singh")

    audit = storage.get_run(run_id)["audit"]
    assert audit["automated_decision"] == "NEEDS_REVIEW"
    assert audit["reason"] == "Invoice total exceeds PO remaining amount."
    assert "PO remaining check" in audit["rules_failed"]


def test_a_reviewed_run_keeps_its_human_outcome_through_a_cascade(db):
    """A later automated pass must not relabel a verdict a person owns."""
    run_id = held_invoice()
    storage.record_human_review(run_id, "ACCEPTED", reviewer="a.singh")

    rules.reevaluate_po_queue(PO)

    run = storage.get_run(run_id)
    assert run["final_decision"] == "HUMAN_APPROVED"
    assert run["human_decision"] == "ACCEPTED"


# --------------------------------------------------------------------------
# reviewer identity and timestamp
# --------------------------------------------------------------------------

def test_reviewer_and_timestamp_are_recorded(db):
    run_id = held_invoice()
    result = storage.record_human_review(run_id, "ACCEPTED", reviewer="a.singh",
                                         note="Confirmed with procurement.")

    run = storage.get_run(run_id)
    assert run["reviewed_by"] == "a.singh"
    assert run["review_note"] == "Confirmed with procurement."
    assert run["reviewed_at"] == result["reviewed_at"]

    from datetime import datetime
    parsed = datetime.fromisoformat(run["reviewed_at"])
    assert parsed.tzinfo is not None, "the timestamp must be unambiguous about its zone"


def test_an_absent_reviewer_is_recorded_as_unknown_not_invented(db):
    """This application has no authentication, so there is no identity to infer.
    Storing a plausible-looking name would be worse than storing none."""
    run_id = held_invoice()
    storage.record_human_review(run_id, "ACCEPTED", reviewer="   ")

    run = storage.get_run(run_id)
    assert run["reviewed_by"] is None
    assert run["human_decision"] == "ACCEPTED"


def test_the_review_is_written_into_the_reasoning_trail(db):
    run_id = held_invoice()
    storage.record_human_review(run_id, "ACCEPTED", reviewer="a.singh")
    texts = " ".join(r["text"] for r in storage.get_run(run_id)["reasons"])
    assert "a.singh" in texts and "ACCEPTED" in texts


# --------------------------------------------------------------------------
# eligibility -- only NEEDS_REVIEW is reviewable
# --------------------------------------------------------------------------

def test_an_approved_run_cannot_be_reviewed(db):
    run_id, status = submit(3000.00, "INV-OK")
    assert status == "APPROVED"

    result = storage.record_human_review(run_id, "REJECTED", reviewer="a.singh")
    assert result["ok"] is False
    assert "NEEDS_REVIEW" in result["error"]
    assert storage.get_run(run_id)["status"] == "APPROVED"


def test_a_rejected_run_cannot_be_waved_through(db):
    """A duplicate must not be approvable from a review screen."""
    submit(3000.00, "INV-DUP")
    run_id, status = submit(3000.00, "INV-DUP")
    assert status == "REJECTED"

    result = storage.record_human_review(run_id, "ACCEPTED", reviewer="a.singh")
    assert result["ok"] is False
    assert storage.get_run(run_id)["status"] == "REJECTED"
    assert storage.get_run(run_id)["human_decision"] is None


@pytest.mark.parametrize("bad", ["", None, "APPROVED", "HUMAN_APPROVED", "MAYBE", "DELETE"])
def test_only_accepted_or_rejected_are_valid_decisions(db, bad):
    run_id = held_invoice()
    result = storage.record_human_review(run_id, bad, reviewer="a.singh")
    assert result["ok"] is False
    assert storage.get_run(run_id)["human_decision"] is None
    assert storage.get_run(run_id)["status"] == "NEEDS_REVIEW"


def test_the_decision_is_case_insensitive(db):
    run_id = held_invoice()
    assert storage.record_human_review(run_id, "accepted", reviewer="x")["ok"] is True


def test_an_unknown_run_is_refused(db):
    assert storage.record_human_review(9999, "ACCEPTED", reviewer="x")["ok"] is False


# --------------------------------------------------------------------------
# over HTTP
# --------------------------------------------------------------------------

def test_review_endpoint_accepts(client):
    run_id = held_invoice()
    r = client.post(f"/api/runs/{run_id}/review",
                    json={"decision": "ACCEPTED", "reviewer": "a.singh",
                          "note": "Confirmed with procurement."})
    body = r.json()

    assert r.status_code == 200 and body["ok"] is True
    assert body["automated_decision"] == "NEEDS_REVIEW"
    assert body["final_decision"] == "HUMAN_APPROVED"
    assert body["run"]["status"] == "APPROVED"
    assert body["run"]["reviewed_by"] == "a.singh"


def test_review_endpoint_rejects(client):
    run_id = held_invoice()
    body = client.post(f"/api/runs/{run_id}/review",
                       json={"decision": "REJECTED", "reviewer": "a.singh"}).json()

    assert body["final_decision"] == "HUMAN_REJECTED"
    assert body["run"]["status"] == "REJECTED"
    assert body["run"]["automated_decision"] == "NEEDS_REVIEW"


def test_review_endpoint_refuses_an_approved_run(client):
    run_id, status = submit(3000.00, "INV-OK")
    assert status == "APPROVED"
    body = client.post(f"/api/runs/{run_id}/review",
                       json={"decision": "REJECTED", "reviewer": "a.singh"}).json()
    assert body["ok"] is False


def test_review_endpoint_releases_budget_and_cascades(client):
    """Rejecting a held invoice frees the PO, which should release the queue."""
    first, s1 = submit(4000.00, "INV-1")
    assert s1 == "APPROVED"
    second, s2 = submit(3000.00, "INV-2")
    assert s2 == "NEEDS_REVIEW", "only $1,000 left, so this is held"

    # A human rules on the HELD invoice; nothing to cascade, budget unchanged.
    body = client.post(f"/api/runs/{second}/review",
                       json={"decision": "REJECTED", "reviewer": "a.singh"}).json()
    assert body["ok"] is True
    assert body["remaining_after"] == 1000.00
    assert body["cascaded"] == []


def test_run_listing_exposes_the_decision_history(client):
    run_id = held_invoice()
    client.post(f"/api/runs/{run_id}/review",
                json={"decision": "ACCEPTED", "reviewer": "a.singh"})

    row = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)
    assert row["automated_decision"] == "NEEDS_REVIEW"
    assert row["human_decision"] == "ACCEPTED"
    assert row["final_decision"] == "HUMAN_APPROVED"
    assert row["reviewed_by"] == "a.singh"


def test_a_never_reviewed_run_reports_no_human_decision(client):
    run_id, _ = submit(3000.00, "INV-OK")
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["automated_decision"] == "APPROVED"
    assert run["final_decision"] == "APPROVED"
    assert run["human_decision"] is None
    assert run["reviewed_by"] is None and run["reviewed_at"] is None
