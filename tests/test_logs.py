"""Phase I: logs, filtering, grouping and exports.

WHAT THESE TESTS ARE FOR

The log is a QUERY, not a table (backend/logs.py). Nothing writes it, so
nothing can be "wrong in the database" -- everything that can go wrong here
goes wrong in the reading: a filter that quietly matches more than it says, a
page boundary that drops a row, an export that ignores the filters the user
was looking at, a search box that treats `%` as a wildcard, a CSV cell that
executes when someone opens it.

So each test puts a KNOWN set of runs, reviews, claims and messages into a
fresh schema and asserts the exact rows that must come back. Four properties
get particular attention, because they are the ones that make a log wrong in
ways nobody notices:

1. **Paging must be total.** Several events land in the same microsecond
   routinely -- `save_run_checked` writes PROCESSING_COMPLETED and
   REVIEW_REQUIRED in one transaction with one timestamp string. If ordering
   is not total, those two swap between pages, which shows one row twice and
   drops the other. Tested by paging through rows written with an IDENTICAL
   timestamp and asserting the union is exactly the set, with no repeats.

2. **The export must not be a second query.** It must return exactly the rows
   the list returned under the same filters -- not "roughly", not "also".

3. **A filter must never widen.** Particularly `actor`: a non-administrator
   asking to group by a colleague must get their own row, not the colleague's.

4. **Nothing may leak.** No storage key, no audit blob, no extracted field,
   no email address or subject -- checked by seeding distinctive values and
   grepping every response body for them.

Timestamps are written directly in several tests. That is deliberate and is
the only way to exercise date boundaries and tie-breaking: the pipeline always
stamps "now". Everywhere else the real `rules.decide()` /
`storage.save_run_checked()` path is used, so the rows under test are the rows
the application actually writes.
"""
import csv
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import analytics   # noqa: E402
import auth        # noqa: E402
import logs        # noqa: E402
import main        # noqa: E402
import matching    # noqa: E402
import rules       # noqa: E402
import storage     # noqa: E402
import pg_schema   # noqa: E402
from conftest import auth_headers, token_for   # noqa: E402

VENDOR = "Globex Logistics"          # approved; holds PO-1002 at $5,000
PO = "PO-1002"
ACME = "Acme Office Supplies"        # approved; holds PO-1001 at $1,240
ACME_PO = "PO-1001"


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    yield schema
    pg_schema.drop_schema(schema)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    with TestClient(main.app, headers=auth_headers("admin", "ada")) as c:
        assert storage.PG_SCHEMA == db, "startup must not restore the real schema"
        yield c


# --------------------------------------------------------------------------
# helpers -- real pipeline rows wherever possible
# --------------------------------------------------------------------------

DEFAULT_STAGES = [{"name": "INGEST", "status": "ok", "ms": 5}]

# A stage log shaped like one `run_pipeline` actually writes -- several
# stages, mixed statuses, a detail sentence and a millisecond count on each.
# Only the stage-view tests need more than one stage, so `submit` keeps its
# one-stage default and this is passed in where it matters.
PIPELINE_STAGES = [
    {"name": "INGEST", "status": "ok", "detail": "Received the file.", "ms": 5},
    {"name": "EXTRACT_TEXT", "status": "ok", "detail": "2 pages.", "ms": 40},
    {"name": "VENDOR_CHECK", "status": "warn", "detail": "Vendor matched loosely.",
     "ms": 3},
    {"name": "PO_MATCH", "status": "fail", "detail": "No PO could be matched.",
     "ms": 7},
    {"name": "DECISION", "status": "ok", "detail": "Held for review.", "ms": 1},
]


def submit(total, invoice_number, po=PO, vendor=VENDOR, uploaded_by="analyst-1",
           currency="USD", stages=None):
    """Evaluate and commit one invoice exactly as the pipeline does."""
    extracted = {
        "vendor_name": vendor, "invoice_number": invoice_number,
        "total": total, "subtotal": total, "tax": 0.0,
        "po_references": [po] if isinstance(po, str) else list(po or []),
        "currency": currency, "extraction_method": "groq (text)",
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
        f"{invoice_number}.pdf", status, extracted, po_match,
        list(stages if stages is not None else DEFAULT_STAGES), reasons,
        tolerance_for=matching.tolerance_for, audit=audit, uploaded_by=uploaded_by)
    return run_id, final_status


def window(key="30d", date_from=None, date_to=None):
    return analytics.resolve_window(key, date_from, date_to)


def filters(**kwargs):
    key = kwargs.pop("range", "30d")
    return logs.LogFilters(window(key), **kwargs)


def rows_of(**kwargs):
    """Every matching row (paged to the cap), as the list endpoint returns it."""
    return logs.search(filters(**kwargs), 1, logs.MAX_PAGE_SIZE)["rows"]


def total_of(**kwargs):
    return logs.search(filters(**kwargs), 1, 1)["total"]


def stamp(days_ago=0, hour=12, minute=0, micro=0):
    d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
        hour=hour, minute=minute, second=0, microsecond=micro)
    return d.isoformat()


def set_activity_time(activity_id, iso):
    """Move one event in time. Only date-boundary and tie-break tests use this."""
    with storage.write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE invoice_activity SET created_at=%s WHERE id=%s",
                        (iso, activity_id))


def activity_ids(run_id):
    return [a["id"] for a in storage.list_activity(run_id)]


def add_document(run_id, source="MANUAL_UPLOAD", filename="invoice.pdf"):
    """A document row, which is where `source` lives (Phase C) -- `runs` has
    no source column, so a log's source filter reads it from here."""
    storage.save_document(run_id=run_id, original_filename=filename,
                          mime_type="application/pdf", size_bytes=1024,
                          sha256_hex="0" * 64, uploaded_by="analyst-1",
                          source=source, storage_backend="local",
                          storage_key="deadbeef" * 4 + ".pdf")


def make_message(sha="sha-1", classification="UNVERIFIED", status="QUARANTINED",
                 sender="billing@vendor.example", subject="Invoice attached",
                 submitted_by="analyst-1"):
    """One Phase F security record, which writes three email_activity rows."""
    return storage.save_email_message({
        "sha256": sha, "message_id": f"<{sha}@example>",
        "from_address": sender, "from_domain": sender.split("@")[-1],
        "from_display_name": "Vendor Billing", "envelope_from": sender,
        "subject": subject, "size_bytes": 2048, "attachment_count": 1,
        "has_pdf_attachment": True, "spf_result": "unavailable",
        "dkim_result": "unavailable", "dmarc_result": "unavailable",
        "dmarc_aligned": False, "signature_kind": None,
        "signature_result": "not_present", "trusted_sender": False,
        "classification": classification, "status": status,
        "reasons": ["nothing could be checked"], "audit": {},
    }, submitted_by=submitted_by, source="SUBMITTED")


# ==========================================================================
# 1. retrieval
# ==========================================================================

def test_a_log_row_carries_the_event_and_the_invoice_it_is_about(db):
    """A log line saying "REJECTED by ada" with no invoice attached is a
    riddle, not a log. The context comes from the join, not from a per-row
    follow-up query."""
    run_id, _ = submit(1000.0, "INV-CTX")
    row = [r for r in rows_of() if r["event"] == "PROCESSING_COMPLETED"][0]

    assert row["run_id"] == run_id
    assert row["vendor"] == VENDOR
    assert row["invoice_number"] == "INV-CTX"
    assert row["po_number"] == PO
    assert row["decision"] == "APPROVED"
    assert row["status"] == "APPROVED"
    assert row["actor"] == "analyst-1"
    assert row["stream"] == "invoice"
    assert row["id"] == f"invoice:{row['event_id']}"


def test_the_log_reads_the_activity_tables_and_nothing_else(db):
    """Every row the log returns is a row Phase D or Phase F already wrote.

    This is the phase's central claim -- no mirror, no second log -- and it is
    checked by counting, not by reading the source."""
    a, _ = submit(1000.0, "INV-1")
    b, _ = submit(99999.0, "INV-2")
    make_message("sha-log")

    written = len(storage.list_activity(a)) + len(storage.list_activity(b)) + \
        len(storage.list_email_activity(1))
    assert total_of(range="all") == written


def test_an_empty_database_reports_no_rows_rather_than_failing(db):
    result = logs.search(filters(), 1, 50)
    assert result["rows"] == []
    assert result["total"] == 0
    assert result["total_is_exact"] is True
    assert result["has_more"] is False


def test_a_system_generated_event_reports_a_null_actor_not_an_invented_name(db):
    """`actor` is NULL for an event the system generated on its own (6.1).
    Reporting it as "system" would be indistinguishable from a real user
    called that."""
    submit(99999.0, "INV-SYS")            # holds -> REVIEW_REQUIRED, actor NULL
    required = [r for r in rows_of() if r["event"] == "REVIEW_REQUIRED"]
    assert len(required) == 1
    assert required[0]["actor"] is None


# ==========================================================================
# 2. pagination and ordering
# ==========================================================================

def test_pages_are_disjoint_and_together_are_the_whole_result(db):
    for i in range(6):
        submit(100.0 + i, f"INV-P{i}")

    everything = rows_of()
    seen, page = [], 1
    while True:
        result = logs.search(filters(), page, 3)
        if not result["rows"]:
            break
        seen.extend(r["id"] for r in result["rows"])
        page += 1

    assert len(seen) == len(set(seen)), "a row appeared on two pages"
    assert seen == [r["id"] for r in everything]


def test_ordering_is_total_so_identical_timestamps_cannot_swap_between_pages(db):
    """THE tie-break test.

    save_run_checked writes PROCESSING_COMPLETED and REVIEW_REQUIRED inside one
    transaction with the same `datetime.now()` string, so equal timestamps are
    the normal case, not a contrived one. Here every event is forced to the
    same instant, which is the worst case: if `created_at` were the only sort
    key, the row order would be whatever the plan produced and could differ
    between two queries -- duplicating rows across pages and dropping others.
    """
    for i in range(5):
        run_id, _ = submit(99999.0 + i, f"INV-TIE{i}")
        for aid in activity_ids(run_id):
            set_activity_time(aid, stamp(days_ago=1))

    everything = [r["id"] for r in rows_of()]
    assert len(everything) == len(set(everything))

    paged = []
    for page in range(1, 20):
        rows = logs.search(filters(), page, 2)["rows"]
        if not rows:
            break
        paged.extend(r["id"] for r in rows)

    assert paged == everything
    # And repeatable: the same query twice gives the same order.
    assert [r["id"] for r in rows_of()] == everything


def test_the_order_can_be_reversed_and_stays_total(db):
    submit(1000.0, "INV-ORD")
    newest_first = [r["id"] for r in rows_of()]
    oldest_first = [r["id"] for r in rows_of(order="asc")]
    assert oldest_first == list(reversed(newest_first))


def test_the_total_says_whether_it_is_exact(db):
    """A caller must be able to tell 10,000 from "at least 10,000"."""
    submit(1000.0, "INV-TOT")
    result = logs.search(filters(), 1, 1)
    assert result["total_is_exact"] is True
    assert result["total"] < logs.COUNT_CEILING


def test_paging_past_the_end_is_empty_not_an_error(db):
    submit(1000.0, "INV-END")
    result = logs.search(filters(), 99, 50)
    assert result["rows"] == []
    assert result["total"] > 0
    assert result["has_more"] is False


@pytest.mark.parametrize("page,size", [(0, 50), (-1, 50), (1, 0), (1, -5),
                                       (1, logs.MAX_PAGE_SIZE + 1), ("x", 50),
                                       (1, "big")])
def test_invalid_paging_is_refused_rather_than_clamped(db, page, size):
    """A page size of 5,000 silently served as 200 tells the caller they have
    everything when they have 4% of it."""
    with pytest.raises(logs.LogError):
        logs.resolve_page(page, size)


# ==========================================================================
# 3. filtering
# ==========================================================================

def test_filtering_by_event_returns_only_that_event(db):
    run_id, _ = submit(99999.0, "INV-EV")
    storage.record_human_review(run_id, "REJECTED", reviewer="ada", note="no")
    rows = rows_of(event="REJECTED")
    assert [r["event"] for r in rows] == ["REJECTED"]


def test_filtering_by_actor_returns_only_that_person(db):
    run_id, _ = submit(99999.0, "INV-ACT")
    storage.add_comment(run_id, "ada", "looking at it")
    storage.add_comment(run_id, "bob", "me too")

    assert {r["actor"] for r in rows_of(actor="ada")} == {"ada"}
    assert {r["actor"] for r in rows_of(actor="bob")} == {"bob"}


def test_the_system_actor_token_selects_events_no_person_performed(db):
    """NULL cannot be expressed in a query string, so the reserved token is
    the only way to ask for system events -- and it must not also return the
    ones a person performed."""
    run_id, _ = submit(99999.0, "INV-NULL")
    storage.add_comment(run_id, "ada", "mine")

    rows = rows_of(actor=logs.SYSTEM_ACTOR)
    assert rows, "the hold itself is a system event"
    assert all(r["actor"] is None for r in rows)
    assert "COMMENT_ADDED" not in {r["event"] for r in rows}


def test_filtering_by_vendor_excludes_other_vendors(db):
    submit(1000.0, "INV-G", po=PO, vendor=VENDOR)
    submit(500.0, "INV-A", po=ACME_PO, vendor=ACME)

    assert {r["vendor"] for r in rows_of(vendor=VENDOR)} == {VENDOR}
    assert {r["vendor"] for r in rows_of(vendor=ACME)} == {ACME}


def test_filtering_by_run_and_by_invoice_number_agree(db):
    run_id, _ = submit(1000.0, "INV-ONE")
    submit(1000.0, "INV-TWO")
    by_run = {r["id"] for r in rows_of(run_id=run_id)}
    by_number = {r["id"] for r in rows_of(invoice_number="INV-ONE")}
    assert by_run == by_number and by_run


def test_filtering_by_po_finds_a_multi_po_invoice_through_the_allocation_ledger(db):
    """A multi-PO invoice names ONE PO in `runs.po_number` and all of them in
    `run_allocations` (3). Filtering on the column alone would hide the
    invoice from the second PO's own log."""
    run_id, status = submit(4000.0, "INV-MULTI", po=[PO, ACME_PO])
    assert status == "NEEDS_REVIEW", "a multi-PO invoice is always held"

    allocated = {a["po_number"] for a in storage.allocations_for_run(run_id)}
    assert allocated == {PO, ACME_PO}

    for po in (PO, ACME_PO):
        found = {r["run_id"] for r in rows_of(po_number=po)}
        assert run_id in found, f"the invoice is invisible under {po}"


def test_a_multi_po_invoice_is_not_counted_twice(db):
    """EXISTS, not a join: three POs must not triple every activity row."""
    run_id, _ = submit(4000.0, "INV-DUP", po=[PO, ACME_PO])
    rows = rows_of(po_number=PO)
    assert len(rows) == len({r["id"] for r in rows})
    assert len(rows) == len(storage.list_activity(run_id))


def test_filtering_by_decision_uses_the_rules_verdict_not_the_human_one(db):
    """`automated_decision` is immutable audit history (3): a later human
    ruling must not retroactively change which invoices the rules held."""
    run_id, _ = submit(99999.0, "INV-IMM")
    storage.record_human_review(run_id, "ACCEPTED", reviewer="ada")

    assert storage.get_run(run_id)["status"] == "APPROVED"
    held = {r["run_id"] for r in rows_of(decision="NEEDS_REVIEW")}
    assert run_id in held, "the rules held it, and that stays true"
    assert run_id not in {r["run_id"] for r in rows_of(decision="APPROVED")}


def test_filtering_by_status_uses_the_ledger_status(db):
    run_id, _ = submit(99999.0, "INV-LED")
    storage.record_human_review(run_id, "ACCEPTED", reviewer="ada")
    assert run_id in {r["run_id"] for r in rows_of(status="APPROVED")}


def test_filtering_by_source_reads_the_document_row(db):
    """`source` lives on `documents` (Phase C); `runs` has no such column."""
    manual, _ = submit(1000.0, "INV-MAN")
    emailed, _ = submit(1100.0, "INV-EML")
    add_document(manual, "MANUAL_UPLOAD")
    add_document(emailed, "EMAIL")

    assert {r["run_id"] for r in rows_of(source="MANUAL_UPLOAD")} == {manual}
    assert emailed in {r["run_id"] for r in rows_of(source="EMAIL")}


def test_a_run_with_several_document_rows_does_not_duplicate_its_events(db):
    """A scalar subquery, not a join -- a join would multiply every activity
    row by the number of documents."""
    run_id, _ = submit(1000.0, "INV-DOCS")
    add_document(run_id, "MANUAL_UPLOAD", "first.pdf")
    add_document(run_id, "MANUAL_UPLOAD", "second.pdf")
    rows = rows_of(run_id=run_id)
    assert len(rows) == len(storage.list_activity(run_id))


def test_filtering_by_rule_failed_groups_by_rule_name(db):
    """The rule NAME, from `audit_json.rules_failed`.

    Not a LIKE over the JSON text: the audit trail lists every rule that was
    EVALUATED, passed ones included, so a text match would return runs where
    the rule passed and look like it had worked."""
    over, _ = submit(99999.0, "INV-OVER")
    fine, _ = submit(1000.0, "INV-FINE")

    audit = storage.get_run(over)["audit"]
    assert "PO remaining check" in audit["rules_failed"]
    evaluated = [r["name"] for r in storage.get_run(fine)["audit"]["rules"]]
    assert "PO remaining check" in evaluated, \
        "the rule was evaluated on the passing run too"

    found = {r["run_id"] for r in rows_of(rule_failed="PO remaining check")}
    assert over in found
    assert fine not in found


def test_a_rule_nothing_failed_matches_nothing_rather_than_everything(db):
    submit(1000.0, "INV-NORULE")
    assert rows_of(rule_failed="No such rule at all") == []


def test_a_malformed_audit_blob_is_skipped_not_fatal(db):
    """One bad row must not take the log down -- the same guarded parse
    Phase H uses (7c.2)."""
    good, _ = submit(99999.0, "INV-GOOD")
    bad, _ = submit(99998.0, "INV-BAD")
    with storage.write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET audit_json='{not json' WHERE id=%s", (bad,))

    found = {r["run_id"] for r in rows_of(rule_failed="PO remaining check")}
    assert good in found
    assert bad not in found


def test_filters_combine(db):
    """The brief's own example: vendor AND period AND action."""
    a, _ = submit(99999.0, "INV-CA", vendor=VENDOR, po=PO)
    b, _ = submit(1000.0, "INV-CB", vendor=ACME, po=ACME_PO)
    storage.record_human_review(a, "REJECTED", reviewer="ada", note="no")

    rows = rows_of(vendor=VENDOR, event="REJECTED", range="30d")
    assert len(rows) == 1
    assert rows[0]["run_id"] == a
    assert rows_of(vendor=ACME, event="REJECTED") == []


def test_combining_filters_narrows_rather_than_widens(db):
    submit(1000.0, "INV-N1")
    submit(99999.0, "INV-N2")   # held, so its history carries a second event
    broad = total_of()
    narrow = total_of(event="PROCESSING_COMPLETED")
    assert 0 < narrow < broad


@pytest.mark.parametrize("kwargs", [
    {"stream": "everything"},
    {"decision": "MAYBE"},
    {"status": "PENDING"},
    {"source": "CARRIER_PIGEON"},
    {"email_status": "OPENED"},
    {"order": "sideways"},
    {"event": "DROP TABLE runs"},
    {"invoice_number": "'; DROP TABLE runs; --"},
    {"po_number": "<script>"},
    {"run_id": "not-a-number"},
])
def test_an_invalid_filter_value_is_refused(db, kwargs):
    with pytest.raises(logs.LogError):
        filters(**kwargs)


def test_an_over_long_filter_value_is_refused(db):
    with pytest.raises(logs.LogError):
        filters(vendor="x" * 5000)


# ==========================================================================
# 4. date windows -- reusing the analytics parser, not a second one
# ==========================================================================

def test_the_window_filters_on_when_the_event_happened(db):
    """Logs window on the EVENT's time, not the invoice's arrival.

    Deliberately different from the analytics endpoints, which window on
    `runs.created_at` because they ask about a cohort of invoices. An event
    yesterday about a month-old invoice belongs in yesterday's log -- the same
    distinction analytics.users() already draws for reviewer workload (7c.9).
    """
    run_id, _ = submit(99999.0, "INV-WHEN")
    ids = activity_ids(run_id)
    set_activity_time(ids[0], stamp(days_ago=90))     # long ago
    set_activity_time(ids[1], stamp(days_ago=0))      # today

    assert total_of(range="today") == 1
    assert total_of(range="all") == len(ids)


def test_a_custom_range_includes_the_day_named_by_to(db):
    """`from == to` is one full day, not an empty window -- exactly as
    analytics.resolve_window already defines it."""
    run_id, _ = submit(1000.0, "INV-CUS")
    day = (datetime.now(timezone.utc) - timedelta(days=5)).date().isoformat()
    for aid in activity_ids(run_id):
        set_activity_time(aid, stamp(days_ago=5))

    f = logs.LogFilters(window("custom", day, day))
    assert logs.search(f, 1, 50)["total"] == len(activity_ids(run_id))


def test_a_midnight_event_belongs_to_the_day_that_starts_then(db):
    """Half-open `[start, end)`, so UTC midnight opens a day rather than
    closing the one before."""
    run_id, _ = submit(99999.0, "INV-MID")   # held: two activity rows
    ids = activity_ids(run_id)
    assert len(ids) == 2
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    set_activity_time(ids[0], today.isoformat())
    set_activity_time(ids[1], (today - timedelta(microseconds=1)).isoformat())

    assert total_of(range="today") == 1


def test_a_bad_range_is_refused_the_same_way_the_dashboard_refuses_it(db):
    """One parser, so a filter panel cannot disagree with the dashboard beside
    it about what a range means -- the trap the Phase I brief named."""
    with pytest.raises(analytics.AnalyticsError):
        window("last-fortnight")
    with pytest.raises(analytics.AnalyticsError):
        window("custom", "2026-13-45", "2026-01-01")


def test_an_unbounded_range_contributes_no_bound(db):
    """`all` must not become a sentinel epoch date, which would silently
    exclude anything older than it."""
    run_id, _ = submit(1000.0, "INV-OLD")
    for aid in activity_ids(run_id):
        set_activity_time(aid, "1999-01-01T00:00:00+00:00")
    assert total_of(range="all") == len(activity_ids(run_id))
    assert total_of(range="30d") == 0


# ==========================================================================
# 5. search
# ==========================================================================

def test_search_matches_across_the_fields_a_person_would_type(db):
    run_id, _ = submit(99999.0, "INV-SEARCHABLE")
    storage.add_comment(run_id, "ada", "chasing the vendor about this one")

    assert rows_of(search="SEARCHABLE")
    assert rows_of(search="Globex")
    assert rows_of(search="chasing the vendor")
    assert rows_of(search="ada")


def test_search_is_case_insensitive(db):
    submit(1000.0, "INV-CASE")
    assert rows_of(search="globex logistics")
    assert rows_of(search="GLOBEX LOGISTICS")


def test_search_matches_partially(db):
    submit(1000.0, "INV-PARTIAL")
    assert rows_of(search="PARTIA")


def test_search_with_no_match_returns_nothing_rather_than_everything(db):
    submit(1000.0, "INV-NOPE")
    assert rows_of(search="nothing here matches this") == []


@pytest.mark.parametrize("term", ["%", "%%", "\\", "100%", "a_b",
                                  "'", "''", "--", "\\%"])
def test_like_metacharacters_are_matched_literally(db, term):
    """`%` typed into a search box must find a literal percent sign.

    Without escaping it matches everything while looking like it had filtered
    something -- the worst kind of wrong answer, because it is plausible."""
    submit(1000.0, "INV-META")
    everything = total_of()
    assert everything > 0
    assert total_of(search=term) == 0, f"{term!r} behaved as a wildcard"


def test_an_underscore_matches_a_literal_underscore_and_only_that(db):
    """`_` cannot join the list above, and the reason is the point of the test.

    Every event type this application writes contains a literal underscore
    (`PROCESSING_COMPLETED`), and `event_type` is one of the columns search
    looks at -- so a CORRECT literal search for `_` must find those rows.
    Asserting it finds nothing would assert the opposite of the property. What
    must not happen is `_` matching a character that is not an underscore.
    """
    run_id, _ = submit(1000.0, "INV-UNDER")

    # It matches literally: the event name really does contain one.
    assert total_of(search="_") > 0
    assert rows_of(search="PROCESSING_COMPLETED")

    # It does NOT stand in for any other character.
    assert total_of(search="INV_UNDER") == 0, "`_` matched the hyphen in INV-UNDER"
    assert total_of(search="PROCESSINGxCOMPLETED") == 0
    assert storage.get_run(run_id)["filename"] == "INV-UNDER.pdf"


def test_an_escaped_metacharacter_still_finds_a_real_one(db):
    """Escaping must not make a literal `%` unfindable either."""
    run_id, _ = submit(1000.0, "INV-PCT")
    storage.add_comment(run_id, "ada", "vendor applied a 10% discount")
    assert len(rows_of(search="10% discount")) == 1
    assert len(rows_of(search="%")) == 1


def test_a_hostile_search_term_is_a_search_term(db):
    """Bound as a parameter, so it can only ever match text."""
    submit(1000.0, "INV-INJ")
    before = total_of()
    assert rows_of(search="'; DROP TABLE invoice_activity; --") == []
    assert total_of() == before, "the table is still there"


def test_search_combines_with_the_other_filters(db):
    a, _ = submit(99999.0, "INV-SA", vendor=VENDOR, po=PO)
    submit(1000.0, "INV-SB", vendor=ACME, po=ACME_PO)
    rows = rows_of(search="INV-S", vendor=VENDOR)
    assert {r["run_id"] for r in rows} == {a}


# ==========================================================================
# 6. grouping
# ==========================================================================

def test_grouping_by_event_counts_each_event_type(db):
    submit(1000.0, "INV-GE1")
    submit(1100.0, "INV-GE2")
    result = logs.group(filters(), "event")
    counts = {g["key"]: g["count"] for g in result["groups"]}
    assert counts["PROCESSING_COMPLETED"] == 2
    assert result["label"] == "Event type"
    assert sum(counts.values()) == total_of()


def test_grouping_by_actor_counts_what_each_person_did(db):
    run_id, _ = submit(99999.0, "INV-GA")
    storage.add_comment(run_id, "ada", "one")
    storage.add_comment(run_id, "ada", "two")
    storage.add_comment(run_id, "bob", "three")

    counts = {g["key"]: g["count"]
              for g in logs.group(filters(event="COMMENT_ADDED"), "actor")["groups"]}
    assert counts == {"ada": 2, "bob": 1}


def test_grouping_by_vendor_day_decision_status_and_stream_all_work(db):
    submit(1000.0, "INV-GV", vendor=VENDOR, po=PO)
    submit(500.0, "INV-GW", vendor=ACME, po=ACME_PO)
    make_message("sha-group")

    for axis in ("vendor", "day", "decision", "status", "stream", "run", "source"):
        result = logs.group(filters(), axis)
        assert result["groups"], f"{axis} produced nothing"
        assert sum(g["count"] for g in result["groups"]) == total_of()


def test_a_group_with_no_value_on_that_axis_reports_a_null_key(db):
    """An email event has no vendor. Reported as null, not bucketed into a
    made-up group called "unknown" -- the rows do not say that."""
    submit(1000.0, "INV-GN")
    make_message("sha-null")
    keys = [g["key"] for g in logs.group(filters(), "vendor")["groups"]]
    assert None in keys
    assert VENDOR in keys


def test_grouped_counts_reflect_the_same_filters_as_the_rows(db):
    a, _ = submit(99999.0, "INV-GF", vendor=VENDOR, po=PO)
    submit(500.0, "INV-GG", vendor=ACME, po=ACME_PO)
    grouped = logs.group(filters(vendor=VENDOR), "event")
    assert sum(g["count"] for g in grouped["groups"]) == total_of(vendor=VENDOR)
    assert all(g["key"] for g in grouped["groups"])


def test_a_group_carries_when_it_started_and_when_it_last_happened(db):
    submit(1000.0, "INV-GT")
    g = logs.group(filters(), "event")["groups"][0]
    assert g["first_at"] <= g["last_at"]


def test_an_unknown_grouping_axis_is_refused(db):
    """`group_by` names a key in a frozen table and nothing else reaches SQL."""
    for axis in ("password", "runs.audit_json", "1); DROP TABLE runs; --", ""):
        with pytest.raises(logs.LogError):
            logs.group(filters(), axis)


@pytest.mark.parametrize("limit", [0, -1, logs.MAX_GROUP_LIMIT + 1, "many"])
def test_an_invalid_group_limit_is_refused(db, limit):
    with pytest.raises(logs.LogError):
        logs.resolve_group_limit(limit)


def test_grouping_says_when_it_returned_fewer_keys_than_exist(db):
    for i in range(6):
        submit(1000.0 + i, f"INV-GL{i}")
    result = logs.group(filters(), "run", limit=2)
    assert len(result["groups"]) == 2
    assert result["truncated"] is True
    assert result["distinct_keys"] > 2


# ==========================================================================
# 7. the two streams
# ==========================================================================

def test_message_events_appear_beside_invoice_events(db):
    submit(1000.0, "INV-2S")
    make_message("sha-two")
    streams = {r["stream"] for r in rows_of()}
    assert streams == {"invoice", "email"}


def test_each_stream_can_be_asked_for_on_its_own(db):
    submit(1000.0, "INV-S1")
    make_message("sha-s1")
    assert {r["stream"] for r in rows_of(stream="invoice")} == {"invoice"}
    assert {r["stream"] for r in rows_of(stream="email")} == {"email"}
    assert total_of(stream="invoice") + total_of(stream="email") == total_of()


def test_a_message_event_carries_its_message_status(db):
    make_message("sha-status", status="QUARANTINED")
    rows = rows_of(stream="email")
    assert {r["email_status"] for r in rows} == {"QUARANTINED"}
    assert all(r["source"] == "EMAIL" for r in rows)


def test_filtering_by_message_status_excludes_invoice_events(db):
    """An invoice event has no message status, so asking for one drops that
    stream from the query rather than returning rows that cannot match."""
    submit(1000.0, "INV-ES")
    make_message("sha-es", status="QUARANTINED")
    rows = rows_of(email_status="QUARANTINED")
    assert rows and all(r["stream"] == "email" for r in rows)
    assert rows_of(email_status="ADMITTED") == []


def test_a_filter_no_stream_can_satisfy_is_an_empty_answer_not_an_error(db):
    """`stream=email` with `vendor=` is coherent and genuinely matches
    nothing; it must not 500 and must not silently ignore one of the two."""
    submit(1000.0, "INV-IMP")
    make_message("sha-imp")
    assert rows_of(stream="email", vendor=VENDOR) == []
    assert total_of(stream="email", vendor=VENDOR) == 0


def test_the_log_never_carries_a_sender_address_or_a_subject(db):
    """Phase F stores those; this phase does not restate them. A log entry
    links to the message by id, and Phase F's own endpoint owns the record."""
    make_message("sha-secret", sender="cfo@acme-secret.example",
                 subject="CONFIDENTIAL-SUBJECT-LINE")
    body = json.dumps(logs.search(filters(), 1, 50))
    assert "cfo@acme-secret.example" not in body
    assert "CONFIDENTIAL-SUBJECT-LINE" not in body
    assert "acme-secret.example" not in body


# ==========================================================================
# 8. detail
# ==========================================================================

def test_an_event_detail_carries_its_structured_metadata(db):
    run_id, _ = submit(99999.0, "INV-DET")
    storage.claim_review(run_id, "ada")
    claimed = [a for a in storage.list_activity(run_id)
               if a["event_type"] == "REVIEW_CLAIMED"][0]

    detail = logs.detail("invoice", claimed["id"])
    assert detail["event"] == "REVIEW_CLAIMED"
    assert detail["actor"] == "ada"
    assert isinstance(detail["metadata"], dict)
    assert "expires_at" in detail["metadata"]


def test_an_invoice_event_detail_names_the_rules_that_failed(db):
    """Structured, not the raw blob: the names and the reason sentences a
    reviewer was shown, not `audit_json` itself."""
    run_id, _ = submit(99999.0, "INV-RULES")
    detail = logs.detail("invoice", activity_ids(run_id)[0])
    assert "PO remaining check" in detail["run"]["rules_failed"]
    assert detail["run"]["reasons"]
    assert "audit" not in detail
    assert "extracted" not in detail


def test_a_message_event_detail_reports_counts_and_verdicts_only(db):
    make_message("sha-det", sender="a@leak.example", subject="LEAKY-SUBJECT")
    email_event = rows_of(stream="email")[0]
    detail = logs.detail("email", email_event["event_id"])
    body = json.dumps(detail)
    assert detail["message"]["classification"] == "UNVERIFIED"
    assert "LEAKY-SUBJECT" not in body
    assert "a@leak.example" not in body


def test_malformed_event_metadata_reads_as_absent_rather_than_raising(db):
    run_id, _ = submit(1000.0, "INV-MM")
    aid = activity_ids(run_id)[0]
    with storage.write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE invoice_activity SET metadata_json='{oops' WHERE id=%s",
                        (aid,))
    assert logs.detail("invoice", aid)["metadata"] is None


def test_an_unknown_event_is_absent_not_an_error(db):
    assert logs.detail("invoice", 999999) is None


@pytest.mark.parametrize("stream", ["runs", "", "INVOICE; DROP", "audit"])
def test_an_unknown_stream_is_refused(db, stream):
    with pytest.raises(logs.LogError):
        logs.detail(stream, 1)


# ==========================================================================
# 9. CSV export
# ==========================================================================

def read_csv(f, **kwargs):
    text = "".join(logs.export_csv(f, **kwargs))
    return list(csv.reader(io.StringIO(text)))


def test_the_export_has_a_header_row_even_when_nothing_matches(db):
    """An empty CSV with no header is indistinguishable from a failed
    download."""
    rows = read_csv(filters())
    assert rows == [[h for _, h in logs.EXPORT_COLUMNS]]


def test_the_export_headers_are_the_documented_columns(db):
    submit(1000.0, "INV-HDR")
    header = read_csv(filters())[0]
    assert header[:5] == ["Timestamp (UTC)", "Stream", "Event", "Actor", "Run"]
    assert "Vendor" in header and "Summary" in header


def test_the_export_contains_exactly_the_rows_the_list_showed(db):
    """The list and the export share one filter object and one query builder,
    so this is a property, not a coincidence -- and if the two ever diverge,
    this is where it surfaces."""
    submit(1000.0, "INV-X1")
    submit(1100.0, "INV-X2")
    f = filters()
    listed = logs.search(f, 1, logs.MAX_PAGE_SIZE)["rows"]
    exported = read_csv(f)[1:]
    assert len(exported) == len(listed)

    ts = [r[0] for r in exported]
    assert ts == [r["timestamp"] for r in listed]


def test_the_export_respects_every_active_filter(db):
    """The brief's own example: vendor + period + action."""
    a, _ = submit(99999.0, "INV-EA", vendor=VENDOR, po=PO)
    b, _ = submit(500.0, "INV-EB", vendor=ACME, po=ACME_PO)
    storage.record_human_review(a, "REJECTED", reviewer="ada", note="no")
    storage.record_human_review(b, "REJECTED", reviewer="ada", note="no")

    rows = read_csv(filters(vendor=VENDOR, event="REJECTED"))[1:]
    assert len(rows) == 1
    assert VENDOR in rows[0]
    assert ACME not in "".join(rows[0])


def test_the_export_never_carries_a_credential_a_key_or_invoice_content(db):
    """A CSV leaves the application, and the authorization boundary with it."""
    run_id, _ = submit(1000.0, "INV-LEAK")
    add_document(run_id, "MANUAL_UPLOAD")
    make_message("sha-leak", sender="leak@leak.example", subject="LEAK-SUBJECT")

    text = "".join(logs.export_csv(filters()))
    for forbidden in ("storage_key", "deadbeef", "password", "token", "secret",
                      "audit_json", "extracted_json", "sha256",
                      "leak@leak.example", "LEAK-SUBJECT", "auth_json"):
        assert forbidden not in text, f"{forbidden} reached the export"


@pytest.mark.parametrize("hostile", [
    "=1+1",
    "+1+1",
    "@SUM(A1:A9)",
    "=cmd|' /C calc'!A0",
    "-2+3+cmd|' /C calc'!A0",
])
def test_a_formula_shaped_value_is_neutralised_in_the_export(db, hostile):
    """A cell beginning `=` is executed on open by Excel and by Sheets, so a
    review note typed as a formula becomes live content in whoever opens the
    file."""
    run_id, _ = submit(99999.0, "INV-CSVI")
    storage.add_comment(run_id, "ada", hostile)

    rows = read_csv(filters(event="COMMENT_ADDED"))[1:]
    summary = rows[0][-1]
    assert summary.startswith("'"), "the formula was written as a formula"
    assert summary == "'" + hostile, "the value itself was altered"


def test_a_negative_amount_is_not_corrupted_by_the_formula_guard(db):
    """The naive fix breaks ordinary data: `-1250.00` starts with `-`, and
    prefixing it produces text that no longer sums."""
    assert logs.csv_safe("-1250.00") == "-1250.00"
    assert logs.csv_safe("1250.00") == "1250.00"
    assert logs.csv_safe(-1250.0) == "-1250.0"
    assert logs.csv_safe("Acme Ltd") == "Acme Ltd"
    assert logs.csv_safe(None) == ""
    assert logs.csv_safe("=1+1") == "'=1+1"


def test_a_comma_or_quote_in_a_value_stays_one_cell(db):
    """csv.writer's own quoting, not hand-built strings."""
    run_id, _ = submit(99999.0, "INV-COMMA")
    storage.add_comment(run_id, "ada", 'held, pending "the PO", per Ada')
    rows = read_csv(filters(event="COMMENT_ADDED"))[1:]
    assert rows[0][-1] == 'held, pending "the PO", per Ada'
    assert len(rows[0]) == len(logs.EXPORT_COLUMNS)


def test_a_large_export_is_produced_in_chunks_and_stays_complete(db):
    """The generator yields repeatedly rather than building one string, and
    the row count must survive the chunking."""
    for i in range(12):
        submit(1000.0 + i, f"INV-BIG{i}")
    expected = total_of()

    chunks = list(logs.export_csv(filters()))
    assert len(chunks) >= 2, "nothing was streamed"
    rows = list(csv.reader(io.StringIO("".join(chunks))))
    assert len(rows) - 1 == expected


def test_an_export_past_the_cap_says_it_was_truncated(db):
    """Rows past the cap are not silently dropped."""
    for i in range(4):
        submit(1000.0 + i, f"INV-CAP{i}")
    rows = read_csv(filters(), max_rows=3)
    assert len(rows) == 1 + 3 + 1                     # header + rows + notice
    assert rows[-1][0].startswith("# truncated at 3 rows")


def test_a_complete_export_does_not_claim_to_be_truncated(db):
    submit(1000.0, "INV-WHOLE")
    rows = read_csv(filters())
    assert not any(r and r[0].startswith("# truncated") for r in rows)


def test_the_export_filename_is_stamped_and_is_a_csv(db):
    name = logs.export_filename()
    assert name.startswith("activity-log-") and name.endswith(".csv")
    assert "Z.csv" in name, "the stamp says which timezone it is in"


# ==========================================================================
# 10. authorization (service level)
# ==========================================================================

def test_grouping_by_actor_shows_an_administrator_the_whole_team(db):
    run_id, _ = submit(99999.0, "INV-AUTH1")
    storage.add_comment(run_id, "ada", "mine")
    storage.add_comment(run_id, "bob", "mine too")

    result = logs.group(filters(event="COMMENT_ADDED"), "actor",
                        viewer="ada", see_everyone=True)
    assert result["scope"] == "all"
    assert {g["key"] for g in result["groups"]} == {"ada", "bob"}


def test_grouping_by_actor_shows_everyone_else_only_themselves(db):
    """The same rule /api/analytics/users already applies (7c.5): the one
    view here that is a report about PEOPLE rather than about invoices."""
    run_id, _ = submit(99999.0, "INV-AUTH2")
    storage.add_comment(run_id, "ada", "mine")
    storage.add_comment(run_id, "bob", "mine too")

    result = logs.group(filters(event="COMMENT_ADDED"), "actor",
                        viewer="ada", see_everyone=False)
    assert result["scope"] == "self"
    assert {g["key"] for g in result["groups"]} == {"ada"}


def test_the_actor_parameter_cannot_widen_what_a_non_admin_sees(db):
    """Asking about a colleague returns YOUR row, not theirs. The filter is
    REPLACED, not added to -- a filter the caller supplies must never be the
    thing that decides what they may see."""
    run_id, _ = submit(99999.0, "INV-AUTH3")
    storage.add_comment(run_id, "ada", "mine")
    storage.add_comment(run_id, "bob", "bob's")

    result = logs.group(filters(actor="bob", event="COMMENT_ADDED"), "actor",
                        viewer="ada", see_everyone=False)
    assert result["scope"] == "self"
    assert {g["key"] for g in result["groups"]} == {"ada"}
    assert "bob" not in json.dumps(result["groups"])


def test_a_non_admin_grouping_by_something_else_is_unrestricted(db):
    """Only the per-person axis is gated. Grouping by event is a view of
    invoice history, which `invoice:read` already reaches one run at a time."""
    submit(1000.0, "INV-AUTH4")
    result = logs.group(filters(), "event", viewer="ada", see_everyone=False)
    assert result["scope"] == "all"


def test_restricting_the_actor_does_not_mutate_the_caller_s_filters(db):
    """The echoed filters must still describe what was asked for."""
    f = filters(actor="bob")
    logs.group(f, "actor", viewer="ada", see_everyone=False)
    assert f.actor == "bob"


# ==========================================================================
# 11. authorization and behaviour over HTTP
# ==========================================================================

def no_scope_headers():
    """A token for a role that grants nothing -- the only way to be
    authenticated and still lack `invoice:read`, since all four demo roles
    carry it."""
    token = auth.create_access_token({"username": "nobody", "roles": ["nobody"]})
    return {"Authorization": "Bearer " + token["access_token"]}


@pytest.mark.parametrize("path", ["/api/logs", "/api/logs/facets",
                                  "/api/logs/export", "/api/logs/invoice/1"])
def test_every_log_endpoint_refuses_an_unauthenticated_caller(client, path):
    assert client.get(path, headers={"Authorization": ""}).status_code == 401


@pytest.mark.parametrize("path", ["/api/logs", "/api/logs/facets",
                                  "/api/logs/export", "/api/logs/invoice/1"])
def test_every_log_endpoint_requires_invoice_read(client, path):
    r = client.get(path, headers=no_scope_headers())
    assert r.status_code == 403
    assert "invoice:read" in r.json()["detail"]


def test_the_export_is_gated_exactly_as_the_list_is(client):
    """Not "also gated" -- IDENTICALLY gated. An export that any authenticated
    caller could reach is the accidental leak this phase is most likely to
    ship."""
    submit(1000.0, "INV-HX")
    for headers, expected in (({"Authorization": ""}, 401),
                              (no_scope_headers(), 403)):
        assert client.get("/api/logs", headers=headers).status_code == expected
        assert client.get("/api/logs/export", headers=headers).status_code == expected


def test_a_reader_can_list_the_log(client):
    submit(1000.0, "INV-VIEWER")
    r = client.get("/api/logs", headers=auth_headers("viewer", "vic"))
    assert r.status_code == 200
    assert r.json()["rows"]


def test_the_http_layer_reports_a_bad_filter_as_a_400(client):
    for query in ("?stream=nope", "?decision=MAYBE", "?order=sideways",
                  "?range=last-fortnight", "?group_by=password",
                  "?page=0", "?page_size=9999"):
        r = client.get("/api/logs" + query)
        assert r.status_code in (400, 422), f"{query} -> {r.status_code}"


def test_a_bad_range_reads_the_same_on_the_log_as_on_the_dashboard(client):
    """One parser, one message. Two would drift."""
    a = client.get("/api/logs?range=fortnight")
    b = client.get("/api/analytics/overview?range=fortnight")
    assert a.status_code == b.status_code == 400
    assert a.json()["detail"] == b.json()["detail"]


def test_the_list_endpoint_pages_over_http(client):
    for i in range(5):
        submit(1000.0 + i, f"INV-HP{i}")
    first = client.get("/api/logs?page=1&page_size=3").json()
    second = client.get("/api/logs?page=2&page_size=3").json()
    assert len(first["rows"]) == 3
    assert first["page"] == 1 and first["page_size"] == 3
    assert not ({r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]})


def test_the_list_endpoint_echoes_the_filters_it_applied(client):
    submit(1000.0, "INV-ECHO")
    body = client.get(f"/api/logs?vendor={VENDOR}&event=PROCESSING_COMPLETED").json()
    assert body["filters"]["vendor"] == VENDOR
    assert body["filters"]["event"] == "PROCESSING_COMPLETED"
    assert body["filters"]["range"]["timezone"] == "UTC"


def test_the_group_view_is_reached_by_a_query_parameter(client):
    submit(1000.0, "INV-HG")
    body = client.get("/api/logs?group_by=event").json()
    assert body["group_by"] == "event"
    assert body["groups"]


def test_a_reviewer_grouping_by_actor_over_http_sees_only_themselves(client):
    """The authorization decision is made from the authenticated principal --
    a `?actor=` in the query string cannot perform it on the caller's behalf."""
    run_id, _ = submit(99999.0, "INV-HA")
    storage.add_comment(run_id, "ada", "mine")
    storage.add_comment(run_id, "zoe", "zoe's")

    r = client.get("/api/logs?group_by=actor&actor=zoe",
                   headers=auth_headers("reviewer", "ada"))
    body = r.json()
    assert body["scope"] == "self"
    assert {g["key"] for g in body["groups"]} == {"ada"}

    admin = client.get("/api/logs?group_by=actor",
                       headers=auth_headers("admin", "root")).json()
    assert admin["scope"] == "all"
    assert {"ada", "zoe"} <= {g["key"] for g in admin["groups"]}


def test_the_export_endpoint_returns_a_downloadable_csv(client):
    submit(1000.0, "INV-DL")
    r = client.get("/api/logs/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert ".csv" in r.headers["content-disposition"]
    body = r.text
    assert body.startswith("Timestamp (UTC),Stream,Event")
    assert "INV-DL" in body


def test_the_export_endpoint_respects_the_query_string(client):
    a, _ = submit(99999.0, "INV-QA", vendor=VENDOR, po=PO)
    submit(500.0, "INV-QB", vendor=ACME, po=ACME_PO)
    body = client.get(f"/api/logs/export?vendor={ACME}").text
    assert "INV-QB" in body
    assert "INV-QA" not in body


def test_the_facets_endpoint_offers_what_the_database_contains(client):
    run_id, _ = submit(99999.0, "INV-FAC")
    storage.add_comment(run_id, "ada", "note")
    body = client.get("/api/logs/facets").json()

    assert VENDOR in [v["value"] for v in body["vendors"]]
    assert "ada" in [a["value"] for a in body["actors"]]
    assert "COMMENT_ADDED" in [e["value"] for e in body["events"]]
    assert PO in [p["value"] for p in body["purchase_orders"]]
    assert "PO remaining check" in [r["value"] for r in body["rules_failed"]]
    assert body["system_actor"] == logs.SYSTEM_ACTOR
    assert {"event", "actor", "vendor"} <= {g["value"] for g in body["groupings"]}


def test_facets_offer_the_system_option_only_when_it_would_match(client):
    """A filter option that returns nothing is worse than no option."""
    assert client.get("/api/logs/facets").json()["system_actor"] is None


def test_the_detail_endpoint_returns_one_event(client):
    run_id, _ = submit(1000.0, "INV-HD")
    aid = activity_ids(run_id)[0]
    body = client.get(f"/api/logs/invoice/{aid}").json()
    assert body["event_id"] == aid
    assert body["run_id"] == run_id


def test_the_detail_endpoint_404s_on_an_unknown_event(client):
    assert client.get("/api/logs/invoice/424242").status_code == 404


def test_the_detail_endpoint_refuses_an_unknown_stream(client):
    assert client.get("/api/logs/runs/1").status_code == 400


# ==========================================================================
# 12. the log is read-only
# ==========================================================================

def snapshot():
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            out = {}
            for table in ("runs", "invoice_activity", "email_activity",
                          "email_messages", "review_claims", "run_allocations",
                          "documents"):
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                out[table] = cur.fetchone()["n"]
            cur.execute("SELECT id, status, automated_decision, human_decision, "
                        "final_decision FROM runs ORDER BY id")
            out["runs_detail"] = [dict(r) for r in cur.fetchall()]
        return out
    finally:
        conn.close()


def test_nothing_under_api_logs_writes_anything(client):
    """There is no logs table and no event mirror; a read path that wrote
    would be the beginning of one."""
    run_id, _ = submit(99999.0, "INV-RO")
    storage.add_comment(run_id, "ada", "note")
    make_message("sha-ro")
    before = snapshot()

    aid = activity_ids(run_id)[0]
    for path in ("/api/logs", "/api/logs?group_by=event", "/api/logs?group_by=actor",
                 "/api/logs/facets", "/api/logs/export",
                 f"/api/logs/invoice/{aid}",
                 "/api/logs?q=note&vendor=" + VENDOR):
        assert client.get(path).status_code == 200

    assert snapshot() == before


@pytest.mark.parametrize("path", ["/api/logs", "/api/logs/facets", "/api/logs/export"])
def test_the_log_endpoints_reject_a_post(client, path):
    assert client.post(path).status_code == 405


# ==========================================================================
# 13. schema
# ==========================================================================

def test_phase_i_adds_one_index_and_no_table(db):
    """The log is a query. The only thing it needs from the schema is for the
    column every one of its queries filters on to be indexed on both sides --
    invoice_activity(created_at) already existed, email_activity(created_at)
    did not."""
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname=%s",
                        (storage.PG_SCHEMA,))
            indexes = {r["indexname"] for r in cur.fetchall()}
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s",
                        (storage.PG_SCHEMA,))
            tables = {r["tablename"] for r in cur.fetchall()}
    finally:
        conn.close()

    assert "idx_email_activity_created_at" in indexes
    assert not {t for t in tables if "log" in t.lower()}, \
        "Phase I must not add a logs table"
    assert tables <= {
        "purchase_orders", "vendors", "runs", "run_allocations", "documents",
        "invoice_activity", "review_claims", "trusted_email_senders",
        "email_messages", "email_activity", "email_attachments",
        "extraction_quota",
    }


# ==========================================================================
# 14. edge cases
# ==========================================================================

def test_a_run_with_no_vendor_or_po_still_produces_readable_rows(db):
    """Null context is normal -- an unreadable scan extracts nothing."""
    extracted = {"vendor_name": None, "invoice_number": None, "total": None,
                 "po_references": [], "extraction_method": "none"}
    run_id, _, _ = storage.save_run_checked(
        "blank.pdf", "NEEDS_REVIEW", extracted, {"matched": False},
        [], [{"text": "Nothing could be read.", "level": "fail"}],
        tolerance_for=matching.tolerance_for, audit={}, uploaded_by="analyst-1")

    rows = rows_of(run_id=run_id)
    assert rows
    assert rows[0]["vendor"] is None
    assert rows[0]["invoice_number"] is None
    assert rows[0]["po_number"] is None


def test_a_run_with_no_stored_document_reports_a_null_source(db):
    """"We did not record how this arrived" is not "it arrived manually"."""
    run_id, _ = submit(1000.0, "INV-NODOC")
    assert rows_of(run_id=run_id)[0]["source"] is None


def test_an_empty_search_term_is_no_filter_at_all(db):
    submit(1000.0, "INV-BLANK")
    assert total_of(search="   ") == total_of()
    assert total_of(search="") == total_of()


def test_a_filter_for_something_absent_returns_nothing(db):
    submit(1000.0, "INV-ABSENT")
    assert rows_of(vendor="No Such Vendor Ltd") == []
    assert rows_of(run_id=999999) == []
    assert rows_of(invoice_number="INV-NEVER") == []
    assert rows_of(po_number="PO-9999") == []


def test_the_log_survives_a_run_whose_history_spans_a_reversal(db):
    """An admin override leaves its own event, and the run's rows must still
    read correctly afterwards."""
    run_id, _ = submit(99999.0, "INV-REV")
    storage.record_human_review(run_id, "ACCEPTED", reviewer="ada")
    storage.set_run_status(run_id, "REJECTED", note="reversed")
    storage.log_activity(run_id, "STATUS_OVERRIDDEN", actor="root",
                         note="reversed")

    rows = rows_of(run_id=run_id)
    events = [r["event"] for r in rows]
    assert "STATUS_OVERRIDDEN" in events
    assert "ACCEPTED" in events
    assert {r["decision"] for r in rows} == {"NEEDS_REVIEW"}
    assert {r["status"] for r in rows} == {"REJECTED"}


# ==========================================================================
# 15. the per-run stage log
#
# The third history Phase I opens up, and the one that is not an event stream:
# `runs.stages_json` is a JSON array on the run, so it gets its own view
# (`logs.stage_rows`) over the same filters. What can go wrong here is
# specific: a stage log shown out of order (which misrepresents what the
# pipeline did), an unmeasured stage averaged in as zero, a filter the view
# cannot apply being ignored rather than refused, and one malformed blob
# taking the page down.
# ==========================================================================

def stage_rows_of(page=1, page_size=logs.MAX_PAGE_SIZE, stage=None,
                  stage_status=None, **kwargs):
    return logs.stage_rows(filters(**kwargs), page, page_size,
                           stage=stage, stage_status=stage_status)


def test_the_stage_view_returns_one_row_per_stage(db):
    run_id, _ = submit(99999.0, "INV-ST1", stages=PIPELINE_STAGES)
    result = stage_rows_of(run_id=run_id)

    assert result["total"] == len(PIPELINE_STAGES)
    assert [r["stage"] for r in result["rows"]] == \
        [s["name"] for s in PIPELINE_STAGES]
    assert [r["seq"] for r in result["rows"]] == [1, 2, 3, 4, 5]


def test_a_stage_row_carries_the_invoice_it_belongs_to(db):
    """A stage line reading "PO_MATCH failed" with no indication of which
    invoice is not a log, it is a riddle -- the same reasoning the activity
    rows are joined to their subject for."""
    run_id, _ = submit(99999.0, "INV-ST2", stages=PIPELINE_STAGES)
    row = stage_rows_of(run_id=run_id, stage="PO_MATCH")["rows"][0]

    assert row["run_id"] == run_id
    assert row["invoice_number"] == "INV-ST2"
    assert row["vendor"] == VENDOR
    assert row["filename"] == "INV-ST2.pdf"
    assert row["decision"] == "NEEDS_REVIEW"
    assert row["run_status"] == "NEEDS_REVIEW"
    assert row["detail"] == "No PO could be matched."
    assert row["stage_status"] == "fail"
    assert row["ms"] == 7


def test_the_stage_log_is_always_read_in_the_order_it_was_written(db):
    """Whichever direction the RUNS are ordered in. A run read backwards would
    show DECISION before INGEST, which is not a sort order -- it is a false
    account of what happened."""
    run_id, _ = submit(99999.0, "INV-ST3", stages=PIPELINE_STAGES)
    for order in ("desc", "asc"):
        rows = stage_rows_of(run_id=run_id, order=order)["rows"]
        assert [r["stage"] for r in rows] == [s["name"] for s in PIPELINE_STAGES]


def test_the_runs_themselves_honour_the_requested_order(db):
    first, _ = submit(1000.0, "INV-ST4A", stages=PIPELINE_STAGES)
    second, _ = submit(1100.0, "INV-ST4B", stages=PIPELINE_STAGES)

    newest = stage_rows_of(order="desc")["rows"][0]["run_id"]
    oldest = stage_rows_of(order="asc")["rows"][0]["run_id"]
    assert (newest, oldest) == (second, first)


def test_filtering_by_stage_name_returns_only_that_stage(db):
    submit(99999.0, "INV-ST5", stages=PIPELINE_STAGES)
    result = stage_rows_of(stage="VENDOR_CHECK")
    assert result["total"] == 1
    assert result["rows"][0]["stage"] == "VENDOR_CHECK"


def test_a_stage_name_is_matched_case_insensitively(db):
    submit(99999.0, "INV-ST6", stages=PIPELINE_STAGES)
    assert stage_rows_of(stage="vendor_check")["total"] == 1


def test_filtering_by_stage_status_finds_the_failures_across_runs(db):
    """The question this view exists for: which invoices failed at this stage,
    which neither `GET /api/runs/{id}` nor the analytics aggregate can
    answer."""
    a, _ = submit(99999.0, "INV-ST7A", stages=PIPELINE_STAGES)
    b, _ = submit(99998.0, "INV-ST7B", stages=PIPELINE_STAGES)
    submit(1000.0, "INV-ST7C")                       # one INGEST stage, ok

    result = stage_rows_of(stage="PO_MATCH", stage_status="fail")
    assert {r["run_id"] for r in result["rows"]} == {a, b}
    assert all(r["stage_status"] == "fail" for r in result["rows"])


def test_a_stage_nothing_recorded_matches_nothing_rather_than_everything(db):
    submit(99999.0, "INV-ST8", stages=PIPELINE_STAGES)
    assert stage_rows_of(stage="NO_SUCH_STAGE")["total"] == 0


def test_the_stage_view_narrows_by_the_same_run_filters_as_the_log(db):
    a, _ = submit(99999.0, "INV-ST9A", vendor=VENDOR, po=PO,
                  stages=PIPELINE_STAGES)
    submit(1000.0, "INV-ST9B", vendor=ACME, po=ACME_PO, stages=PIPELINE_STAGES)

    for narrowing in ({"vendor": VENDOR}, {"invoice_number": "INV-ST9A"},
                      {"po_number": PO}, {"run_id": a}):
        result = stage_rows_of(**narrowing)
        assert {r["run_id"] for r in result["rows"]} == {a}, narrowing


def test_the_stage_view_narrows_by_decision_and_status(db):
    held, _ = submit(99999.0, "INV-ST10A", stages=PIPELINE_STAGES)
    approved, _ = submit(1000.0, "INV-ST10B", stages=PIPELINE_STAGES)

    assert {r["run_id"] for r in stage_rows_of(decision="NEEDS_REVIEW")["rows"]} \
        == {held}
    assert {r["run_id"] for r in stage_rows_of(status="APPROVED")["rows"]} \
        == {approved}


def test_the_stage_view_narrows_by_the_rule_that_failed(db):
    """Reusing `runs_failing_rule`, so a rule means the same thing on both
    views rather than being resolved twice."""
    over, _ = submit(99999.0, "INV-ST11A", stages=PIPELINE_STAGES)
    submit(1000.0, "INV-ST11B", stages=PIPELINE_STAGES)

    result = stage_rows_of(rule_failed="PO remaining check")
    assert {r["run_id"] for r in result["rows"]} == {over}


def test_the_stage_view_narrows_by_source(db):
    a, _ = submit(1000.0, "INV-ST12A", stages=PIPELINE_STAGES)
    b, _ = submit(1100.0, "INV-ST12B", stages=PIPELINE_STAGES)
    add_document(a, "EMAIL")
    add_document(b, "MANUAL_UPLOAD")

    assert {r["run_id"] for r in stage_rows_of(source="EMAIL")["rows"]} == {a}


def test_the_stage_view_windows_on_when_the_INVOICE_arrived(db):
    """A stage has no timestamp of its own -- it is what happened during the
    run -- so the run's arrival time is the only honest thing to window on,
    and it is the column the analytics endpoints already window on."""
    run_id, _ = submit(99999.0, "INV-ST13", stages=PIPELINE_STAGES)
    with storage.write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET created_at=%s WHERE id=%s",
                        (stamp(days_ago=90), run_id))

    assert stage_rows_of(range="30d")["total"] == 0
    assert stage_rows_of(range="all")["total"] == len(PIPELINE_STAGES)


@pytest.mark.parametrize("conflicting", [
    {"actor": "ada"},
    {"event": "REJECTED"},
    {"email_status": "QUARANTINED"},
    {"stream": "email"},
])
def test_the_stage_view_refuses_a_filter_it_cannot_apply(db, conflicting):
    """Not ignored (which would return rows the caller did not ask for and
    look like it had filtered them), and not answered with an empty page
    (which would read as "these runs have no stages")."""
    submit(99999.0, "INV-ST14", stages=PIPELINE_STAGES)
    with pytest.raises(logs.LogError) as exc:
        stage_rows_of(**conflicting)
    named = list(conflicting)[0]
    assert named in str(exc.value) or "stream=email" in str(exc.value)


def test_an_invalid_stage_status_is_refused_rather_than_ignored(db):
    submit(99999.0, "INV-ST15", stages=PIPELINE_STAGES)
    with pytest.raises(logs.LogError):
        stage_rows_of(stage_status="probably-fine")


def test_a_blank_stage_filter_is_no_filter_at_all(db):
    submit(99999.0, "INV-ST16", stages=PIPELINE_STAGES)
    everything = stage_rows_of()["total"]
    assert stage_rows_of(stage="  ", stage_status="")["total"] == everything


def test_a_malformed_stage_blob_is_skipped_counted_and_not_fatal(db):
    """One bad row must not take the view down -- the same guarded parse
    Phase H uses (7c.2)."""
    good, _ = submit(99999.0, "INV-ST17A", stages=PIPELINE_STAGES)
    bad, _ = submit(99998.0, "INV-ST17B", stages=PIPELINE_STAGES)
    with storage.write_txn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET stages_json='{not json' WHERE id=%s",
                        (bad,))

    result = stage_rows_of()
    assert {r["run_id"] for r in result["rows"]} == {good}
    assert result["data_quality"]["malformed_stages"] == 1


def test_valid_json_of_the_wrong_shape_is_skipped_too(db):
    """A list of strings, an object, a number -- all parse, none is a stage
    log."""
    run_id, _ = submit(99999.0, "INV-ST18", stages=PIPELINE_STAGES)
    for blob in ('["INGEST", "DECISION"]', '{"name": "INGEST"}', '5',
                 '[{"status": "ok"}]', '[null]'):
        with storage.write_txn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE runs SET stages_json=%s WHERE id=%s",
                            (blob, run_id))
        assert stage_rows_of()["total"] == 0, blob


def test_an_unmeasured_stage_reports_null_milliseconds_not_zero(db):
    """Zero would say the stage took no time. It says nothing was recorded --
    the distinction Phase H's timing block makes for the same values."""
    run_id, _ = submit(99999.0, "INV-ST19", stages=[
        {"name": "INGEST", "status": "ok", "detail": "no timing here"},
        {"name": "DECISION", "status": "ok", "ms": True},
    ])
    rows = stage_rows_of(run_id=run_id)["rows"]
    assert [r["ms"] for r in rows] == [None, None], \
        "an absent ms, and a boolean, both read as unmeasured"


def test_a_stage_with_no_status_reports_null_rather_than_ok(db):
    run_id, _ = submit(99999.0, "INV-ST20",
                       stages=[{"name": "INGEST", "ms": 4}])
    row = stage_rows_of(run_id=run_id)["rows"][0]
    assert row["stage_status"] is None
    assert row["detail"] is None


def test_a_run_with_no_stage_log_contributes_nothing_and_does_not_raise(db):
    run_id, _ = submit(99999.0, "INV-ST21", stages=[])
    assert stage_rows_of(run_id=run_id)["total"] == 0


def test_the_stage_view_pages_without_dropping_or_repeating_a_row(db):
    for i in range(3):
        submit(99990.0 + i, f"INV-ST22{i}", stages=PIPELINE_STAGES)
    expected = stage_rows_of()["total"]
    assert expected == 15

    seen, page = [], 1
    while True:
        result = stage_rows_of(page=page, page_size=4)
        seen.extend(r["id"] for r in result["rows"])
        if not result["has_more"]:
            break
        page += 1
        assert page < 20, "paging did not terminate"

    assert len(seen) == expected
    assert len(set(seen)) == expected, "a row was shown on two pages"


def test_the_stage_row_id_is_stable_and_unique(db):
    a, _ = submit(99999.0, "INV-ST23A", stages=PIPELINE_STAGES)
    b, _ = submit(99998.0, "INV-ST23B", stages=PIPELINE_STAGES)
    ids = [r["id"] for r in stage_rows_of()["rows"]]
    assert len(set(ids)) == len(ids)
    assert f"{a}:1" in ids and f"{b}:1" in ids


def test_search_over_the_stage_view_matches_the_name_and_the_detail(db):
    run_id, _ = submit(99999.0, "INV-ST24", stages=PIPELINE_STAGES)
    assert stage_rows_of(search="VENDOR_CHECK")["total"] == 1
    assert stage_rows_of(search="no po could be matched")["total"] == 1
    assert stage_rows_of(search="INV-ST24")["total"] == len(PIPELINE_STAGES)
    assert stage_rows_of(search="nothing here matches")["total"] == 0


def test_a_metacharacter_is_an_ordinary_character_on_the_stage_view(db):
    """Matched in Python, on values already parsed, so no LIKE pattern exists
    for `%` or `_` to be a metacharacter in."""
    submit(99999.0, "INV-ST25", stages=[
        {"name": "DECISION", "status": "ok", "detail": "held at 100% of the PO",
         "ms": 2},
    ])
    assert stage_rows_of(search="100% of")["total"] == 1
    assert stage_rows_of(search="%")["total"] == 1
    assert stage_rows_of(search="DECISION")["total"] == 1
    assert stage_rows_of(search="DECISIO_")["total"] == 0, \
        "`_` stood in for a character"


def test_the_stage_view_echoes_the_filters_it_applied(db):
    submit(99999.0, "INV-ST26", stages=PIPELINE_STAGES)
    described = stage_rows_of(stage="po_match", stage_status="fail",
                              vendor=VENDOR)["filters"]
    assert described["stage"] == "PO_MATCH"
    assert described["stage_status"] == "fail"
    assert described["vendor"] == VENDOR


def test_the_stage_view_leaks_nothing_the_activity_log_withholds(db):
    run_id, _ = submit(99999.0, "INV-ST27", stages=PIPELINE_STAGES)
    add_document(run_id, "MANUAL_UPLOAD")
    body = json.dumps(stage_rows_of())
    for forbidden in ("storage_key", "deadbeef", "audit_json", "extracted_json",
                      "rules_failed", "provenance", "raw_text"):
        assert forbidden not in body, f"{forbidden} reached the stage view"


def test_the_stage_view_writes_nothing(db):
    """Read-only, asserted the way Phase H asserts it: snapshot, call,
    compare."""
    run_id, _ = submit(99999.0, "INV-ST28", stages=PIPELINE_STAGES)
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, automated_decision, stages_json "
                        "FROM runs WHERE id=%s", (run_id,))
            before = dict(cur.fetchone())
            cur.execute("SELECT COUNT(*) AS n FROM invoice_activity")
            events_before = cur.fetchone()["n"]
    finally:
        conn.close()

    stage_rows_of()
    stage_rows_of(stage="PO_MATCH", stage_status="fail")
    logs.stage_vocabulary(window())
    list(logs.export_stages_csv(filters()))

    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, automated_decision, stages_json "
                        "FROM runs WHERE id=%s", (run_id,))
            assert dict(cur.fetchone()) == before
            cur.execute("SELECT COUNT(*) AS n FROM invoice_activity")
            assert cur.fetchone()["n"] == events_before
    finally:
        conn.close()


# -- the stage export -------------------------------------------------------

def read_stage_csv(f, **kwargs):
    text = "".join(logs.export_stages_csv(f, **kwargs))
    return list(csv.reader(io.StringIO(text)))


def test_the_stage_export_has_a_header_row_even_when_nothing_matches(db):
    rows = read_stage_csv(filters())
    assert rows == [[h for _, h in logs.STAGE_EXPORT_COLUMNS]]


def test_the_stage_export_contains_exactly_the_rows_the_view_showed(db):
    submit(99999.0, "INV-SX1", stages=PIPELINE_STAGES)
    submit(99998.0, "INV-SX2", stages=PIPELINE_STAGES)
    f = filters()
    listed = logs.stage_rows(f, 1, logs.MAX_PAGE_SIZE)["rows"]
    exported = read_stage_csv(f)[1:]

    assert len(exported) == len(listed)
    assert [r[6] for r in exported] == [r["stage"] for r in listed]


def test_the_stage_export_respects_every_active_filter(db):
    a, _ = submit(99999.0, "INV-SX3", vendor=VENDOR, po=PO,
                  stages=PIPELINE_STAGES)
    submit(1000.0, "INV-SX4", vendor=ACME, po=ACME_PO, stages=PIPELINE_STAGES)

    rows = read_stage_csv(filters(vendor=VENDOR), stage="PO_MATCH")[1:]
    assert len(rows) == 1
    assert rows[0][1] == str(a)
    assert ACME not in "".join(rows[0])


def test_the_stage_export_refuses_a_filter_the_view_refuses(db):
    """Validated on the first pull rather than mid-download, so a bad filter
    is a 400 and never a broken CSV behind a 200."""
    submit(99999.0, "INV-SX5", stages=PIPELINE_STAGES)
    with pytest.raises(logs.LogError):
        list(logs.export_stages_csv(filters(actor="ada")))


def test_a_formula_shaped_stage_detail_is_neutralised(db):
    """The detail line embeds a filename the uploader chose, so a PDF named
    `=cmd|...` must arrive as text."""
    submit(99999.0, "INV-SX6", stages=[
        {"name": "INGEST", "status": "ok", "ms": 1,
         "detail": "=cmd|' /C calc'!A0"},
    ])
    row = read_stage_csv(filters())[1]
    assert row[-1] == "'=cmd|' /C calc'!A0"


def test_a_stage_export_past_the_cap_says_it_was_truncated(db):
    submit(99999.0, "INV-SX7", stages=PIPELINE_STAGES)
    rows = read_stage_csv(filters(), max_rows=3)
    assert len(rows) == 1 + 3 + 1
    assert rows[-1][0].startswith("# truncated at 3 rows")


def test_a_complete_stage_export_does_not_claim_to_be_truncated(db):
    submit(99999.0, "INV-SX8", stages=PIPELINE_STAGES)
    rows = read_stage_csv(filters())
    assert not any(r and r[0].startswith("# truncated") for r in rows)


def test_the_stage_export_carries_no_credential_key_or_invoice_content(db):
    run_id, _ = submit(99999.0, "INV-SX9", stages=PIPELINE_STAGES)
    add_document(run_id, "MANUAL_UPLOAD")
    text = "".join(logs.export_stages_csv(filters()))
    for forbidden in ("storage_key", "deadbeef", "password", "token", "secret",
                      "audit_json", "extracted_json"):
        assert forbidden not in text, f"{forbidden} reached the export"


def test_the_stage_export_filename_says_what_it_is(db):
    name = logs.export_filename("stage-log")
    assert name.startswith("stage-log-") and name.endswith(".csv")


# -- facets and HTTP --------------------------------------------------------

def test_the_facets_offer_the_stages_that_actually_ran(db):
    submit(99999.0, "INV-SF1", stages=PIPELINE_STAGES)
    panel = logs.facets(window())
    assert {s["value"] for s in panel["stages"]} == \
        {s["name"] for s in PIPELINE_STAGES}
    assert {s["value"] for s in panel["stage_statuses"]} == {"ok", "warn", "fail"}


def test_the_stage_facets_are_empty_rather_than_absent_on_a_quiet_period(db):
    panel = logs.facets(window())
    assert panel["stages"] == []
    assert panel["stage_statuses"] == []


def test_the_stage_view_is_reachable_over_http(client):
    submit(99999.0, "INV-SH1", stages=PIPELINE_STAGES)
    r = client.get("/api/logs/stages?stage=PO_MATCH&stage_status=fail")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["rows"][0]["stage"] == "PO_MATCH"


def test_the_stage_export_is_downloadable_over_http(client):
    submit(99999.0, "INV-SH2", stages=PIPELINE_STAGES)
    r = client.get("/api/logs/stages/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "stage-log-" in r.headers["content-disposition"]
    assert "PO_MATCH" in r.text


@pytest.mark.parametrize("path", ["/api/logs/stages", "/api/logs/stages/export"])
def test_the_stage_endpoints_refuse_an_unauthenticated_caller(client, path):
    assert client.get(path, headers={"Authorization": ""}).status_code == 401


@pytest.mark.parametrize("path", ["/api/logs/stages", "/api/logs/stages/export"])
def test_the_stage_endpoints_require_invoice_read(client, path):
    r = client.get(path, headers=no_scope_headers())
    assert r.status_code == 403
    assert "invoice:read" in r.json()["detail"]


def test_the_stage_export_is_gated_exactly_as_the_stage_view_is(client):
    submit(99999.0, "INV-SH3", stages=PIPELINE_STAGES)
    for headers, expected in (({"Authorization": ""}, 401),
                              (no_scope_headers(), 403)):
        assert client.get("/api/logs/stages",
                          headers=headers).status_code == expected
        assert client.get("/api/logs/stages/export",
                          headers=headers).status_code == expected


@pytest.mark.parametrize("query", ["?stage_status=probably-fine",
                                   "?actor=ada", "?event=REJECTED",
                                   "?stream=email", "?stage=" + "x" * 200,
                                   "?range=last-fortnight", "?page=0"])
def test_the_stage_endpoint_reports_a_bad_filter_as_a_400(client, query):
    r = client.get("/api/logs/stages" + query)
    assert r.status_code in (400, 422), f"{query} -> {r.status_code}"


def test_a_bad_stage_filter_fails_the_export_before_any_bytes_are_sent(client):
    """A 400, not a 200 carrying half a CSV and an error nobody sees."""
    submit(99999.0, "INV-SH4", stages=PIPELINE_STAGES)
    r = client.get("/api/logs/stages/export?actor=ada")
    assert r.status_code == 400
    assert "actor" in r.json()["detail"]


def test_the_stage_endpoint_does_not_answer_a_write(client):
    assert client.post("/api/logs/stages").status_code == 405
    assert client.post("/api/logs/stages/export").status_code == 405
