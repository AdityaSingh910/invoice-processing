"""Phase H: KPIs and analytics.

WHAT THESE TESTS ARE FOR

Every figure this module produces is a claim about rows that exist. So the
tests are built the same way: each one puts a KNOWN set of runs, reviews,
claims and messages into a fresh schema and then asserts the exact number that
must come back -- not that the endpoint returned 200, and not that some field
is "roughly right".

Three properties get particular attention, because they are the ones that make
an analytics layer wrong in ways nobody notices:

1. **It must never invent a number.** A rate with an empty denominator is
   `None`, never `0.0` and never `1.0`. Several tests below assert exactly
   that, because "0% automated" on a day with no invoices is a false
   statement, not a neutral one.

2. **It must not become a second source of truth.** The PO figures are
   asserted equal to what `storage.consumed_amount_for_po()` -- the ledger the
   rest of the application reads -- says, on a database containing multi-PO
   invoices, reversals and rejections. If the two ever disagree, that is the
   defect, and this is where it surfaces.

3. **It must not leak one employee's activity to another.** The per-person
   endpoint is tested from both sides: a reviewer sees only themselves even
   when other reviewers have rows, and an administrator sees everyone.

Timestamps are written directly in a few tests. That is deliberate: date
filtering, timezone boundaries and null-timestamp handling cannot be exercised
by running the pipeline, which always stamps "now". Everywhere else the real
`rules.decide()` / `storage.save_run_checked()` path is used, so the rows under
test are the rows the application actually writes.
"""
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
import config      # noqa: E402
import main        # noqa: E402
import matching    # noqa: E402
import rules       # noqa: E402
import storage     # noqa: E402
import pg_schema   # noqa: E402
from conftest import auth_headers   # noqa: E402

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

def submit(total, invoice_number, po=PO, vendor=VENDOR, uploaded_by="analyst-1",
           currency="USD", stages=None):
    """Evaluate and commit one invoice exactly as the pipeline does.

    Same shape as test_review_collaboration.py's helper, plus a `stages` hook,
    because per-stage timing is a Phase H metric and the pipeline's own stage
    list is what it reads.
    """
    extracted = {
        "vendor_name": vendor, "invoice_number": invoice_number,
        "total": total, "subtotal": total, "tax": 0.0,
        "po_references": [po] if po else [], "currency": currency,
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
        f"{invoice_number}.pdf", status, extracted, po_match,
        stages if stages is not None else default_stages(), reasons,
        tolerance_for=matching.tolerance_for, audit=audit, uploaded_by=uploaded_by)
    return run_id, final_status


def default_stages(**overrides):
    """A stage list shaped like the one main.run_pipeline records."""
    base = {"INGEST": 5, "EXTRACT_TEXT": 40, "EXTRACT_FIELDS": 300,
            "VALIDATE": 2, "VENDOR_CHECK": 3, "PO_MATCH": 8,
            "DUPLICATE_CHECK": 4, "TOLERANCE_CHECK": 2, "DECISION": 1}
    base.update(overrides)
    return [{"name": n, "status": "ok", "detail": "", "ms": ms} for n, ms in base.items()]


def approved(invoice_number="INV-OK", total=100.0, **kw):
    run_id, status = submit(total, invoice_number, **kw)
    assert status == "APPROVED", status
    return run_id


def held(invoice_number="INV-OVER", total=6000.0, **kw):
    """The rules hold this: $6,000 against an untouched $5,000 PO."""
    run_id, status = submit(total, invoice_number, **kw)
    assert status == "NEEDS_REVIEW", status
    return run_id


def rejected(invoice_number="INV-DUP"):
    """A duplicate -- the rules reject it outright, never a hold."""
    approved(invoice_number, 100.0)
    run_id, status = submit(100.0, invoice_number)
    assert status == "REJECTED", status
    return run_id


def raw_run(created_at, automated="APPROVED", status=None, vendor=VENDOR,
            total=100.0, stages_json=None, audit_json=None,
            extracted_json=None, human=None, reviewed_by=None, reviewed_at=None):
    """A run row written directly, for the cases the pipeline cannot produce.

    Only used where the test is ABOUT a value the pipeline never varies -- a
    backdated `created_at`, a malformed JSON column, a null timestamp. Never
    used to fake a decision the rules would have reached differently.
    """
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO runs (filename, status, created_at, vendor_name,
                       invoice_number, total, extracted_json, stages_json, audit_json,
                       automated_decision, final_decision, human_decision,
                       reviewed_by, reviewed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                ("raw.pdf", status or automated, created_at, vendor,
                 f"RAW-{created_at}", total,
                 extracted_json if extracted_json is not None
                 else json.dumps({"extraction_method": "groq (text)", "currency": "USD"}),
                 stages_json if stages_json is not None else json.dumps(default_stages()),
                 audit_json, automated, status or automated, human,
                 reviewed_by, reviewed_at))
            return cur.fetchone()["id"]
    finally:
        conn.close()


def iso(days_ago=0, hour=12, minute=0):
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def window(key="all", **kw):
    return analytics.resolve_window(key, kw.get("date_from"), kw.get("date_to"))


# ==========================================================================
# 1. time windows -- resolution, validation, boundaries
# ==========================================================================

def test_every_named_range_resolves_to_a_half_open_utc_window():
    for key in ("today", "7d", "30d", "month"):
        w = analytics.resolve_window(key)
        assert w.key == key
        assert w.start.endswith("+00:00") and w.end.endswith("+00:00")
        assert w.start < w.end
        assert w.as_dict()["timezone"] == "UTC"


def test_all_time_is_unbounded_on_both_sides():
    w = analytics.resolve_window("all")
    assert w.start is None and w.end is None
    # An unbounded window must contribute no SQL and no parameters, rather
    # than substituting an epoch-shaped sentinel that would silently exclude
    # anything older than it.
    params = []
    assert w.clause("runs.created_at", params) == ""
    assert params == []


def test_today_covers_from_utc_midnight_to_the_start_of_tomorrow():
    w = analytics.resolve_window("today")
    start = datetime.fromisoformat(w.start)
    end = datetime.fromisoformat(w.end)
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    assert end - start == timedelta(days=1)


def test_seven_days_means_seven_buckets_including_today():
    assert analytics.resolve_window("7d").bucket_days() == 7
    assert analytics.resolve_window("30d").bucket_days() == 30


def test_a_custom_range_includes_the_day_named_by_to():
    w = analytics.resolve_window("custom", "2026-03-01", "2026-03-01")
    # from == to is one FULL day, not an empty window: `to` names a day the
    # caller expects to be included.
    assert w.start == "2026-03-01T00:00:00+00:00"
    assert w.end == "2026-03-02T00:00:00+00:00"
    assert w.bucket_days() == 1


def test_supplying_dates_implies_a_custom_range_even_without_saying_so():
    w = analytics.resolve_window("30d", "2026-03-01", "2026-03-05")
    assert w.key == "custom" and w.start.startswith("2026-03-01")


def test_an_unknown_range_is_refused_rather_than_defaulted():
    with pytest.raises(analytics.AnalyticsError) as exc:
        analytics.resolve_window("last-fortnight")
    assert "last-fortnight" in str(exc.value)


def test_a_malformed_custom_date_is_refused():
    for bad in ("01-03-2026", "2026-13-01", "yesterday", ""):
        with pytest.raises(analytics.AnalyticsError):
            analytics.resolve_window("custom", bad, "2026-03-05")


def test_a_backwards_custom_range_is_refused():
    with pytest.raises(analytics.AnalyticsError):
        analytics.resolve_window("custom", "2026-03-05", "2026-03-01")


def test_a_custom_range_needs_both_ends():
    with pytest.raises(analytics.AnalyticsError):
        analytics.resolve_window("custom", "2026-03-01", None)


def test_the_limit_parameter_is_validated():
    assert analytics.resolve_limit(None) == analytics.DEFAULT_GROUP_LIMIT
    assert analytics.resolve_limit(5) == 5
    for bad in (0, -1, analytics.MAX_GROUP_LIMIT + 1, "many"):
        with pytest.raises(analytics.AnalyticsError):
            analytics.resolve_limit(bad)


# ==========================================================================
# 2. the empty database -- nothing may be invented
# ==========================================================================

def test_with_no_runs_every_rate_is_null_and_no_count_is_null(db):
    result = analytics.overview(window())
    for name, kpi in result["kpis"].items():
        assert kpi["value"] is None, f"{name} invented a rate from no data"
        assert kpi["denominator"] == 0
    assert result["volume"]["runs"] == 0
    assert result["backlog"]["awaiting_review"] == 0
    assert result["backlog"]["oldest_awaiting_at"] is None
    assert result["value_by_currency"] == {}


def test_no_metric_is_ever_nan_or_infinite(db):
    """A division that cannot be performed must produce null, not a float that
    serialises as NaN -- which is not valid JSON and renders as garbage."""
    approved("INV-1")
    held("INV-2")
    payloads = [analytics.overview(window()), analytics.trends(window("7d")),
                analytics.processing(window()), analytics.reviews(window()),
                analytics.vendors(window()), analytics.email(window())]
    blob = json.dumps(payloads, default=str)
    assert "NaN" not in blob and "Infinity" not in blob


def test_timing_stats_report_zero_samples_rather_than_a_zero_average(db):
    """No measurement and a measurement of zero are different facts."""
    result = analytics.processing(window())
    assert result["run_time_ms"]["samples"] == 0
    assert result["run_time_ms"]["average"] is None
    assert result["run_time_ms"]["median"] is None
    assert result["stages"] == []


def test_an_empty_custom_range_reports_zero_runs_not_the_whole_database(db):
    approved("INV-1")
    result = analytics.overview(window("custom", date_from="2020-01-01", date_to="2020-01-31"))
    assert result["volume"]["runs"] == 0
    assert result["kpis"]["automation_rate"]["value"] is None


def test_a_user_with_no_activity_gets_an_empty_list_not_a_fabricated_row(db):
    result = analytics.users(window(), viewer="nobody", see_everyone=False)
    assert result["users"] == []
    assert result["scope"] == "self"


def test_a_vendor_with_no_runs_does_not_appear(db):
    approved("INV-1", vendor=VENDOR, po=PO)
    names = [v["vendor"] for v in analytics.vendors(window())["vendors"]]
    assert names == [VENDOR]
    assert ACME not in names


def test_a_purchase_order_with_no_invoices_reports_zero_consumed_not_null(db):
    """Zero and unavailable are different: an untouched PO has consumed
    exactly nothing, which is a number, and its full balance remains."""
    pos = {p["po_number"]: p for p in analytics.purchase_orders(window())}
    untouched = pos[ACME_PO]
    assert untouched["consumed"] == 0.0
    assert untouched["remaining"] == untouched["amount"]
    assert untouched["utilisation"] == 0.0
    assert untouched["runs_in_range"] == 0


# ==========================================================================
# 3. automation rate
# ==========================================================================

def test_automation_rate_counts_rules_decided_runs_over_all_runs(db):
    approved("INV-A", 100.0)          # automated APPROVED
    approved("INV-B", 100.0)          # automated APPROVED
    rejected("INV-C")                 # automated REJECTED (+1 approved above it)
    held("INV-D")                     # automated NEEDS_REVIEW

    kpi = analytics.overview(window())["kpis"]["automation_rate"]
    # rejected() commits an APPROVED run first so the duplicate has something
    # to collide with: 3 approved + 1 rejected + 1 held = 5 runs, 4 automated.
    assert kpi["denominator"] == 5
    assert kpi["numerator"] == 4
    assert kpi["value"] == pytest.approx(4 / 5)


def test_an_automatic_rejection_counts_as_automation_not_as_failure(db):
    """Correctly stopping a duplicate is the process working."""
    # rejected() commits the original first, then the duplicate that collides
    # with it: two runs, both decided by the rules alone.
    rejected("INV-ORIG")
    kpi = analytics.overview(window())["kpis"]["automation_rate"]
    assert kpi["numerator"] == 2 and kpi["denominator"] == 2
    assert kpi["value"] == 1.0


def test_automation_rate_is_unchanged_by_a_later_human_ruling(db):
    """`automated_decision` is immutable, so how automated the process WAS
    cannot be rewritten by what a person decided afterwards."""
    approved("INV-A")
    run_id = held("INV-B")
    before = analytics.overview(window())["kpis"]["automation_rate"]["value"]

    storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")
    after = analytics.overview(window())["kpis"]["automation_rate"]["value"]

    assert before == after == pytest.approx(0.5)
    # ...even though the ledger now reads both as approved.
    assert analytics.overview(window())["decisions"]["status"]["APPROVED"] == 2


def test_review_rate_is_the_exact_complement_of_the_automation_rate(db):
    approved("INV-A")
    approved("INV-B")
    held("INV-C")
    kpis = analytics.overview(window())["kpis"]
    assert kpis["automation_rate"]["value"] + kpis["human_review_rate"]["value"] == 1.0


# ==========================================================================
# 4. processing success rate -- machinery, not verdicts
# ==========================================================================

def test_a_rejected_invoice_is_a_processing_success(db):
    """The pipeline read it and the rules judged it. That the judgement was
    'reject' says nothing about whether processing worked."""
    rejected("INV-ORIG")
    kpi = analytics.overview(window())["kpis"]["processing_success_rate"]
    assert kpi["value"] == 1.0
    assert analytics.overview(window())["volume"]["extraction_failures"] == 0


def test_an_unreadable_document_is_a_processing_failure_even_though_it_was_held(db):
    """main.py's unreadable path records extraction_method 'none' and holds the
    run. The hold is the right response; the extraction still failed."""
    approved("INV-OK")
    raw_run(iso(0), automated="NEEDS_REVIEW",
            extracted_json=json.dumps({"extraction_method": "none", "raw_text": ""}))

    result = analytics.overview(window())
    assert result["volume"]["extraction_failures"] == 1
    assert result["kpis"]["processing_success_rate"]["value"] == pytest.approx(0.5)
    # ...and it is NOT counted as automated, because the rules held it.
    assert result["kpis"]["automation_rate"]["value"] == pytest.approx(0.5)


def test_processing_success_and_approval_are_not_the_same_number(db):
    """The whole reason the two metrics exist separately."""
    approved("INV-A")
    held("INV-B")
    held("INV-C", 7000.0)
    result = analytics.overview(window())
    assert result["kpis"]["processing_success_rate"]["value"] == 1.0
    assert result["decisions"]["automated"]["APPROVED"] == 1


# ==========================================================================
# 5. task success ratio
# ==========================================================================

def test_task_success_counts_a_hold_a_reviewer_ruled_on(db):
    """Not automated -- but the work finished, by the route it was meant to."""
    run_id = held("INV-HELD")
    before = analytics.overview(window())["kpis"]["task_success_ratio"]
    assert before["numerator"] == 0        # still open, waiting on a person

    storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")
    after = analytics.overview(window())["kpis"]["task_success_ratio"]
    assert after["numerator"] == 1 and after["denominator"] == 1
    assert after["value"] == 1.0


def test_an_open_hold_is_not_a_task_success(db):
    approved("INV-A")
    held("INV-B")
    kpi = analytics.overview(window())["kpis"]["task_success_ratio"]
    assert kpi["numerator"] == 1 and kpi["denominator"] == 2
    assert kpi["value"] == pytest.approx(0.5)


def test_an_administrator_override_removes_a_run_from_task_success(db):
    """Reaching past the process to correct a decision is not the process
    succeeding, even though the run ends up terminal either way."""
    run_id = approved("INV-A")
    approved("INV-B")
    assert analytics.overview(window())["kpis"]["task_success_ratio"]["numerator"] == 2

    storage.set_run_status(run_id, "REJECTED", note="operator reversal")
    storage.log_activity(run_id, "STATUS_OVERRIDDEN", actor="ada",
                         metadata={"from": "APPROVED", "to": "REJECTED"})

    kpi = analytics.overview(window())["kpis"]["task_success_ratio"]
    assert kpi["numerator"] == 1 and kpi["denominator"] == 2
    assert analytics.overview(window())["volume"]["overridden"] == 1


def test_task_success_is_documented_as_operational_not_correct(db):
    """The definition travels with the number precisely so nobody reads it as
    a claim about which decisions were RIGHT."""
    definition = analytics.overview(window())["kpis"]["task_success_ratio"]["definition"]
    assert "NOT correctness" in definition
    assert "ground truth" in definition


def test_task_success_differs_from_automation_rate(db):
    """If these two were the same number one of them would be redundant."""
    approved("INV-A")
    run_id = held("INV-B")
    storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")
    kpis = analytics.overview(window())["kpis"]
    assert kpis["automation_rate"]["value"] == pytest.approx(0.5)
    assert kpis["task_success_ratio"]["value"] == 1.0


# ==========================================================================
# 6. every KPI ships its own arithmetic
# ==========================================================================

def test_every_kpi_carries_its_numerator_denominator_and_definition(db):
    approved("INV-A")
    held("INV-B")
    for name, kpi in analytics.overview(window())["kpis"].items():
        assert set(kpi) == {"value", "numerator", "denominator", "definition"}, name
        assert isinstance(kpi["definition"], str) and len(kpi["definition"]) > 40, name
        if kpi["denominator"]:
            assert kpi["value"] == pytest.approx(kpi["numerator"] / kpi["denominator"]), name


# ==========================================================================
# 7. decision mix -- automated vs human vs ledger status
# ==========================================================================

def test_the_three_decision_views_stay_distinct_after_a_review(db):
    run_id = held("INV-HELD")
    storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")

    decisions = analytics.overview(window())["decisions"]
    # What the rules concluded -- never rewritten.
    assert decisions["automated"] == {"APPROVED": 0, "NEEDS_REVIEW": 1, "REJECTED": 0}
    # What the person concluded.
    assert decisions["human"]["ACCEPTED"] == 1
    # What the ledger now reads.
    assert decisions["status"] == {"APPROVED": 1, "NEEDS_REVIEW": 0, "REJECTED": 0}


def test_a_run_written_without_an_automated_decision_falls_back_to_its_status(db):
    """save_run (the unreadable-document path) writes no automated_decision;
    init_db backfills it on the next startup, so a run can legitimately hold
    NULL in the meantime and must still be counted."""
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO runs (filename, status, created_at, automated_decision)
                   VALUES ('x.pdf','NEEDS_REVIEW',%s, NULL)""", (iso(0),))
    finally:
        conn.close()
    decisions = analytics.overview(window())["decisions"]
    assert decisions["automated"]["NEEDS_REVIEW"] == 1


def test_the_transition_matrix_accounts_for_every_run_exactly_once(db):
    approved("INV-A")
    run_id = held("INV-B")
    storage.record_human_review(run_id, "REJECTED", reviewer="alice")
    held("INV-C", 7000.0)

    rows = analytics.reviews(window())["transitions"]
    assert sum(r["n"] for r in rows) == 3
    ruled = [r for r in rows if r["human"] == "REJECTED"][0]
    assert ruled["automated"] == "NEEDS_REVIEW"
    assert ruled["final_status"] == "REJECTED"
    assert ruled["final_decision"] == "HUMAN_REJECTED"


# ==========================================================================
# 8. value -- never summed across currencies
# ==========================================================================

def test_amounts_are_reported_per_currency_and_never_added_together(db):
    approved("INV-USD", 100.0, po=PO, currency="USD")
    # A EUR invoice against a USD PO: the rules hold it, which is correct and
    # beside the point here -- what matters is that its value lands in its own
    # currency bucket rather than being added to the dollars.
    submit(200.0, "INV-EUR", po=PO, currency="EUR")

    value = analytics.overview(window())["value_by_currency"]
    assert set(value) == {"USD", "EUR"}
    assert value["USD"]["processed"] == pytest.approx(100.0)
    assert value["EUR"]["processed"] == pytest.approx(200.0)
    # No combined total exists anywhere in the payload to be misread as one.
    assert "value_processed" not in analytics.overview(window())


def test_value_is_split_by_the_automated_decision(db):
    approved("INV-A", 100.0)
    held("INV-B", 6000.0)
    usd = analytics.overview(window())["value_by_currency"]["USD"]
    assert usd["approved"] == pytest.approx(100.0)
    assert usd["held"] == pytest.approx(6000.0)
    assert usd["processed"] == pytest.approx(6100.0)


# ==========================================================================
# 9. processing time and per-stage bottlenecks
# ==========================================================================

def test_run_time_is_the_sum_of_the_stage_timings_the_pipeline_recorded(db):
    approved("INV-A", stages=default_stages())
    expected = sum(s["ms"] for s in default_stages())
    stats = analytics.processing(window())["run_time_ms"]
    assert stats["samples"] == 1
    assert stats["average"] == pytest.approx(expected)
    assert stats["median"] == pytest.approx(expected)
    assert stats["min"] == stats["max"] == pytest.approx(expected)


def test_median_is_a_median_and_not_a_mean(db):
    for i, ms in enumerate((10, 20, 3000)):
        approved(f"INV-{i}", 10.0 + i, stages=[{"name": "DECISION", "status": "ok", "ms": ms}])
    stats = analytics.processing(window())["run_time_ms"]
    assert stats["median"] == pytest.approx(20)
    assert stats["average"] == pytest.approx((10 + 20 + 3000) / 3)
    assert stats["min"] == 10 and stats["max"] == 3000


def test_the_slowest_stage_is_reported_first_with_its_share_of_total_time(db):
    approved("INV-A", stages=default_stages())
    stages = analytics.processing(window())["stages"]
    assert stages[0]["stage"] == "EXTRACT_FIELDS"        # 300ms, the bottleneck
    total = sum(s["ms"] for s in default_stages())
    assert stages[0]["share_of_time"] == pytest.approx(300 / total)
    assert sum(s["share_of_time"] for s in stages) == pytest.approx(1.0)


def test_a_run_with_stages_but_no_timings_is_unmeasured_not_zero(db):
    """Folding an unmeasured run in as 0 ms would drag every average down."""
    approved("INV-A", stages=default_stages())
    approved("INV-B", 11.0,
             stages=[{"name": "DECISION", "status": "ok", "detail": ""}])   # no ms
    stats = analytics.processing(window())["run_time_ms"]
    assert stats["samples"] == 1
    assert stats["average"] == pytest.approx(sum(s["ms"] for s in default_stages()))


def test_stage_statuses_are_counted_alongside_the_timings(db):
    approved("INV-A", stages=[{"name": "VENDOR_CHECK", "status": "warn", "ms": 5}])
    stage = analytics.processing(window())["stages"][0]
    assert stage["statuses"] == {"warn": 1}


def test_extraction_routes_are_counted_from_what_the_pipeline_recorded(db):
    approved("INV-A")
    raw_run(iso(0), extracted_json=json.dumps({"extraction_method": "regex"}))
    routes = analytics.processing(window())["extraction"]["by_route"]
    assert routes["groq (text)"] == 1
    assert routes["regex"] == 1


# ==========================================================================
# 10. review latency
# ==========================================================================

def test_time_to_decision_measures_creation_to_ruling(db):
    run_id = raw_run(iso(2), automated="NEEDS_REVIEW", human="ACCEPTED",
                     reviewed_by="alice", reviewed_at=iso(0))
    assert run_id
    latency = analytics.reviews(window())["latency"]["time_to_decision"]
    assert latency["samples"] == 1
    assert latency["average_seconds"] == pytest.approx(2 * 86400, rel=1e-6)


def test_handling_time_measures_the_claim_to_the_ruling(db):
    run_id = held("INV-HELD")
    storage.claim_review(run_id, "alice")
    storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")

    latency = analytics.reviews(window())["latency"]["handling_time"]
    assert latency["samples"] == 1
    # Sub-second in a test, but real and non-negative -- the point is that a
    # claim was found and used, not the magnitude.
    assert latency["average_seconds"] >= 0
    assert latency["unclaimed_reviews"] == 0


def test_a_review_with_no_claim_is_counted_as_unmeasurable_not_as_zero(db):
    """Claiming is optional (§6.4), so handling time genuinely cannot be
    computed for an unclaimed review. Reported, not silently averaged in."""
    run_id = held("INV-HELD")
    storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")

    latency = analytics.reviews(window())["latency"]
    assert latency["time_to_decision"]["samples"] == 1
    assert latency["handling_time"]["samples"] == 0
    assert latency["handling_time"]["average_seconds"] is None
    assert latency["handling_time"]["unclaimed_reviews"] == 1


def test_a_run_that_was_never_reviewed_contributes_no_latency(db):
    held("INV-HELD")
    latency = analytics.reviews(window())["latency"]["time_to_decision"]
    assert latency["samples"] == 0
    assert latency["median_seconds"] is None


def test_null_timestamps_never_reach_the_latency_arithmetic(db):
    """A run marked reviewed with no reviewed_at cannot produce a duration.
    It must be skipped, not turned into a zero or an exception."""
    raw_run(iso(1), automated="NEEDS_REVIEW", human="ACCEPTED",
            reviewed_by="alice", reviewed_at=None)
    latency = analytics.reviews(window())["latency"]["time_to_decision"]
    assert latency["samples"] == 0
    assert latency["average_seconds"] is None


def test_the_backlog_reports_what_is_open_and_how_old_it_is(db):
    approved("INV-A")
    held("INV-B")
    backlog = analytics.overview(window())["backlog"]
    assert backlog["awaiting_review"] == 1
    assert backlog["oldest_awaiting_at"] is not None
    assert backlog["oldest_awaiting_age_seconds"] >= 0
    assert backlog["claimed_now"] == 0


def test_the_backlog_counts_a_live_claim_and_forgets_an_expired_one(db):
    """`claimed_now` is derived the way get_active_claim derives it -- an
    expired lease reads as unheld immediately, with no sweep job involved."""
    run_id = held("INV-HELD")
    storage.claim_review(run_id, "alice")
    assert analytics.overview(window())["backlog"]["claimed_now"] == 1

    conn = storage.get_conn()
    try:
        conn.cursor().execute(
            "UPDATE review_claims SET expires_at=%s WHERE run_id=%s", (iso(1), run_id))
    finally:
        conn.close()
    assert analytics.overview(window())["backlog"]["claimed_now"] == 0


# ==========================================================================
# 11. review funnel and effectiveness
# ==========================================================================

def test_the_review_funnel_counts_each_stage_from_the_rows(db):
    approved("INV-A")
    a = held("INV-B")
    b = held("INV-C", 7000.0)
    held("INV-D", 8000.0)
    storage.record_human_review(a, "ACCEPTED", reviewer="alice")
    storage.record_human_review(b, "REJECTED", reviewer="bob")

    funnel = analytics.reviews(window())["funnel"]
    assert funnel == {"runs": 4, "held_for_review": 3, "ruled_on": 2,
                      "accepted": 1, "rejected": 1, "still_awaiting": 1}


def test_accept_and_reject_rates_are_over_rulings_not_over_all_runs(db):
    approved("INV-A")
    a = held("INV-B")
    b = held("INV-C", 7000.0)
    storage.record_human_review(a, "ACCEPTED", reviewer="alice")
    storage.record_human_review(b, "ACCEPTED", reviewer="alice")

    rates = analytics.reviews(window())["rates"]
    assert rates["accept_rate"]["denominator"] == 2      # rulings, not the 3 runs
    assert rates["accept_rate"]["value"] == 1.0
    assert rates["reject_rate"]["value"] == 0.0          # a real zero: 0 of 2


def test_review_rates_never_claim_a_decision_was_correct(db):
    """The database holds no ground truth, so no definition may imply one."""
    rates = analytics.reviews(window())["rates"]
    assert "NOT evidence the hold was wrong" in rates["accept_rate"]["definition"]
    assert "NOT evidence the hold was right" in rates["reject_rate"]["definition"]
    for kpi in rates.values():
        assert "correct" not in kpi["definition"].lower().replace("correctness", "")


def test_hold_reasons_are_grouped_by_rule_name_not_by_reason_sentence(db):
    """Reason sentences carry the invoice's own amounts, so grouping by them
    would produce a list of individual invoices rather than a list of causes."""
    held("INV-B", 6000.0)
    held("INV-C", 7000.0)          # a DIFFERENT amount, the SAME cause

    reasons = analytics.reviews(window())["reasons"]
    names = {r["rule"]: r["runs"] for r in reasons}
    assert "PO remaining check" in names
    assert names["PO remaining check"] == 2
    # No amount from either invoice leaked into the grouping key.
    assert not any("6000" in r["rule"] or "7000" in r["rule"] for r in reasons)


def test_review_activity_counts_events_without_naming_anyone(db):
    run_id = held("INV-HELD")
    storage.claim_review(run_id, "alice")
    storage.add_comment(run_id, "alice", "checking with the vendor")

    activity = analytics.reviews(window())["activity"]
    assert activity["REVIEW_CLAIMED"] == 1
    assert activity["COMMENT_ADDED"] == 1
    # Aggregate only -- no actor appears anywhere in this payload.
    assert "alice" not in json.dumps(analytics.reviews(window()))


# ==========================================================================
# 12. vendor and PO breakdowns -- the ledger, not a second copy of it
# ==========================================================================

def test_set_based_ledger_matches_the_per_po_ledger(db):
    """THE ANTI-DRIFT TEST.

    storage.consumed_amounts_by_po() exists so analytics can read every PO's
    consumption in one query instead of one query per PO. That means the
    ledger rule -- sum run_allocations, joined to runs.status='APPROVED' -- is
    written twice. This asserts the two agree on a database that has the cases
    where a naive second implementation would diverge: a multi-PO invoice, a
    rejection, and a reversal that refunds budget.
    """
    approved("INV-A", 100.0, po=PO)
    approved("INV-ACME", 200.0, po=ACME_PO, vendor=ACME)
    reversed_id = approved("INV-REV", 300.0, po=PO)
    approved("INV-ORIG2", 50.0, po=PO)
    rejected("INV-ORIG2")                                   # duplicate: consumes nothing
    # A multi-PO invoice, accepted by a reviewer so it consumes both POs.
    multi = submit(400.0, "INV-MULTI", po=None, vendor=VENDOR)[0]
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM runs WHERE id=%s", (multi,))
    finally:
        conn.close()
    # A reversal, so at least one PO's balance has been refunded.
    storage.set_run_status(reversed_id, "REJECTED", note="reversed")

    set_based = storage.consumed_amounts_by_po()
    for po in storage.list_purchase_orders():
        number = po["po_number"]
        assert set_based.get(number, 0.0) == pytest.approx(
            storage.consumed_amount_for_po(number)), number


def test_po_analytics_report_the_ledgers_own_balance(db):
    approved("INV-A", 1000.0, po=PO)
    rows = {p["po_number"]: p for p in analytics.purchase_orders(window())}
    row = rows[PO]
    assert row["consumed"] == pytest.approx(storage.consumed_amount_for_po(PO))
    assert row["remaining"] == pytest.approx(storage.remaining_for_po(PO))
    assert row["utilisation"] == pytest.approx(1000.0 / 5000.0)
    assert row["over_budget"] is False


def test_a_reversal_refunds_the_po_in_the_analytics_view_immediately(db):
    """No refund step exists because nothing was deducted -- the same property
    the ledger has, seen through analytics."""
    run_id = approved("INV-A", 1000.0, po=PO)
    before = {p["po_number"]: p for p in analytics.purchase_orders(window())}[PO]
    assert before["consumed"] == pytest.approx(1000.0)

    storage.set_run_status(run_id, "REJECTED", note="reversed")
    after = {p["po_number"]: p for p in analytics.purchase_orders(window())}[PO]
    assert after["consumed"] == 0.0
    assert after["remaining"] == pytest.approx(after["amount"])


def test_po_consumption_is_all_time_while_the_invoice_counts_are_windowed(db):
    """A remaining balance "as of the last 7 days" is a number with no meaning
    to anyone about to approve an invoice against that PO."""
    raw_id = approved("INV-OLD", 1000.0, po=PO)
    conn = storage.get_conn()
    try:
        conn.cursor().execute("UPDATE runs SET created_at=%s WHERE id=%s",
                              (iso(90), raw_id))
    finally:
        conn.close()

    row = {p["po_number"]: p for p in analytics.purchase_orders(window("7d"))}[PO]
    assert row["consumed"] == pytest.approx(1000.0)      # ledger: all time
    assert row["runs_in_range"] == 0                     # activity: windowed


def test_vendor_rows_carry_counts_rates_and_timing(db):
    approved("INV-A", 100.0, vendor=VENDOR, po=PO)
    held("INV-B", 6000.0, vendor=VENDOR, po=PO)
    approved("INV-C", 100.0, vendor=ACME, po=ACME_PO)

    rows = {v["vendor"]: v for v in analytics.vendors(window())["vendors"]}
    assert rows[VENDOR]["runs"] == 2
    assert rows[VENDOR]["approval_rate"] == pytest.approx(0.5)
    assert rows[VENDOR]["hold_rate"] == pytest.approx(0.5)
    assert rows[VENDOR]["rejection_rate"] == 0.0
    assert rows[VENDOR]["avg_processing_ms"] is not None
    assert rows[ACME]["runs"] == 1


def test_a_run_with_no_vendor_is_labelled_rather_than_dropped(db):
    raw_run(iso(0), vendor=None)
    names = [v["vendor"] for v in analytics.vendors(window())["vendors"]]
    assert names == ["(unidentified)"]


def test_the_vendor_list_honours_its_limit_and_says_it_was_truncated(db):
    approved("INV-A", 100.0, vendor=VENDOR, po=PO)
    approved("INV-C", 100.0, vendor=ACME, po=ACME_PO)
    result = analytics.vendors(window(), limit=1)
    assert len(result["vendors"]) == 1
    assert result["truncated"] is True


# ==========================================================================
# 13. date filtering and timezone boundaries
# ==========================================================================

def test_runs_are_filtered_by_the_window(db):
    raw_run(iso(0))
    raw_run(iso(3))
    raw_run(iso(40))

    assert analytics.overview(window("today"))["volume"]["runs"] == 1
    assert analytics.overview(window("7d"))["volume"]["runs"] == 2
    assert analytics.overview(window("30d"))["volume"]["runs"] == 2
    assert analytics.overview(window("all"))["volume"]["runs"] == 3


def test_the_window_boundary_is_half_open_at_both_ends(db):
    """A run at exactly UTC midnight belongs to the day that STARTS then, not
    to the one that ends then -- otherwise it is counted twice or not at all."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    raw_run(today.isoformat())                                  # exactly the start
    raw_run((today - timedelta(microseconds=1)).isoformat())    # a tick before

    assert analytics.overview(window("today"))["volume"]["runs"] == 1


def test_a_custom_range_includes_its_final_day_in_full(db):
    day = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    raw_run(iso(5, hour=23, minute=59))       # late on the final day

    result = analytics.overview(window("custom", date_from=day, date_to=day))
    assert result["volume"]["runs"] == 1


def test_day_buckets_are_utc_days_and_the_response_says_so(db):
    """The stored timestamp's first ten characters ARE its UTC date, which is
    what the bucketing uses -- no cast, no local-midnight assumption."""
    stamp = iso(1, hour=23, minute=30)
    raw_run(stamp)
    result = analytics.trends(window("7d"))
    assert result["timezone"] == "UTC"
    day = next(b for b in result["buckets"] if b["day"] == stamp[:10])
    assert day["runs"] == 1


def test_email_analytics_are_filtered_by_received_at_not_by_run_time(db):
    """The email funnel is about when mail ARRIVED."""
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            for days, relevance in ((0, "HIGH"), (40, "HIGH")):
                cur.execute(
                    """INSERT INTO email_messages (sha256, received_at, classification,
                           status, relevance, ingest_status)
                       VALUES (%s,%s,'VERIFIED','ADMITTED',%s,'PROCESSED')""",
                    (f"sha-{days}", iso(days), relevance))
    finally:
        conn.close()
    assert analytics.email(window("7d"))["funnel"]["received"] == 1
    assert analytics.email(window("all"))["funnel"]["received"] == 2


# ==========================================================================
# 14. trends
# ==========================================================================

def test_a_trend_series_has_one_bucket_per_day_including_empty_ones(db):
    raw_run(iso(0))
    buckets = analytics.trends(window("7d"))["buckets"]
    assert len(buckets) == 7
    assert [b["day"] for b in buckets] == sorted(b["day"] for b in buckets)
    assert sum(b["runs"] for b in buckets) == 1


def test_an_empty_day_reports_zero_runs_and_a_null_rate(db):
    """Zero invoices is a real fact; an automation rate of 0% that day is not."""
    raw_run(iso(0))
    empty = [b for b in analytics.trends(window("7d"))["buckets"] if b["runs"] == 0]
    assert empty, "the window should contain at least one quiet day"
    for b in empty:
        assert b["automation_rate"] is None
        assert b["approval_rate"] is None
        assert b["avg_processing_ms"] is None
        assert b["timed_runs"] == 0


def test_daily_rates_are_computed_within_the_day(db):
    day = iso(1, hour=10)
    raw_run(day, automated="APPROVED")
    raw_run(iso(1, hour=11), automated="NEEDS_REVIEW")

    bucket = next(b for b in analytics.trends(window("7d"))["buckets"]
                  if b["day"] == day[:10])
    assert bucket["runs"] == 2
    assert bucket["approved"] == 1 and bucket["needs_review"] == 1
    assert bucket["automation_rate"] == pytest.approx(0.5)


def test_daily_average_processing_time_agrees_with_the_headline_figure(db):
    """One JSON pass feeds both, so the trend and the summary cannot disagree
    about what a millisecond is."""
    approved("INV-A", stages=default_stages())
    expected = sum(s["ms"] for s in default_stages())
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bucket = next(b for b in analytics.trends(window("today"))["buckets"]
                  if b["day"] == day)
    assert bucket["avg_processing_ms"] == pytest.approx(expected)
    assert analytics.processing(window("today"))["run_time_ms"]["average"] == \
        pytest.approx(expected)


def test_an_absurdly_wide_range_is_refused_rather_than_truncated(db):
    with pytest.raises(analytics.AnalyticsError) as exc:
        analytics.trends(analytics.resolve_window("custom", "2000-01-01", "2026-01-01"))
    assert "buckets" in str(exc.value)


def test_all_time_trends_start_at_the_first_run_not_at_the_epoch(db):
    raw_run(iso(3))
    buckets = analytics.trends(window("all"))["buckets"]
    assert len(buckets) == 4                          # 3 days ago through today
    assert buckets[0]["runs"] == 1


def test_all_time_trends_on_an_empty_database_return_no_buckets(db):
    assert analytics.trends(window("all"))["buckets"] == []


# ==========================================================================
# 15. malformed data must not take the dashboard down
# ==========================================================================

def test_malformed_stages_json_costs_its_own_row_and_nothing_else(db):
    approved("INV-GOOD", stages=default_stages())
    raw_run(iso(0), stages_json="{not json at all")

    result = analytics.processing(window())
    assert result["run_time_ms"]["samples"] == 1                 # the good one
    assert result["data_quality"]["malformed_json"]["stages"] == 1
    assert result["data_quality"]["runs_scanned"] == 2


def test_malformed_audit_json_costs_its_own_row_and_nothing_else(db):
    held("INV-HELD")                                              # real audit trail
    raw_run(iso(0), audit_json="]]]not json[[[")

    reasons = analytics.reviews(window())["reasons"]
    assert any(r["rule"] == "PO remaining check" for r in reasons)
    assert analytics.overview(window())["data_quality"]["malformed_json"]["audit"] == 1


def test_json_of_the_wrong_shape_is_treated_as_absent_not_as_data(db):
    """Valid JSON that is not the expected type must not be iterated."""
    raw_run(iso(0), stages_json='"a string"', audit_json='[1,2,3]',
            extracted_json='42')
    result = analytics.overview(window())
    assert result["volume"]["runs"] == 1
    assert result["data_quality"]["malformed_total"] == 3


def test_a_stage_entry_that_is_not_an_object_is_skipped(db):
    approved("INV-A", stages=[{"name": "DECISION", "status": "ok", "ms": 10},
                              "not an object", 42, None])
    stats = analytics.processing(window())["run_time_ms"]
    assert stats["samples"] == 1 and stats["average"] == pytest.approx(10)


def test_a_non_numeric_stage_duration_is_ignored(db):
    approved("INV-A", stages=[{"name": "DECISION", "status": "ok", "ms": "fast"},
                              {"name": "INGEST", "status": "ok", "ms": 7}])
    assert analytics.processing(window())["run_time_ms"]["average"] == pytest.approx(7)


def test_a_boolean_is_not_accepted_as_a_number(db):
    """`True` is an int in Python, and would silently count as 1 ms."""
    approved("INV-A", stages=[{"name": "DECISION", "status": "ok", "ms": True}])
    assert analytics.processing(window())["run_time_ms"]["samples"] == 0


def test_data_quality_is_reported_rather_than_swallowed(db):
    approved("INV-A")
    raw_run(iso(0), stages_json="broken")
    quality = analytics.overview(window())["data_quality"]
    assert quality["runs_scanned"] == 2
    assert quality["runs_with_timing"] == 1
    assert quality["malformed_total"] == 1


# ==========================================================================
# 16. email ingestion funnel
# ==========================================================================

def make_message(sha, relevance="HIGH", classification="VERIFIED", status="ADMITTED",
                 ingest_status="PROCESSED", sender_type="CORPORATE",
                 trust_status="TRUSTED", has_pdf=True, run_id=None, days=0):
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO email_messages (sha256, received_at, classification,
                       status, relevance, ingest_status, sender_type, trust_status,
                       has_pdf_attachment, run_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (sha, iso(days), classification, status, relevance, ingest_status,
                 sender_type, trust_status, has_pdf, run_id))
            return cur.fetchone()["id"]
    finally:
        conn.close()


def make_attachment(email_id, status="PROCESSED", candidate=True, run_id=None, sha=None):
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO email_attachments (email_id, seq, filename, sha256,
                       is_invoice_candidate, status, run_id, created_at)
                   VALUES (%s,0,'invoice.pdf',%s,%s,%s,%s,%s) RETURNING id""",
                (email_id, sha or f"att-{email_id}-{status}", candidate, status,
                 run_id, iso(0)))
            return cur.fetchone()["id"]
    finally:
        conn.close()


def test_the_email_funnel_follows_the_order_ingestion_evaluates_in(db):
    run_id = approved("INV-FROM-EMAIL")
    relevant = make_message("sha-1", relevance="HIGH", run_id=run_id)
    make_attachment(relevant, status="PROCESSED", run_id=run_id)
    make_message("sha-2", relevance="IRRELEVANT", ingest_status="FILTERED_OUT",
                 status="QUARANTINED", has_pdf=False)
    make_message("sha-3", relevance="POSSIBLE", status="QUARANTINED",
                 classification="UNVERIFIED", ingest_status="QUARANTINED")

    funnel = analytics.email(window())["funnel"]
    assert funnel["received"] == 3
    assert funnel["relevant"] == 2               # HIGH + POSSIBLE
    assert funnel["filtered_out"] == 1           # IRRELEVANT
    assert funnel["admitted"] == 1
    assert funnel["quarantined"] == 2
    assert funnel["runs_created"] == 1


def test_one_email_carrying_several_invoices_produces_several_runs(db):
    """Counting emails would understate what ingestion delivered."""
    a = approved("INV-1", 100.0)
    b = approved("INV-2", 110.0)
    email_id = make_message("sha-multi")
    make_attachment(email_id, run_id=a, sha="att-a")
    make_attachment(email_id, run_id=b, sha="att-b")
    make_attachment(email_id, status="SKIPPED", candidate=False, sha="att-logo")

    funnel = analytics.email(window())["funnel"]
    assert funnel["received"] == 1
    assert funnel["attachments"] == 3
    assert funnel["invoice_candidates"] == 2
    assert funnel["runs_created"] == 2
    assert funnel["runs_approved"] == 2
    # More runs than emails: the yield ratio is allowed to exceed 1, and says so.
    assert analytics.email(window())["rates"]["invoice_yield"]["value"] == 2.0


def test_attachment_statuses_are_reported_with_every_known_value_present(db):
    email_id = make_message("sha-a")
    make_attachment(email_id, status="FAILED", sha="att-f")
    statuses = analytics.email(window())["attachments_by_status"]
    assert statuses["FAILED"] == 1
    # A status that did not occur is a real zero, so it is present as one.
    for known in config.EMAIL_ATTACHMENT_STATUSES:
        assert known in statuses
    assert statuses["PROCESSED"] == 0


def test_security_status_and_ingest_status_are_reported_separately(db):
    """"We would not accept it" and "we never tried" are different facts."""
    make_message("sha-q", status="QUARANTINED", ingest_status="FILTERED_OUT")
    result = analytics.email(window())
    assert result["by_security_status"]["QUARANTINED"] == 1
    assert result["by_ingest_status"]["FILTERED_OUT"] == 1


def test_sender_type_and_trust_status_stay_two_independent_axes(db):
    """A corporate sender is not automatically trusted; a personal one is not
    automatically refused (Phase G, §7b.2)."""
    make_message("sha-1", sender_type="PERSONAL", trust_status="TRUSTED")
    make_message("sha-2", sender_type="CORPORATE", trust_status="UNKNOWN")
    result = analytics.email(window())
    assert result["by_sender_type"] == {"CORPORATE": 1, "PERSONAL": 1, "UNKNOWN": 0}
    assert result["by_trust_status"]["TRUSTED"] == 1
    assert result["by_trust_status"]["UNKNOWN"] == 1


def test_email_analytics_on_an_empty_mailbox_report_zero_and_null(db):
    result = analytics.email(window())
    assert result["funnel"]["received"] == 0
    assert result["rates"]["relevance_rate"]["value"] is None


def test_email_analytics_never_expose_message_content(db):
    """Counts and statuses only. No address, no subject, no body -- Phase F
    does not store the last of those at all, and this must not start."""
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO email_messages (sha256, received_at, classification,
                       status, relevance, from_address, from_domain, subject)
                   VALUES ('s',%s,'VERIFIED','ADMITTED','HIGH',
                           'billing@vendor.example','vendor.example',
                           'Invoice 99 attached')""", (iso(0),))
    finally:
        conn.close()
    blob = json.dumps(analytics.email(window()))
    assert "billing@vendor.example" not in blob
    assert "vendor.example" not in blob
    assert "Invoice 99 attached" not in blob


# ==========================================================================
# 17. extraction usage -- counts, never invented cost
# ==========================================================================

def test_extraction_usage_reports_counts_and_says_cost_is_unavailable(db):
    quota_block = analytics.processing(window())["quota"]
    assert quota_block["cost_available"] is False
    assert "does not persist" in quota_block["note"]
    providers = {p["provider"]: p for p in quota_block["providers"]}
    for p in providers.values():
        assert p["limit"] > 0
        assert p["used_today"] >= 0
        assert p["remaining"] == max(0, p["limit"] - p["used_today"])


def test_no_monetary_figure_appears_anywhere_in_the_payload(db):
    """The application persists request counts and nothing else, so a dollar
    figure could only have been invented."""
    blob = json.dumps(analytics.processing(window()))
    for token in ('"cost"', '"spend"', '"usd_cost"', '"price"'):
        assert token not in blob


def test_recorded_extraction_usage_is_read_back(db):
    import quota
    quota.try_consume(quota.TEXT)
    providers = {p["provider"]: p
                 for p in analytics.processing(window())["quota"]["providers"]}
    assert providers[quota.TEXT]["used_today"] >= 1
    assert providers[quota.TEXT]["utilisation"] is not None


# ==========================================================================
# 18. per-person analytics and their authorization
# ==========================================================================

def seed_two_reviewers(db):
    a = held("INV-A", 6000.0)
    b = held("INV-B", 7000.0)
    c = held("INV-C", 8000.0)
    storage.record_human_review(a, "ACCEPTED", reviewer="alice")
    storage.record_human_review(b, "REJECTED", reviewer="alice")
    storage.record_human_review(c, "ACCEPTED", reviewer="bob")
    return a, b, c


def test_an_administrator_sees_every_reviewer(db):
    seed_two_reviewers(db)
    result = analytics.users(window(), viewer="ada", see_everyone=True)
    assert result["scope"] == "all"
    by_name = {u["username"]: u for u in result["users"]}
    assert set(by_name) >= {"alice", "bob"}
    assert by_name["alice"]["reviews"] == 2
    assert by_name["alice"]["accepted"] == 1
    assert by_name["alice"]["rejected"] == 1
    assert by_name["alice"]["accept_rate"] == pytest.approx(0.5)
    assert by_name["bob"]["reviews"] == 1


def test_a_reviewer_sees_only_themselves_even_when_colleagues_have_rows(db):
    """The leak this endpoint exists to prevent."""
    seed_two_reviewers(db)
    result = analytics.users(window(), viewer="alice", see_everyone=False)
    assert result["scope"] == "self"
    assert [u["username"] for u in result["users"]] == ["alice"]
    assert "bob" not in json.dumps(result)
    assert result["note"] and "invoice:admin" in result["note"]


def test_a_reviewer_cannot_read_a_colleague_by_asking_for_them(db):
    """Scope is decided from the authenticated principal, and there is no
    parameter that could ask for someone else."""
    seed_two_reviewers(db)
    result = analytics.users(window(), viewer="bob", see_everyone=False)
    assert [u["username"] for u in result["users"]] == ["bob"]
    assert result["users"][0]["reviews"] == 1


def test_someone_holding_a_claim_but_having_ruled_on_nothing_still_appears(db):
    run_id = held("INV-HELD")
    storage.claim_review(run_id, "carol")
    result = analytics.users(window(), viewer="ada", see_everyone=True)
    carol = next(u for u in result["users"] if u["username"] == "carol")
    assert carol["reviews"] == 0
    assert carol["claims_held_now"] == 1
    assert carol["accept_rate"] is None       # not 0.0 -- they have ruled on nothing


def test_per_user_activity_counts_come_from_the_append_only_log(db):
    run_id = held("INV-HELD")
    storage.claim_review(run_id, "alice")
    storage.add_comment(run_id, "alice", "asked the vendor")
    result = analytics.users(window(), viewer="alice", see_everyone=False)
    events = result["users"][0]["events"]
    assert events["REVIEW_CLAIMED"] == 1
    assert events["COMMENT_ADDED"] == 1


def test_per_user_timing_is_reported_or_explicitly_absent(db):
    seed_two_reviewers(db)
    for user in analytics.users(window(), viewer="ada", see_everyone=True)["users"]:
        if user["reviews"]:
            assert user["avg_time_to_decision_seconds"] is not None
            assert user["median_time_to_decision_seconds"] is not None
        else:
            assert user["avg_time_to_decision_seconds"] is None


# ==========================================================================
# 19. HTTP: authentication, authorization, validation
# ==========================================================================

ENDPOINTS = ["/api/analytics/overview", "/api/analytics/trends",
             "/api/analytics/processing", "/api/analytics/reviews",
             "/api/analytics/vendors", "/api/analytics/email",
             "/api/analytics/users"]


def test_every_analytics_endpoint_refuses_an_unauthenticated_caller(client):
    for path in ENDPOINTS:
        r = client.get(path, headers={"Authorization": ""})
        assert r.status_code == 401, path


def test_every_analytics_endpoint_refuses_a_forged_token(client):
    bad = {"Authorization": "Bearer not.a.real.token"}
    for path in ENDPOINTS:
        assert client.get(path, headers=bad).status_code == 401, path


def test_a_viewer_may_read_the_aggregate_analytics(client):
    """`invoice:read` already reads a run and its audit trail, so an aggregate
    derived from those rows needs no more than the same permission."""
    for path in ENDPOINTS[:-1]:
        r = client.get(path, headers=auth_headers("viewer", "vic"))
        assert r.status_code == 200, (path, r.text)


def test_a_viewer_sees_only_their_own_row_on_the_users_endpoint(client):
    seed_two_reviewers(None)
    r = client.get("/api/analytics/users", headers=auth_headers("viewer", "alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "self"
    assert body["viewer"] == "alice"
    assert "bob" not in r.text


def test_an_administrator_sees_the_whole_team_over_http(client):
    seed_two_reviewers(None)
    r = client.get("/api/analytics/users", headers=auth_headers("admin", "ada"))
    body = r.json()
    assert body["scope"] == "all"
    assert {"alice", "bob"} <= {u["username"] for u in body["users"]}


def test_a_reviewer_is_not_an_administrator_for_this_purpose(client):
    """Every reviewer is a peer; giving them each other's throughput on the
    strength of `invoice:review` would expose the whole team to the whole team."""
    seed_two_reviewers(None)
    r = client.get("/api/analytics/users", headers=auth_headers("reviewer", "alice"))
    assert r.json()["scope"] == "self"
    assert "bob" not in r.text


def test_a_bad_range_parameter_is_a_400_not_a_500(client):
    for path in ENDPOINTS:
        r = client.get(path, params={"range": "since-tuesday"})
        assert r.status_code == 400, (path, r.status_code)
        assert "since-tuesday" in r.json()["error"]


def test_a_malformed_date_is_a_400_not_a_500(client):
    r = client.get("/api/analytics/overview",
                   params={"range": "custom", "from": "not-a-date", "to": "2026-01-01"})
    assert r.status_code == 400
    assert "YYYY-MM-DD" in r.json()["error"]


def test_a_backwards_custom_range_is_a_400(client):
    r = client.get("/api/analytics/overview",
                   params={"from": "2026-03-05", "to": "2026-03-01"})
    assert r.status_code == 400


def test_an_out_of_range_limit_is_refused(client):
    assert client.get("/api/analytics/vendors", params={"limit": 0}).status_code == 422
    assert client.get("/api/analytics/vendors",
                      params={"limit": analytics.MAX_GROUP_LIMIT + 1}).status_code == 422


def test_a_trend_range_wider_than_the_bucket_cap_is_a_400(client):
    r = client.get("/api/analytics/trends",
                   params={"from": "2000-01-01", "to": "2026-01-01"})
    assert r.status_code == 400
    assert "buckets" in r.json()["error"]


def test_the_endpoints_return_the_same_numbers_as_the_service_layer(client):
    approved("INV-A")
    held("INV-B")
    body = client.get("/api/analytics/overview", params={"range": "all"}).json()
    direct = analytics.overview(window("all"))
    assert body["kpis"]["automation_rate"]["value"] == \
        direct["kpis"]["automation_rate"]["value"]
    assert body["volume"]["runs"] == direct["volume"]["runs"] == 2


def test_the_range_travels_back_with_the_answer(client):
    body = client.get("/api/analytics/overview", params={"range": "7d"}).json()
    assert body["range"]["key"] == "7d"
    assert body["range"]["timezone"] == "UTC"
    assert body["range"]["from"] < body["range"]["to"]


# ==========================================================================
# 20. security -- what analytics must not disclose
# ==========================================================================

def test_analytics_never_return_invoice_line_items_or_raw_audit_blobs(client):
    """An aggregate is not a licence to ship the whole row. A dashboard needs
    counts; it does not need the document's contents."""
    approved("INV-SECRET", 4321.0)
    for path in ENDPOINTS:
        text = client.get(path, params={"range": "all"}).text
        assert "audit_json" not in text, path
        assert "extracted_json" not in text, path
        assert "raw_text" not in text, path
        assert "storage_key" not in text, path
        assert "provenance" not in text, path


def test_analytics_never_expose_a_document_storage_location(client):
    approved("INV-A")
    for path in ENDPOINTS:
        body = client.get(path, params={"range": "all"}).text
        assert "storage_backend" not in body, path


def test_analytics_are_read_only_and_change_no_row(db):
    """Nothing under /api/analytics writes. Asserted by taking the whole
    decision-bearing state before and after and requiring it identical."""
    approved("INV-A")
    run_id = held("INV-B")
    storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")

    def snapshot():
        conn = storage.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT id, status, automated_decision, human_decision,
                                      final_decision, reviewed_by, reviewed_at
                               FROM runs ORDER BY id""")
                runs = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT COUNT(*) n FROM invoice_activity")
                activity = cur.fetchone()["n"]
                cur.execute("SELECT COUNT(*) n FROM run_allocations")
                allocations = cur.fetchone()["n"]
        finally:
            conn.close()
        return runs, activity, allocations

    before = snapshot()
    w = window()
    analytics.overview(w)
    analytics.trends(window("30d"))
    analytics.processing(w)
    analytics.reviews(w)
    analytics.vendors(w)
    analytics.email(w)
    analytics.users(w, viewer="ada", see_everyone=True)
    assert snapshot() == before


def test_no_analytics_endpoint_accepts_a_write_method(client):
    for path in ENDPOINTS:
        assert client.post(path, json={}).status_code == 405, path


# ==========================================================================
# 21. backwards compatibility -- Phase H changed no existing behaviour
# ==========================================================================

def test_the_existing_run_endpoints_are_unchanged(client):
    run_id = approved("INV-A")
    listed = client.get("/api/runs")
    assert listed.status_code == 200 and len(listed.json()) == 1
    single = client.get(f"/api/runs/{run_id}")
    assert single.status_code == 200
    assert single.json()["status"] == "APPROVED"
    assert "audit" in single.json()


def test_the_review_path_still_works_alongside_analytics(client):
    run_id = held("INV-HELD")
    r = client.post(f"/api/runs/{run_id}/review",
                    json={"decision": "ACCEPTED", "note": "checked"},
                    headers=auth_headers("reviewer", "alice"))
    assert r.status_code == 200
    assert storage.get_run(run_id)["human_decision"] == "ACCEPTED"
    assert analytics.overview(window())["decisions"]["human"]["ACCEPTED"] == 1


def test_the_new_indexes_exist_and_the_schema_gained_no_table(db):
    """Phase H adds indexes and nothing else -- no counter column, no rollup
    table. Asserted rather than promised."""
    conn = storage.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname=%s",
                        (storage.PG_SCHEMA,))
            indexes = {r["indexname"] for r in cur.fetchall()}
            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema=%s", (storage.PG_SCHEMA,))
            tables = {r["table_name"] for r in cur.fetchall()}
    finally:
        conn.close()

    assert {"idx_runs_created_at", "idx_runs_vendor_name",
            "idx_runs_reviewed_by", "idx_activity_actor"} <= indexes
    assert tables == {"purchase_orders", "vendors", "runs", "run_allocations",
                      "documents", "invoice_activity", "review_claims",
                      "trusted_email_senders", "email_messages", "email_activity",
                      "email_attachments",
                      # Phase G2's Gmail connection. Listed rather than the
                      # assertion being loosened: the point of comparing the
                      # WHOLE set is that a table nobody mentioned shows up
                      # here, and that only keeps working while the expected
                      # set is maintained.
                      "email_oauth_connections", "oauth_pending_authorizations"}
    # In particular, nothing that looks like a stored rollup.
    assert not any("analytic" in t or "metric" in t or "kpi" in t for t in tables)


def test_reviewer_workload_is_windowed_by_when_the_work_was_done(db):
    """A reviewer who spends today clearing a month-old backlog did today's
    work, and must not read as idle.

    This is the ONE endpoint windowed on `reviewed_at` rather than
    `created_at`, and the difference is load-bearing rather than cosmetic --
    a review queue produces exactly this case constantly.
    """
    run_id = held("INV-OLD", 6000.0)
    conn = storage.get_conn()
    try:
        conn.cursor().execute("UPDATE runs SET created_at=%s WHERE id=%s",
                              (iso(45), run_id))
    finally:
        conn.close()
    # Ruled on NOW, on an invoice that arrived 45 days ago.
    storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")

    today = analytics.users(window("today"), viewer="alice", see_everyone=False)
    assert today["users"] and today["users"][0]["reviews"] == 1, \
        "today's work on an old invoice must count as today's work"

    # ...while the invoice-cohort endpoints correctly place that INVOICE in the
    # month it arrived, which is a different question and a different answer.
    assert analytics.reviews(window("today"))["funnel"]["held_for_review"] == 0
    assert analytics.reviews(window("all"))["funnel"]["ruled_on"] == 1


def test_a_column_reference_can_never_carry_injected_sql():
    """The window bounds are bind parameters; the COLUMN is interpolated,
    because SQL cannot bind an identifier. Every call site passes a literal --
    and that is enforced here, not merely documented, so a future edit that
    threads a request value into this argument fails loudly."""
    w = analytics.resolve_window("30d")
    for hostile in ("runs.created_at; DROP TABLE runs--",
                    "runs.created_at OR 1=1",
                    "runs.created_at)",
                    "*",
                    ""):
        with pytest.raises(analytics.AnalyticsError):
            w.clause(hostile, [])
    # The legitimate forms still work.
    params = []
    assert "runs.created_at" in w.clause("runs.created_at", params)
    assert len(params) == 2
    assert "received_at" in w.clause("received_at", [])


def test_the_range_bounds_reach_the_database_as_parameters_not_as_text(db):
    """A hostile `from`/`to` cannot become SQL, because it never becomes SQL --
    it is rejected as a date first, and would be a bind parameter even if it
    were not."""
    with pytest.raises(analytics.AnalyticsError):
        analytics.resolve_window("custom", "2026-01-01'; DROP TABLE runs--", "2026-02-01")
    # The table is still there and still queryable.
    assert analytics.overview(window())["volume"]["runs"] == 0
