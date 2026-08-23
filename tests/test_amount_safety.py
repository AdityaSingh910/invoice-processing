"""Amounts a document can state but this application cannot record.

TWO DEFECTS, BOTH FOUND BY PROBING RATHER THAN BY READING

1. NON-FINITE. Python's json.loads accepts the bare literals `Infinity`,
   `-Infinity` and `NaN` as an extension to the JSON spec, so a model emitting
   one handed us a float that flowed all the way into runs.total. The verdict
   was still safe -- every comparison against inf or NaN is False, so the
   tolerance check held the run -- but by accident rather than by decision, and
   the STORED ROW was then unserialisable: FastAPI refuses to encode a
   non-finite float, so one such run returned 500 from /api/runs,
   /api/runs/{id} and /api/analytics/overview for every user until it was
   deleted. The invoice was correctly held; the record of holding it took the
   register down.

2. UNRECORDABLE. There was no upper bound anywhere, and the money columns are
   PostgreSQL `real`, which tops out near 3.4e38. A 46-digit total parsed
   cleanly to 1e45, was correctly held for review, and then raised
   NumericValueOutOfRange on INSERT -- so no run existed, no audit trail was
   written, and the uploader was shown a raw database error naming the column
   type.

THE FIX IS AT THE BOUNDARY, NOT IN THE LEDGER

extraction._usable_amount refuses both where a document becomes data, so
neither value reaches a decision or a column. rules.validate_amount is the
second layer, for a total arriving by some other door, and names the condition
instead of letting it reach a column that will reject it. Nothing is clamped:
turning 1e45 into a storable number would invent an amount the document never
stated.
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

import config       # noqa: E402
import extraction   # noqa: E402
import matching     # noqa: E402
import rules        # noqa: E402
import storage      # noqa: E402
import pg_schema    # noqa: E402

INF = float("inf")
NAN = float("nan")
VENDOR = "Acme Office Supplies"


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    yield schema
    pg_schema.drop_schema(schema)


def invoice(total, **kw):
    d = {"vendor_name": VENDOR, "invoice_number": "AMT-1", "total": total,
         "currency": "USD", "po_references": ["PO-1001"], "line_items": []}
    d.update(kw)
    return d


# --------------------------------------------------------------------------
# 1. the boundary refuses what cannot be recorded
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [INF, -INF, NAN], ids=["inf", "-inf", "nan"])
def test_a_non_finite_number_is_not_an_amount(value):
    assert extraction._usable_amount(value) is None


@pytest.mark.parametrize("value", [1e45, -1e45, 1e39, 3.4e38])
def test_a_number_too_large_to_store_is_not_an_amount(value):
    assert extraction._usable_amount(value) is None


@pytest.mark.parametrize("value", [0.0, 0.01, 1240.0, 500000.0, 1e12, 1e15])
def test_ordinary_amounts_are_untouched(value):
    """The guard must be invisible to every amount a real invoice states."""
    assert extraction._usable_amount(value) == value


def test_a_bool_is_not_an_amount():
    """isinstance(True, int) is True, so this needs excluding explicitly."""
    assert extraction._usable_amount(True) is None


def test_the_printed_amount_parser_refuses_the_same_values():
    """_to_float is the regex route's door and needs the same guard."""
    assert extraction._to_float("1" + "0" * 45) is None      # 46 digits -> 1e45
    assert extraction._to_float("{:,}".format(10 ** 45)) is None
    assert extraction._to_float("Infinity") is None
    assert extraction._to_float("NaN") is None
    # ...without disturbing anything a document really says.
    assert extraction._to_float("1,234.56") == 1234.56
    assert extraction._to_float("500000.00") == 500000.00


def test_the_ceiling_is_configurable_and_read_at_call_time(monkeypatch):
    monkeypatch.setenv("MAX_INVOICE_TOTAL", "1000")
    assert extraction._usable_amount(999.0) == 999.0
    assert extraction._usable_amount(1001.0) is None
    monkeypatch.setenv("MAX_INVOICE_TOTAL", "not a number")
    assert config.max_invoice_total() == 1e15


# --------------------------------------------------------------------------
# 2. the LLM route -- where the non-finite value actually came from
# --------------------------------------------------------------------------

def test_python_json_really_does_accept_the_infinity_literal():
    """The premise. If this ever stops being true the guard is still correct,
    but the reason it exists would have changed."""
    assert json.loads('{"total": Infinity}')["total"] == INF


def test_a_model_emitting_infinity_produces_no_total_rather_than_an_infinite_one():
    payload = {"vendor_name": VENDOR, "invoice_number": "INF-1", "total": INF,
               "line_items": [{"description": "Widget", "quantity": INF,
                               "unit_price": 1.0, "amount": INF}]}
    inv = extraction._invoice_from_payload(payload, "raw", "groq-text")
    d = inv.to_dict()
    assert d["total"] is None
    assert d["line_items"][0]["quantity"] is None
    assert d["line_items"][0]["amount"] is None
    # The one finite value on that line is kept -- only the bad ones are dropped.
    assert d["line_items"][0]["unit_price"] == 1.0


# --------------------------------------------------------------------------
# 3. the second layer: a total arriving by some other door is NAMED
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [INF, -INF, NAN], ids=["inf", "-inf", "nan"])
def test_validate_amount_names_a_non_finite_total(value):
    out = rules.validate_amount(invoice(value))
    assert out["kind"] == "non_finite"


def test_validate_amount_names_an_unrecordable_total():
    assert rules.validate_amount(invoice(1e45))["kind"] == "unrecordable"


@pytest.mark.parametrize("value,kind", [(-5.0, "negative"), (0.0, "zero")])
def test_the_existing_amount_findings_are_unchanged(value, kind):
    assert rules.validate_amount(invoice(value))["kind"] == kind


@pytest.mark.parametrize("value", [0.01, 1240.0, 500000.0, 1e12])
def test_a_normal_total_still_produces_no_finding(value):
    assert rules.validate_amount(invoice(value)) is None


@pytest.mark.parametrize("value,phrase", [(INF, "not a number"),
                                          (1e45, "larger than this application can record")])
def test_the_reviewer_is_told_what_is_actually_wrong(db, value, phrase):
    """Not "exceeds the PO balance", which is what used to be reported."""
    ext = invoice(value)
    audit = {}
    status, reasons = rules.decide(
        {"route": "groq-text", "notes": [], "security_flags": []},
        [], True, "ok", None, "no dup", matching.match_po(ext),
        amount=rules.validate_amount(ext), audit=audit, extracted=ext)
    assert status == "NEEDS_REVIEW"
    assert "Invoice amount valid" in audit["rules_failed"]
    assert any(phrase in r["text"] for r in reasons if r["level"] == "fail")


def test_an_infinite_quantity_is_not_silently_ignored_by_the_line_item_rule():
    """abs(inf*p - inf) is NaN and every NaN comparison is False, so without an
    explicit guard the line produces no finding at all."""
    assert rules._num(INF) is None
    assert rules._num(-INF) is None
    assert rules._num(NAN) is None
    assert rules._num(8.0) == 8.0


# --------------------------------------------------------------------------
# 4. end to end -- the register must stay up
# --------------------------------------------------------------------------

def test_a_document_stating_a_46_digit_total_is_held_and_recorded(db):
    """The realistic path: extraction refuses the figure, so the run commits.

    Before the fix this reached INSERT and raised NumericValueOutOfRange, so no
    run existed at all and the uploader got a raw database error.
    """
    text = ("Acme Office Supplies\nINVOICE\nInvoice #: BIG-1\n"
            "Invoice Date: 2026-08-01\nPO Number: PO-1001\n"
            "Total Due: $" + "1" + "0" * 45 + "\n")
    inv = extraction.regex_extract(text)
    assert inv.total is None, "the unrecordable figure must not become a total"

    ext = inv.to_dict()
    pm = matching.match_po(ext)
    audit = {}
    status, reasons = rules.decide(
        {"route": "regex", "notes": [], "security_flags": []},
        rules.validate_required_fields(ext), True, "ok", None, "no dup", pm,
        amount=rules.validate_amount(ext), audit=audit, extracted=ext)
    assert status == "NEEDS_REVIEW"

    run_id, final, _ = storage.save_run_checked(
        "big.pdf", status, ext, pm, [], reasons,
        tolerance_for=matching.tolerance_for, audit=audit)
    assert run_id is not None, "the run must be recorded, not lost to a type error"
    assert storage.get_run(run_id)["total"] is None


def test_an_infinite_total_no_longer_reaches_the_database(db):
    payload = {"vendor_name": VENDOR, "invoice_number": "INF-2", "total": INF,
               "po_references": ["PO-1001"], "line_items": []}
    ext = extraction._invoice_from_payload(payload, "raw", "groq-text").to_dict()
    pm = matching.match_po(ext)
    audit = {}
    status, reasons = rules.decide(
        {"route": "groq-text", "notes": [], "security_flags": []},
        rules.validate_required_fields(ext), True, "ok", None, "no dup", pm,
        amount=rules.validate_amount(ext), audit=audit, extracted=ext)
    run_id, _, _ = storage.save_run_checked(
        "inf.pdf", status, ext, pm, [], reasons,
        tolerance_for=matching.tolerance_for, audit=audit)

    stored = storage.get_run(run_id)["total"]
    assert stored is None or (stored == stored and abs(stored) != INF), (
        "a non-finite total in runs.total makes every listing endpoint 500"
    )


def test_the_run_register_stays_serialisable(db):
    """The actual failure: one bad row 500s /api/runs for everyone.

    Asserted the way it broke -- by encoding the response, which is what
    FastAPI does and what raised ValueError before the fix.
    """
    from fastapi.encoders import jsonable_encoder

    for payload_total in (INF, -INF, NAN, 1e45):
        ext = extraction._invoice_from_payload(
            {"vendor_name": VENDOR, "invoice_number": "SER-1",
             "total": payload_total, "po_references": ["PO-1001"],
             "line_items": []}, "raw", "groq-text").to_dict()
        pm = matching.match_po(ext)
        audit = {}
        status, reasons = rules.decide(
            {"route": "groq-text", "notes": [], "security_flags": []},
            rules.validate_required_fields(ext), True, "ok", None, "no dup", pm,
            amount=rules.validate_amount(ext), audit=audit, extracted=ext)
        storage.save_run_checked("ser.pdf", status, ext, pm, [], reasons,
                                 tolerance_for=matching.tolerance_for, audit=audit)

    runs = storage.list_runs()
    assert len(runs) == 4
    json.dumps(jsonable_encoder(runs), allow_nan=False)   # raises if any is non-finite
