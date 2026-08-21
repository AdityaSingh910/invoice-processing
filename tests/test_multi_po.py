"""Invoices that cover more than one purchase order.

THE DEFECT THIS CLOSES

`match_po` bound the FIRST resolvable PO reference and dropped the rest, and the
ledger charged the run's whole total to it. An invoice for $6,240 covering
PO-1001 ($1,240) and PO-1002 ($5,000) therefore left:

    PO-1001 remaining: -5000.00     over-consumed by the value of PO-1002
    PO-1002 remaining:  5000.00     never touched

test_the_original_defect_is_closed pins exactly that scenario.

THE JUDGEMENT CALL WORTH DEFENDING

Binding every PO is uncontroversial. Deciding how much of the invoice belongs to
each is not: nothing on the document says. Line items carry no PO references, so
any division is computed rather than read.

So the process computes one -- fill each PO to its remaining balance in the order
the invoice named them -- and then refuses to act on it. A multi-PO invoice is
always NEEDS_REVIEW, even when the combined balance covers it comfortably, for
the same reason an INFERRED single-PO match is: approving would commit money in
amounts no document and no person ever specified. The proposal is stored and
shown so the reviewer confirms figures rather than working them out.

The invariant underneath all of it: **allocations sum to the invoice total.**
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

ACME = "Acme Office Supplies"        # PO-1001, $1,240
GLOBEX = "Globex Logistics"          # PO-1002, $5,000
INITECH = "Initech Consulting"       # PO-1003, $8,200


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    yield schema
    pg_schema.drop_schema(schema)


def invoice(total, refs, vendor=ACME, number="INV-M1", currency="USD"):
    return {"vendor_name": vendor, "invoice_number": number, "total": total,
            "currency": currency, "po_references": list(refs), "line_items": []}


def decide(po_match, extracted):
    audit = {}
    status, reasons = rules.decide(
        {"route": "groq-text"}, [], True, "Vendor approved", None, "No prior run",
        po_match, audit=audit, extracted=extracted)
    return status, reasons, audit


def commit(extracted, po_match, status, reasons, filename="multi.pdf"):
    run_id, final, _ = storage.save_run_checked(
        filename, status, extracted, po_match, [], reasons,
        tolerance_for=matching.tolerance_for, audit={})
    return run_id, final


# --------------------------------------------------------------------------
# the defect itself
# --------------------------------------------------------------------------

def test_the_original_defect_is_closed(db):
    """$6,240 across PO-1001 ($1,240) and PO-1002 ($5,000).

    Each PO must be charged what it authorised -- not one charged everything and
    the other nothing.
    """
    ext = invoice(6240.00, ["PO-1001", "PO-1002"])
    pm = matching.match_po(ext)
    status, reasons, _ = decide(pm, ext)
    run_id, _ = commit(ext, pm, status, reasons)

    storage.set_run_status(run_id, "APPROVED", "reviewer confirmed the split")

    assert storage.remaining_for_po("PO-1001") == 0.0        # was -5000.00
    assert storage.remaining_for_po("PO-1002") == 0.0        # was  5000.00
    assert storage.consumed_amount_for_po("PO-1001") == 1240.0
    assert storage.consumed_amount_for_po("PO-1002") == 5000.0


def test_reversing_a_multi_po_run_refunds_every_po_it_touched(db):
    ext = invoice(6240.00, ["PO-1001", "PO-1002"])
    pm = matching.match_po(ext)
    status, reasons, _ = decide(pm, ext)
    run_id, _ = commit(ext, pm, status, reasons)
    storage.set_run_status(run_id, "APPROVED", "accepted")

    storage.set_run_status(run_id, "NEEDS_REVIEW", "reversed")

    assert storage.remaining_for_po("PO-1001") == 1240.0
    assert storage.remaining_for_po("PO-1002") == 5000.0


# --------------------------------------------------------------------------
# binding
# --------------------------------------------------------------------------

def test_every_referenced_po_is_bound(db):
    pm = matching.match_po(invoice(6240.00, ["PO-1001", "PO-1002"]))
    assert pm["po_numbers"] == ["PO-1001", "PO-1002"]
    assert pm["is_multi"] is True
    assert pm["matched_via"] == "explicit"


def test_the_primary_po_is_still_the_first_reference(db):
    """`runs.po_number`, the dashboard and every existing consumer read this."""
    pm = matching.match_po(invoice(6240.00, ["PO-1002", "PO-1001"]))
    assert pm["po_number"] == "PO-1002"
    assert pm["po_numbers"] == ["PO-1002", "PO-1001"]


def test_repeated_references_to_one_po_bind_it_once(db):
    """"PO-1001" printed in a header and again in a footer is one PO, not two --
    counting it twice would double its balance."""
    pm = matching.match_po(invoice(1240.00, ["PO-1001", "PO-1001"]))
    assert pm["po_numbers"] == ["PO-1001"]
    assert pm["is_multi"] is False
    assert pm["remaining_before"] == 1240.0


def test_references_that_match_no_po_are_ignored(db):
    pm = matching.match_po(invoice(1240.00, ["PO-9999", "PO-1001"]))
    assert pm["po_numbers"] == ["PO-1001"]
    assert pm["is_multi"] is False


def test_combined_figures_are_the_sum_of_the_bound_pos(db):
    pm = matching.match_po(invoice(6240.00, ["PO-1001", "PO-1002"]))
    assert pm["po_amount"] == 6240.0
    assert pm["remaining_before"] == 6240.0
    assert pm["diff"] == 0.0


def test_combined_balance_reflects_what_prior_runs_consumed(db):
    """PO-1002 is half spent, so the combined balance must say so."""
    ext = invoice(3000.00, ["PO-1002"], vendor=GLOBEX, number="INV-EARLY")
    pm = matching.match_po(ext)
    run_id, _ = commit(ext, pm, "APPROVED", [])

    pm = matching.match_po(invoice(4240.00, ["PO-1001", "PO-1002"]))
    assert pm["remaining_before"] == 1240.0 + 2000.0
    assert [a["remaining_before"] for a in pm["allocations"]] == [1240.0, 2000.0]


# --------------------------------------------------------------------------
# how the total is split
# --------------------------------------------------------------------------

def test_allocations_always_sum_to_the_invoice_total(db):
    """The invariant. If it breaks, the ledger describes money nobody billed."""
    for total in (100.00, 1240.00, 3000.00, 6240.00, 7000.00, 20000.00):
        pm = matching.match_po(invoice(total, ["PO-1001", "PO-1002"]))
        assert round(sum(a["amount"] for a in pm["allocations"]), 2) == total


def test_pos_are_filled_in_the_order_the_invoice_referenced_them(db):
    pm = matching.match_po(invoice(3000.00, ["PO-1001", "PO-1002"]))
    # PO-1001 settled in full first, the balance to PO-1002.
    assert [a["amount"] for a in pm["allocations"]] == [1240.0, 1760.0]


def test_reversing_the_reference_order_reverses_the_fill_order(db):
    pm = matching.match_po(invoice(3000.00, ["PO-1002", "PO-1001"]))
    assert [a["amount"] for a in pm["allocations"]] == [3000.0, 0.0]


def test_excess_beyond_every_balance_lands_on_the_last_po_and_is_flagged(db):
    """$7,000 against $6,240 of combined balance. The $760 has to be visible
    somewhere rather than quietly disappearing out of the total."""
    pm = matching.match_po(invoice(7000.00, ["PO-1001", "PO-1002"]))
    amounts = [a["amount"] for a in pm["allocations"]]
    assert amounts == [1240.0, 5760.0]
    assert sum(amounts) == 7000.0
    assert pm["allocations"][0]["over"] is False
    assert pm["allocations"][1]["over"] is True
    assert pm["within_tolerance"] is False       # combined check catches it too


def test_each_allocation_reports_the_balance_it_would_leave(db):
    pm = matching.match_po(invoice(3000.00, ["PO-1001", "PO-1002"]))
    assert pm["allocations"][0]["remaining_after"] == 0.0
    assert pm["allocations"][1]["remaining_after"] == 3240.0


# --------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------

def test_a_multi_po_invoice_is_never_auto_approved(db):
    """Even when the combined balance covers it exactly. The split was computed,
    not read off the document, so a person confirms it."""
    ext = invoice(6240.00, ["PO-1001", "PO-1002"])
    pm = matching.match_po(ext)
    assert pm["within_tolerance"] is True        # the money is all there
    status, _, _ = decide(pm, ext)
    assert status == "NEEDS_REVIEW"


def test_a_well_under_budget_multi_po_invoice_is_still_held(db):
    ext = invoice(500.00, ["PO-1001", "PO-1002"])
    status, _, _ = decide(matching.match_po(ext), ext)
    assert status == "NEEDS_REVIEW"


def test_the_reason_names_every_po_and_the_proposed_amounts(db):
    """A reviewer should be able to confirm figures, not reconstruct them."""
    ext = invoice(3000.00, ["PO-1001", "PO-1002"])
    _, reasons, _ = decide(matching.match_po(ext), ext)
    text = " ".join(r["text"] for r in reasons if r["level"] == "fail")
    assert "PO-1001" in text and "PO-1002" in text
    assert "$1240.00" in text and "$1760.00" in text
    assert "calculated" in text.lower()


def test_the_split_rule_is_recorded_as_failed_in_the_audit_trail(db):
    ext = invoice(6240.00, ["PO-1001", "PO-1002"])
    _, _, audit = decide(matching.match_po(ext), ext)
    assert "Invoice-to-PO split stated" in audit["rules_failed"]
    assert audit["automated_decision"] == "NEEDS_REVIEW"


def test_a_single_po_invoice_passes_the_split_rule_and_can_still_approve(db):
    """The guard must not catch ordinary invoices."""
    ext = invoice(1240.00, ["PO-1001"])
    status, _, audit = decide(matching.match_po(ext), ext)
    assert status == "APPROVED"
    assert "Invoice-to-PO split stated" in audit["rules_passed"]


def test_a_closed_po_among_several_is_named_rather_than_the_primary(db):
    ext = invoice(1800.00, ["PO-1001", "PO-1004"])      # PO-1004 is closed
    _, reasons, _ = decide(matching.match_po(ext), ext)
    closed = [r["text"] for r in reasons if "closed" in r["text"]]
    assert closed and "PO-1004" in closed[0]


def test_a_currency_mismatch_on_any_referenced_po_is_flagged(db):
    """The combined balance was summed as bare numbers, so one PO in another
    currency invalidates the whole comparison, not just its own share."""
    conn = storage.get_conn()
    conn.execute("UPDATE purchase_orders SET currency='EUR' WHERE po_number='PO-1002'")
    conn.commit()
    conn.close()

    pm = matching.match_po(invoice(6240.00, ["PO-1001", "PO-1002"]))
    assert pm["currency_mismatch"] is True


# --------------------------------------------------------------------------
# the ledger, end to end
# --------------------------------------------------------------------------

def test_the_stored_allocations_are_what_the_matcher_proposed(db):
    ext = invoice(3000.00, ["PO-1001", "PO-1002"])
    pm = matching.match_po(ext)
    status, reasons, _ = decide(pm, ext)
    run_id, _ = commit(ext, pm, status, reasons)

    stored = storage.allocations_for_run(run_id)
    assert [(a["po_number"], a["amount"]) for a in stored] == [
        ("PO-1001", 1240.0), ("PO-1002", 1760.0)]


def test_a_held_multi_po_run_consumes_nothing_until_it_is_accepted(db):
    ext = invoice(6240.00, ["PO-1001", "PO-1002"])
    pm = matching.match_po(ext)
    status, reasons, _ = decide(pm, ext)
    run_id, final = commit(ext, pm, status, reasons)

    assert final == "NEEDS_REVIEW"
    assert storage.remaining_for_po("PO-1001") == 1240.0
    assert storage.remaining_for_po("PO-1002") == 5000.0


def test_a_run_is_queued_against_every_po_it_charges_not_just_the_primary(db):
    """Budget freed on a secondary PO is still relevant to this invoice."""
    ext = invoice(6240.00, ["PO-1001", "PO-1002"])
    pm = matching.match_po(ext)
    status, reasons, _ = decide(pm, ext)
    run_id, _ = commit(ext, pm, status, reasons)

    queued = [r["id"] for r in storage.runs_pending_on_po("PO-1002")]
    assert run_id in queued


def test_freeing_budget_never_auto_approves_a_multi_po_invoice(db):
    """The cascade releases invoices held on a short BALANCE. A multi-PO invoice
    is held on the unstated SPLIT, which freeing budget does nothing about --
    otherwise a reversal elsewhere could commit a split nobody confirmed."""
    ext = invoice(6240.00, ["PO-1001", "PO-1002"])
    pm = matching.match_po(ext)
    status, reasons, _ = decide(pm, ext)
    run_id, _ = commit(ext, pm, status, reasons)

    changed = rules.reevaluate_po_queue("PO-1002")

    assert changed == []
    assert storage.get_run(run_id)["status"] == "NEEDS_REVIEW"


def test_a_raced_secondary_po_holds_the_whole_invoice(db):
    """The commit-time re-check covers every PO, not only the primary. A split
    is a package -- committing part of it would charge a PO for an invoice that
    was not approved."""
    ext = invoice(6240.00, ["PO-1001", "PO-1002"])
    pm = matching.match_po(ext)

    # Someone else consumes PO-1002 after this invoice computed its verdict.
    other = invoice(5000.00, ["PO-1002"], vendor=GLOBEX, number="INV-RACE")
    commit(other, matching.match_po(other), "APPROVED", [], filename="race.pdf")

    run_id, final = commit(ext, pm, "APPROVED", [])

    assert final == "NEEDS_REVIEW"
    assert storage.remaining_for_po("PO-1001") == 1240.0


def test_the_audit_trail_carries_the_split_and_says_it_was_calculated(db):
    ext = invoice(3000.00, ["PO-1001", "PO-1002"])
    _, _, audit = decide(matching.match_po(ext), ext)

    assert audit["allocation_basis"] == "calculated"
    assert [(a["po_number"], a["amount"]) for a in audit["allocations"]] == [
        ("PO-1001", 1240.0), ("PO-1002", 1760.0)]
    assert audit["purchase_order"]["po_numbers"] == ["PO-1001", "PO-1002"]
    assert audit["purchase_order"]["is_multi"] is True


def test_a_single_po_audit_trail_reports_one_allocation_of_the_full_total(db):
    """An auditor reads the same field either way."""
    ext = invoice(1240.00, ["PO-1001"])
    _, _, audit = decide(matching.match_po(ext), ext)

    assert audit["allocation_basis"] == "single_po"
    assert [(a["po_number"], a["amount"]) for a in audit["allocations"]] == [
        ("PO-1001", 1240.0)]
    assert audit["purchase_order"]["is_multi"] is False


def test_three_purchase_orders_on_one_invoice(db):
    ext = invoice(14440.00, ["PO-1001", "PO-1002", "PO-1003"])
    pm = matching.match_po(ext)
    assert [a["amount"] for a in pm["allocations"]] == [1240.0, 5000.0, 8200.0]
    assert pm["remaining_before"] == 14440.0

    status, reasons, _ = decide(pm, ext)
    run_id, _ = commit(ext, pm, status, reasons)
    storage.set_run_status(run_id, "APPROVED", "confirmed")

    for po in ("PO-1001", "PO-1002", "PO-1003"):
        assert storage.remaining_for_po(po) == 0.0
