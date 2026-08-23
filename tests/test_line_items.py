"""Line-item agreement between an invoice and the purchase order it names.

THE HOLE THIS CLOSES

Every money check that existed before compares ONE number -- the invoice total
-- against what the PO authorises. That is blind by construction to a
rearrangement underneath a correct total:

    PO-EDGE-001   10 laptops @ 50,000  =  500,000
    INV-EDGE-001   8 laptops @ 62,500  =  500,000

Same money. Two fewer machines, 25% more each. `PO remaining check` reports a
variance of exactly zero and passes, because from where it stands nothing is
wrong. The rule under test looks below the total instead.

WHAT IT MUST NOT DO, WHICH IS MOST OF THIS FILE

The dangerous version of this feature is one that starts holding invoices it
has no business holding. Most POs in this application state a total and nothing
else, and the regex extraction route frequently reads no line items at all --
so "there was nothing to compare" is the NORMAL case, and it must pass. The
tests below pin that from four directions (no PO items, no invoice items,
malformed PO JSON, and a partial delivery) because a regression there would be
invisible in the happy path and would hold nearly every invoice this system
currently approves.

It also holds and never rejects. A quantity that differs is very often
legitimate -- a short shipment, a substituted part, a price varied by email
after the PO was raised. `test_a_line_item_mismatch_never_rejects` pins that.
"""
import json
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

import extraction   # noqa: E402
import matching     # noqa: E402
import rules        # noqa: E402
import storage      # noqa: E402
import pg_schema    # noqa: E402

SAMPLES = os.path.join(ROOT, "sample_invoices")

VENDOR = "Acme Technologies"
EDGE_PO = "PO-EDGE-001"          # 10 x Laptop @ 50,000 INR = 500,000 INR
ACME = "Acme Office Supplies"    # PO-1001, $1,240, NO line items on the PO


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    yield schema
    pg_schema.drop_schema(schema)


def invoice(total, refs, line_items, vendor=VENDOR, number="INV-EDGE-001",
            currency="INR"):
    return {"vendor_name": vendor, "invoice_number": number, "total": total,
            "currency": currency, "po_references": list(refs),
            "line_items": list(line_items)}


def line(description, quantity, unit_price, amount):
    return {"description": description, "quantity": quantity,
            "unit_price": unit_price, "amount": amount}


def decide(extracted, po_match=None, **kw):
    """Run the real decision with everything else passing.

    Vendor approved, no duplicate, no missing fields -- so whatever verdict comes
    back is attributable to the rule under test and nothing else.
    """
    pm = po_match if po_match is not None else matching.match_po(extracted)
    audit = {}
    kw.setdefault("dup_row", None)
    status, reasons = rules.decide(
        {"route": "groq-text"}, [], True, "Vendor approved",
        kw["dup_row"], "No prior run", pm, audit=audit, extracted=extracted)
    return status, reasons, audit


def seed_po(po_number, vendor, amount, line_items, currency="INR"):
    """Write a PO with its own line items, for cases the shipped seed lacks.

    Raw SQL because there is no public writer for purchase_orders -- the table is
    reloaded from data/purchase_orders.json on every startup and has no runtime
    writer by design (CLAUDE.md section 4). A test needing a shape the seed file
    does not carry writes it directly rather than adding a permanent demo PO.
    """
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO purchase_orders
                   (po_number, vendor, amount, currency, issued_date, status,
                    description, source_file, source_row, line_items_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (po_number, vendor, amount, currency, "2026-07-01", "open",
                 "test fixture", "test", 1,
                 json.dumps(line_items) if line_items is not None else None),
            )
        conn.commit()
    finally:
        conn.close()


def failed(audit):
    return audit.get("rules_failed") or []


# --------------------------------------------------------------------------
# 1. the case that motivated the rule
# --------------------------------------------------------------------------

def test_same_total_but_different_quantity_and_price_is_held(db):
    """The headline scenario: 8 @ 62,500 billed against a PO for 10 @ 50,000.

    The totals agree to the cent, so every check above this one passes. This is
    the whole reason a total-only comparison is not enough.
    """
    ext = invoice(500000.00, [EDGE_PO], [line("Laptop", 8, 62500.00, 500000.00)])
    pm = matching.match_po(ext)

    # The premise: the balance check really does see nothing wrong.
    assert pm["po_number"] == EDGE_PO
    assert pm["within_tolerance"] is True
    assert pm["diff"] == 0.0

    status, reasons, audit = decide(ext, pm)
    assert status == "NEEDS_REVIEW"
    assert failed(audit) == [rules.LINE_ITEM_RULE], (
        "the line-item rule must be the ONLY thing holding this invoice"
    )
    assert any("Line items do not agree" in r["text"] for r in reasons
               if r["level"] == "fail")


def test_quantity_alone_differing_is_held(db):
    """Only the count changed -- 8 units, still at the agreed 50,000."""
    ext = invoice(400000.00, [EDGE_PO], [line("Laptop", 8, 50000.00, 400000.00)])
    li = rules.line_item_check(ext, matching.match_po(ext))
    kinds = {f["kind"] for f in li["findings"]}
    assert kinds == {"quantity", "po_line_total"}, kinds

    status, _, audit = decide(ext)
    assert status == "NEEDS_REVIEW"
    assert rules.LINE_ITEM_RULE in failed(audit)


def test_unit_price_alone_differing_is_held(db):
    """Only the price changed -- all 10 units, at 62,500 instead of 50,000.

    Note this one ALSO exceeds the PO balance, so two rules fire. That is
    correct and is asserted rather than avoided: the point is that the
    line-item rule is among them, not that it is alone.
    """
    ext = invoice(625000.00, [EDGE_PO], [line("Laptop", 10, 62500.00, 625000.00)])
    li = rules.line_item_check(ext, matching.match_po(ext))
    assert "unit_price" in {f["kind"] for f in li["findings"]}

    status, _, audit = decide(ext)
    assert status == "NEEDS_REVIEW"
    assert rules.LINE_ITEM_RULE in failed(audit)


def test_a_line_that_does_not_multiply_out_is_held(db):
    """quantity x unit_price must equal the line's own amount.

    Independent of the PO: a line that does not multiply out is wrong whoever
    ordered it. 10 x 50,000 is 500,000, not 450,000.
    """
    ext = invoice(450000.00, [EDGE_PO], [line("Laptop", 10, 50000.00, 450000.00)])
    li = rules.line_item_check(ext, matching.match_po(ext))
    assert "line_total" in {f["kind"] for f in li["findings"]}

    status, _, audit = decide(ext)
    assert status == "NEEDS_REVIEW"
    assert rules.LINE_ITEM_RULE in failed(audit)


def test_one_wrong_line_among_several_is_held(db):
    """Three items, two correct, one short-delivered. The one must be found."""
    seed_po("PO-EDGE-002", VENDOR, 100000.00, [
        {"description": "Laptop", "quantity": 1, "unit_price": 50000.00, "amount": 50000.00},
        {"description": "Docking station", "quantity": 4, "unit_price": 10000.00, "amount": 40000.00},
        {"description": "Carry case", "quantity": 10, "unit_price": 1000.00, "amount": 10000.00},
    ])
    ext = invoice(100000.00, ["PO-EDGE-002"], [
        line("Laptop", 1, 50000.00, 50000.00),            # correct
        line("Docking station", 2, 20000.00, 40000.00),   # HALF the units, double the price
        line("Carry case", 10, 1000.00, 10000.00),        # correct
    ])
    li = rules.line_item_check(ext, matching.match_po(ext))

    assert li["compared"] == 3
    offenders = {f["kind"] for f in li["findings"]}
    assert offenders == {"quantity", "unit_price"}, offenders
    assert all("Docking station" in f["item"] for f in li["findings"]), (
        "only the docking-station line is wrong; the other two must not be reported"
    )

    status, _, audit = decide(ext)
    assert status == "NEEDS_REVIEW"
    assert rules.LINE_ITEM_RULE in failed(audit)


def test_an_item_the_po_never_ordered_is_held(db):
    """A line billed against a PO that does not list it."""
    ext = invoice(500000.00, [EDGE_PO], [
        line("Laptop", 9, 50000.00, 450000.00),
        line("Extended warranty", 1, 50000.00, 50000.00),
    ])
    li = rules.line_item_check(ext, matching.match_po(ext))
    unknown = [f for f in li["findings"] if f["kind"] == "unknown_item"]
    assert len(unknown) == 1
    assert "Extended warranty" in unknown[0]["item"]


# --------------------------------------------------------------------------
# 2. the invoice that is fine must stay fine
# --------------------------------------------------------------------------

def test_a_matching_invoice_is_approved(db):
    """Quantity, unit price and line total all agree -> APPROVED, as before."""
    ext = invoice(500000.00, [EDGE_PO], [line("Laptop", 10, 50000.00, 500000.00)])
    li = rules.line_item_check(ext, matching.match_po(ext))
    assert li["findings"] == []
    assert li["applicable"] is True
    assert li["compared"] == 1

    status, _, audit = decide(ext)
    assert status == "APPROVED"
    assert failed(audit) == []


def test_the_item_name_is_compared_loosely_enough_to_be_useful(db):
    """Punctuation and case must not read as a different product."""
    ext = invoice(500000.00, [EDGE_PO], [line("  LAPTOP.  ", 10, 50000.00, 500000.00)])
    li = rules.line_item_check(ext, matching.match_po(ext))
    assert li["compared"] == 1
    assert li["findings"] == [], "casing and punctuation are not a discrepancy"


def test_billing_fewer_of_the_ordered_lines_is_not_a_finding(db):
    """A PO line the invoice does not bill is a partial invoice, not a fault.

    Tolerance is one-sided everywhere else in this system for the same reason.
    """
    seed_po("PO-EDGE-003", VENDOR, 100000.00, [
        {"description": "Laptop", "quantity": 1, "unit_price": 50000.00, "amount": 50000.00},
        {"description": "Monitor", "quantity": 1, "unit_price": 50000.00, "amount": 50000.00},
    ])
    ext = invoice(50000.00, ["PO-EDGE-003"], [line("Laptop", 1, 50000.00, 50000.00)])
    li = rules.line_item_check(ext, matching.match_po(ext))
    assert li["findings"] == [], "the unbilled Monitor line must not be reported"

    status, _, audit = decide(ext)
    assert status == "APPROVED"


# --------------------------------------------------------------------------
# 3. absence is not a fault -- the property that protects every existing invoice
# --------------------------------------------------------------------------

def test_a_po_with_no_line_items_skips_the_check(db):
    """The shipped POs state a total and nothing else. They must still approve."""
    ext = invoice(1240.00, ["PO-1001"], [line("Paper reams", 10, 124.00, 1240.00)],
                  vendor=ACME, number="INV-NOITEMS", currency="USD")
    li = rules.line_item_check(ext, matching.match_po(ext))
    assert li["applicable"] is False
    assert li["findings"] == []
    assert "no purchase order on file states line items" in li["skipped_because"]

    status, _, audit = decide(ext)
    assert status == "APPROVED"
    assert failed(audit) == []


def test_an_invoice_with_no_line_items_skips_the_check(db):
    """The regex extraction route often reads none. That is not a discrepancy."""
    ext = invoice(500000.00, [EDGE_PO], [])
    li = rules.line_item_check(ext, matching.match_po(ext))
    assert li["applicable"] is False
    assert "the invoice states no line items" in li["skipped_because"]

    status, _, audit = decide(ext)
    assert status == "APPROVED"


def test_a_malformed_po_line_item_blob_skips_the_check(db):
    """A bad JSON blob must not hold every invoice against that PO.

    Same guarded-parse discipline analytics._loads applies to runs.audit_json:
    the column is TEXT, so one bad row must not take a decision down -- and it
    must not read as an empty itemisation either, which would make every billed
    line look like an item nobody ordered.
    """
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE purchase_orders SET line_items_json = %s WHERE po_number = %s",
                        ("{not json at all", EDGE_PO))
        conn.commit()
    finally:
        conn.close()

    ext = invoice(500000.00, [EDGE_PO], [line("Laptop", 8, 62500.00, 500000.00)])
    li = rules.line_item_check(ext, matching.match_po(ext))
    assert li["findings"] == []
    assert li["applicable"] is False

    status, _, audit = decide(ext)
    assert status == "APPROVED", "a bad blob must fail open, not hold the invoice"


def test_a_line_missing_a_number_is_not_compared_on_that_number(db):
    """A null quantity is unknown, not zero. Nothing may be inferred from it."""
    ext = invoice(500000.00, [EDGE_PO], [line("Laptop", None, None, 500000.00)])
    li = rules.line_item_check(ext, matching.match_po(ext))
    assert li["findings"] == [], "absent numbers cannot disagree with anything"


def test_the_check_never_raises_on_hostile_shapes(db):
    """A guard that crashes the pipeline is worse than one that finds nothing."""
    pm = matching.match_po(invoice(500000.00, [EDGE_PO], []))
    for items in ([{"description": None, "quantity": "eight"}],
                  [{"quantity": float("nan"), "unit_price": 1, "amount": 1}],
                  [{"description": "Laptop", "quantity": True, "unit_price": True,
                    "amount": True}],
                  ["not a dict", 42, None],
                  [{}]):
        out = rules.line_item_check({"line_items": items}, pm)
        assert isinstance(out["findings"], list)


# --------------------------------------------------------------------------
# 4. it holds; it does not reject, and it does not outrank a rejection
# --------------------------------------------------------------------------

def test_a_line_item_mismatch_never_rejects(db):
    """A short delivery at a revised price is a conversation, not fraud."""
    ext = invoice(500000.00, [EDGE_PO], [line("Laptop", 8, 62500.00, 500000.00)])
    status, _, _ = decide(ext)
    assert status == "NEEDS_REVIEW"
    assert status != "REJECTED"


def test_a_duplicate_still_outranks_a_line_item_mismatch(db):
    """Reject wins over review, exactly as it did before this rule existed."""
    ext = invoice(500000.00, [EDGE_PO], [line("Laptop", 8, 62500.00, 500000.00)])
    status, _, audit = decide(ext, dup_row={"id": 7})
    assert status == "REJECTED"
    assert rules.LINE_ITEM_RULE in failed(audit), (
        "the finding is still recorded even when a rejection outranks it"
    )


def test_the_rule_is_registered_in_the_audit_vocabulary(db):
    """`rules_failed` is a fixed vocabulary the portal and analytics read.

    A rule with no entry in the two lookup tables produces a hold that nothing
    downstream can explain or group by.
    """
    assert rules.LINE_ITEM_RULE in rules._SUGGESTED_RESOLUTIONS
    assert rules.LINE_ITEM_RULE in rules._RULE_FIELDS


# --------------------------------------------------------------------------
# 5. the shipped sample, through the real extraction pipeline
# --------------------------------------------------------------------------

def test_the_shipped_sample_pdf_is_held_for_review(db):
    """PDF bytes -> extraction -> matching -> verdict, on the committed fixture.

    Runs on whichever route is configured. The assertions are about the verdict
    and the reason, not about which model read the document.
    """
    path = os.path.join(SAMPLES, "11_line_item_mismatch_acme_tech.pdf")
    with open(path, "rb") as fh:
        inv, info = extraction.extract_invoice(fh.read())
    ext = inv.to_dict()

    if not ext.get("line_items"):
        pytest.skip("this route read no line items; covered by the unit cases above")

    pm = matching.match_po(ext)
    assert pm["po_number"] == EDGE_PO
    assert pm["within_tolerance"] is True, "the premise: the total is not the problem"

    audit = {}
    status, reasons = rules.decide(
        info, rules.validate_required_fields(ext), True, "Vendor approved",
        None, "No prior run", pm, audit=audit, extracted=ext)

    assert status == "NEEDS_REVIEW"
    assert failed(audit) == [rules.LINE_ITEM_RULE]
    assert any("62,500" in r["text"] for r in reasons if r["level"] == "fail"), (
        "the reviewer must be told the actual prices, not just that something differs"
    )
