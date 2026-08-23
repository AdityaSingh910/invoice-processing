"""End-to-end regression suite: the 7 sample invoices, in manifest order.

WHY THIS SHAPE

The pipeline's headline behaviours are *history-dependent*. A PO's remaining
balance is derived from prior APPROVED runs, and a duplicate is only a duplicate
because something came before it. So these cases cannot be tested in isolation:
the same `03b_split_po_globex_overflow.pdf` bytes are APPROVED against a fresh
PO-1002 and NEEDS_REVIEW once two earlier invoices have drained it. That is the
design, not a quirk, and the suite has to honour it.

Three consequences:

1. The samples run **sequentially, in manifest order**, sharing one database.
   `pytest.mark.parametrize` preserves the order it is given.
2. The database is **isolated per module** — `storage.PG_SCHEMA` is
   monkeypatched to a fresh, uniquely-named schema before `init_db()`. Running
   against the real application schema would fail immediately, since it
   already carries runs from manual testing.
3. Expected verdicts are read from `sample_invoices/manifest.json`, the same
   file that labels the samples in the UI. One source of truth, so the tests and
   the interface cannot drift apart.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
SAMPLES = os.path.join(ROOT, "sample_invoices")

# The backend is a flat package of top-level modules (`import storage`, etc.),
# matching how main.py puts its own directory on the path at import time.
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import config      # noqa: E402
import main        # noqa: E402
import storage     # noqa: E402
import pg_schema   # noqa: E402


# --------------------------------------------------------------------------
# manifest -- the single source of truth for what each sample should produce
# --------------------------------------------------------------------------

def load_manifest(vision_available):
    """[(filename, expected_status), ...] in the manifest's declared order.

    One expectation is **route-dependent**, and it has to be, because the verdict
    genuinely differs: `05_scanned_no_text.pdf` is an image-only PDF. With no API
    key there is nothing to read, so the process refuses to guess and the invoice
    goes to review. With a key, the vision route reads INV-9004 / PO-1005 /
    $15,400.00 off the page image, which matches open PO-1005 exactly and
    approves. Same bytes, different verdict, because a different route ran.

    Samples carrying an `expect_with_vision` key use it when a key is live. The
    manifest stays the single source of truth for both modes.
    """
    with open(os.path.join(SAMPLES, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    ordered = sorted(manifest.items(), key=lambda kv: kv[1].get("order", 999))

    out = []
    for name, meta in ordered:
        expected = meta["expect"]
        if vision_available and meta.get("expect_with_vision"):
            expected = meta["expect_with_vision"]
        out.append((name, expected))
    return out


# Resolved at import time so parametrize ids reflect the mode actually running.
# config.load_dotenv() is what picks up a key from .env; the real environment wins.
config.load_dotenv()
LIVE_LLM = config.has_api_key()      # Gemini -> the vision route
LIVE_GROQ = config.has_groq_key()    # Groq   -> the LLM text route
MANIFEST = load_manifest(LIVE_LLM)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """A TestClient wired to a throwaway Postgres schema seeded from data/*.json.

    Module-scoped because the schema has to outlive a single test -- the whole
    point is that state accumulates across the ten samples.

    `storage.PG_SCHEMA` is a module-level constant read inside `get_conn()` at
    call time, with no environment override, so patching the attribute is the
    only way to redirect it (see tests/pg_schema.py -- the same mechanism
    every other test file's `db` fixture uses, just module-scoped here instead
    of per-test).

    `GEMINI_API_KEY` and `GROQ_API_KEY` are deliberately **not** stripped. When a
    key is present the suite exercises the real `groq (text)` / `gemini (vision)`
    routes end to end, which is what makes it deployment/demo readiness rather
    than a mock. Route-level behaviour is covered deterministically with mocks in
    tests/test_extraction_routing.py; this file is the live counterpart.
    The tradeoff is real and worth stating: in that mode the suite costs money,
    needs a network, and is only as reproducible as the model is. The verdicts
    should still be stable, because the decision logic downstream of extraction
    is deterministic -- but an extraction miss will surface here as a failure.
    """
    # A green suite means something different in each mode, so say which one ran
    # rather than leaving a reader to infer it from the absence of a key.
    print("\n[extraction mode] text route : %s" %
          ("LIVE groq (text) - %s" % config.groq_model() if LIVE_GROQ
           else "regex fallback - no GROQ_API_KEY"))
    print("[extraction mode] scan route : %s" %
          ("LIVE gemini (vision) - %s" % config.EXTRACTION_MODEL if LIVE_LLM
           else "route 'none' - no GEMINI_API_KEY"))

    mp = pytest.MonkeyPatch()
    schema = pg_schema.fresh_schema(mp)   # creates the schema + seeds reference data

    assert storage.list_purchase_orders(), "seed POs did not load into the test DB"
    assert storage.list_vendors(), "seed vendors did not load into the test DB"
    assert storage.list_runs() == [], "test DB should start with no run history"

    # The context manager fires FastAPI's startup event, which calls init_db()
    # again -- harmless, and it proves startup works against the patched schema.
    # The API now requires a bearer token on every invoice endpoint, so the
    # test client authenticates like any other caller. `admin` carries every
    # scope, which keeps this fixture about samples rather than about
    # permissions -- those are tested directly in tests/test_api_security.py.
    from conftest import auth_headers
    from fastapi.testclient import TestClient
    with TestClient(main.app, headers=auth_headers("admin")) as c:
        assert storage.PG_SCHEMA == schema, "startup must not restore the real schema"
        yield c

    mp.undo()
    pg_schema.drop_schema(schema)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def run_invoice(client, filename):
    """POST a sample through the live SSE endpoint and return the final result.

    The response is a stream of `data: {...}` lines, one per stage, terminated by
    a `final` event carrying the verdict. Reading the last `final` rather than
    the last line matters -- the stream ends with a blank line, so tailing it
    yields nothing.
    """
    path = os.path.join(SAMPLES, filename)
    assert os.path.isfile(path), f"missing sample fixture: {filename}"

    with open(path, "rb") as fh:
        resp = client.post(
            "/api/runs/stream",
            files={"file": (filename, fh, "application/pdf")},
        )
    assert resp.status_code == 200, f"{filename}: HTTP {resp.status_code}"

    final, stage_events = None, []
    for line in resp.text.splitlines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[len("data: "):])
        if event.get("type") == "stage":
            stage_events.append(event["stage"])
        elif event.get("type") == "final":
            final = event["result"]

    assert final is not None, f"{filename}: stream produced no final event"
    assert stage_events, f"{filename}: stream produced no stage events"
    return final


def po_balances(result):
    """(remaining_before, remaining_after) for the matched PO, or (None, None)."""
    match = result.get("po_match") or {}
    return match.get("remaining_before"), match.get("remaining_after")


# --------------------------------------------------------------------------
# per-sample scenario checks
#
# The verdict alone would pass even if the numbers behind it were wrong, so each
# sample that has something specific worth pinning gets an extra assertion. These
# live here rather than in separate test functions to keep the suite at exactly
# one test per sample.
# --------------------------------------------------------------------------

def _check_happy_path(result):
    assert result["po_match"]["po_number"] == "PO-1001"
    assert result["po_match"]["matched_via"] == "explicit"
    assert result["extracted"]["invoice_number"] == "INV-2201"


def _check_split_a(result):
    # First bill against an untouched $5,000 PO.
    before, after = po_balances(result)
    assert (before, after) == (5000.00, 2000.00)
    assert result["po_match"]["is_partial"] is True


def _check_split_b(result):
    # Second bill lands exactly on the remaining balance, closing it out.
    before, after = po_balances(result)
    assert (before, after) == (2000.00, 0.00)


def _check_overflow(result):
    # Third bill against a drained PO: the ledger, not the file, is what flags it.
    before, _ = po_balances(result)
    assert before == 0.00
    assert result["po_match"]["within_tolerance"] is False


def _check_missing_number(result):
    assert not result["extracted"].get("invoice_number")
    assert any("invoice_number" in r["text"] for r in result["reasons"]), \
        "the missing field should be named in the reasoning trail"


def _check_scanned(result):
    """The one sample whose behaviour is route-dependent -- see load_manifest()."""
    if LIVE_LLM:
        # Vision route: pages rasterised by pypdfium2 and read by the model.
        assert result["extracted"]["extraction_method"] == "gemini (vision)"
        assert result["extracted"]["invoice_number"] == "INV-9004"
        assert result["po_match"]["po_number"] == "PO-1005"
        assert result["po_match"]["within_tolerance"] is True
    else:
        # Refuses to guess rather than fabricating fields off an image-only PDF.
        assert result["extracted"]["extraction_method"] == "none"
        assert result["po_match"]["po_number"] is None


def _check_duplicate(result):
    assert any("matches run #" in r["text"] for r in result["reasons"]), \
        "the rejection should cite the earlier run it collided with"


def _check_multi_po(result):
    """One invoice, two purchase orders.

    The interesting part is that NOTHING is over budget: PO-1006 ($4,000) and
    PO-1007 ($2,500) authorise exactly the $6,500 billed. It is held anyway,
    because the document never says which PO each line belongs to -- the split
    is calculated, and a calculated split is a proposal rather than an
    authorisation.
    """
    pm = result["po_match"]
    assert pm["is_multi"] is True
    assert pm["po_numbers"] == ["PO-1006", "PO-1007"]
    # Every dollar is authorised; this is not an over-budget hold.
    assert pm["within_tolerance"] is True
    assert pm["remaining_before"] == 6500.00

    allocations = pm["allocations"]
    assert [(a["po_number"], a["amount"]) for a in allocations] == [
        ("PO-1006", 4000.00), ("PO-1007", 2500.00)]
    # The invariant the ledger depends on.
    assert round(sum(a["amount"] for a in allocations), 2) == pm["invoice_total"]

    assert result["audit"]["allocation_basis"] == "calculated"
    assert "Invoice-to-PO split stated" in result["audit"]["rules_failed"]
    assert any("does not state how much belongs to each" in r["text"]
               for r in result["reasons"]), \
        "the hold should say the split was calculated, not that money is missing"


def _check_fx_match(result):
    """EUR 2,000.00 converts to exactly USD 2,160.00 at the pinned rate, which
    is precisely what PO-1008 authorises. A genuinely different currency,
    genuinely the same value once converted -- the case the old "mismatch
    always holds" rule could never approve, and the pinned+versioned rate
    table is what makes approving it safe.
    """
    pm = result["po_match"]
    assert pm["currency_mismatch"] is True
    assert pm["currency_same_number_suspected"] is False
    assert pm["fx"]["applied"] is True
    assert pm["fx"]["converted_total"] == 2160.00
    assert pm["within_tolerance"] is True

    assert result["audit"]["currency"]["fx"]["rate_version"]
    assert any(r["text"].startswith("Currency converted:") for r in result["reasons"])


def _check_currency_collision(result):
    """Invoice states 5,000.00 EUR -- the same raw digits as PO-1009's
    5,000.00 USD, not a converted equivalent. No correct conversion produces
    identical digits in a different currency, so this is rejected outright
    rather than held: at the pinned rate it is actually 5,400.00, so paying
    the face value would silently underpay by $400.
    """
    pm = result["po_match"]
    assert pm["currency_same_number_suspected"] is True
    assert pm["fx"]["converted_total"] == 5400.00

    assert any("exact same figure" in r["text"] for r in result["reasons"])
    assert result["audit"]["currency"]["same_number_suspected"] is True


def _check_prompt_injection(result):
    """The invoice is otherwise flawless -- approved vendor, explicit PO, exact
    amount, sound arithmetic -- so the hold has exactly one cause, and that is
    what makes this case worth having.

    Two properties matter and neither is "the injection was blocked":

    1. The hostile text was TRANSCRIBED, not obeyed and not dropped. The prompt
       instructs the model to copy such text into the field where it physically
       appears, so a reviewer sees what the document actually said.
    2. The verdict is REJECTED. That reverses the original policy by the product
       owner's decision -- a document that tries to direct the process judging
       it is refused outright rather than queued for a person. The cost is real
       and is recorded beside the branch in rules.decide: the guard is a keyword
       matcher, and REJECTED is terminal, so a false positive needs an
       administrator rather than a reviewer.

    Note the guard is not what stops an injection from working -- the model has
    no authority to begin with, and rules.decide() never sees it. The guard is
    what puts a person in front of the document before money moves.
    """
    audit = result["audit"]
    assert audit["rules_failed"] == ["Security screen"], (
        "the ONLY failing check must be the security screen -- if anything else "
        "fails, this sample has stopped isolating the injection"
    )

    security = [r["text"] for r in result["reasons"] if r["text"].startswith("SECURITY:")]
    assert len(security) == 1, "the security finding must be stated once, plainly"

    # Two payloads in two fields, so two findings under two different labels --
    # the guard reports one finding per field, which is what keeps them
    # distinguishable. They are asserted against the REASON TEXT because that
    # is the sentence a reviewer actually reads; the audit records the count.
    assert "decision tampering" in security[0]
    assert "instruction override" in security[0]

    screen = next(c for c in audit["rules"] if c["name"] == "Security screen")
    assert screen["passed"] is False
    assert "2 instruction-like finding" in screen["detail"]

    # The document was transcribed, not sanitised: the reviewer sees the real text.
    assert "auto-approve" in security[0]

    # The PO was matched and covered in full -- the money was never the problem.
    pm = result["po_match"]
    assert pm["po_number"] == "PO-1010"
    assert pm["within_tolerance"] is True

    # Nothing was consumed: a rejected run charges no PO budget either.
    assert result["status"] == "REJECTED"



def _check_line_item_mismatch(result):
    """The invoice total is EXACTLY what the PO authorises, and it is still held.

    That is the whole case, so the premise is asserted before the conclusion: if
    the balance check ever stops reporting a zero variance here, this sample has
    quietly become an ordinary over-budget invoice and proves nothing.

    Held, not rejected. A short delivery at a revised price is something to ask
    the buyer about; rejecting would bounce an invoice the buyer may already
    have agreed to.
    """
    pm = result["po_match"]
    assert pm["po_number"] == "PO-EDGE-001"
    assert pm["diff"] == 0.0, "the premise: the total is not what is wrong here"
    assert pm["within_tolerance"] is True

    audit = result["audit"]
    assert audit["rules_failed"] == ["Line items match the PO"], (
        "every total-based check must pass; only the line-item rule may fail"
    )

    reason = next(r["text"] for r in result["reasons"] if r["level"] == "fail")
    # The reviewer is told the actual figures, not merely that something differs.
    assert "10" in reason and "8" in reason
    assert "62,500" in reason and "50,000" in reason



def _check_concurrency_a(result):
    # First of a pair that together claim $8,000 of a $7,000 order. Alone it is
    # an ordinary partial invoice -- which is the point: each is individually
    # valid, so only the ledger can tell them apart.
    before, after = po_balances(result)
    assert (before, after) == (7000.00, 3000.00)
    assert result["po_match"]["is_partial"] is True


def _check_concurrency_b(result):
    # Second of the pair. Run sequentially it meets a $3,000 balance and is held;
    # run simultaneously, the row lock in save_run_checked() decides which of the
    # two gets here, and the other one is this.
    before, _ = po_balances(result)
    assert before == 3000.00
    assert result["po_match"]["within_tolerance"] is False


EXTRA_CHECKS = {
    "01_happy_path_acme.pdf": _check_happy_path,
    "02_split_po_globex_a.pdf": _check_split_a,
    "03_split_po_globex_b.pdf": _check_split_b,
    "03b_split_po_globex_overflow.pdf": _check_overflow,
    "04_missing_invoice_number.pdf": _check_missing_number,
    "05_scanned_no_text.pdf": _check_scanned,
    "06_duplicate_of_01.pdf": _check_duplicate,
    "07_multi_po_wayne.pdf": _check_multi_po,
    "08_fx_match_oscorp.pdf": _check_fx_match,
    "09_currency_number_collision_lexcorp.pdf": _check_currency_collision,
    "10_prompt_injection_cyberdyne.pdf": _check_prompt_injection,
    "11_line_item_mismatch_acme_tech.pdf": _check_line_item_mismatch,
    "12_concurrency_race_keyboard_a.pdf": _check_concurrency_a,
    "13_concurrency_race_keyboard_b.pdf": _check_concurrency_b,
}


# --------------------------------------------------------------------------
# the suite
# --------------------------------------------------------------------------

@pytest.mark.parametrize("filename,expected", MANIFEST, ids=[n for n, _ in MANIFEST])
def test_sample_invoice(client, filename, expected):
    """Each sample, in manifest order, must produce its documented verdict.

    Order-dependent by design: these share a database and build on each other.
    A failure early in the sequence will cascade, because the later cases are
    *about* the state the earlier ones left behind.
    """
    result = run_invoice(client, filename)

    assert result["status"] == expected, (
        f"{filename}: expected {expected}, got {result['status']}\n"
        + "\n".join(f"  [{r['level']}] {r['text']}" for r in result["reasons"])
    )

    assert result["run_id"] is not None, "every run must be persisted"
    assert result["reasons"], "a verdict with no reasoning is not auditable"

    EXTRA_CHECKS[filename](result)
