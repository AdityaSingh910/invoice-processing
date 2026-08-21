"""Phase D: multi-user collaboration -- review claims and activity history.

THE PROPERTY THAT MATTERS

Two employees can look at the same NEEDS_REVIEW invoice at the same time and
the database, not the frontend, decides who owns reviewing it:

    Employee A: POST .../review/claim  -> 200, claim_id, expires_at
    Employee B: POST .../review/claim  -> 409, "currently being reviewed by A"

Enforced with a row lock (`SELECT ... FOR UPDATE` on the run itself, in
storage.claim_review), the same tool save_run_checked already uses to
serialise two invoices racing one PO -- here it serialises two employees
racing one run. The concurrency tests below exercise that lock with real
threads against the real Postgres schema, not a mock.

Activity history is a SEPARATE concept from the deterministic audit trail
(rules.build_audit): the audit trail explains why the rules decided what they
decided; invoice_activity records what people (and the system, on their
behalf) did about it afterwards, and is append-only -- a later event must
never erase an earlier one.
"""
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import config     # noqa: E402
import main       # noqa: E402
import matching   # noqa: E402
import rules      # noqa: E402
import storage    # noqa: E402
import pg_schema  # noqa: E402

VENDOR = "Globex Logistics"      # approved; holds PO-1002 at $5,000
PO = "PO-1002"


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    yield schema
    pg_schema.drop_schema(schema)


@pytest.fixture
def client(db):
    from conftest import auth_headers
    from fastapi.testclient import TestClient
    with TestClient(main.app, headers=auth_headers("reviewer", "a.singh")) as c:
        assert storage.PG_SCHEMA == db, "startup must not restore the real schema"
        yield c


def submit(total, invoice_number, po=PO, vendor=VENDOR, uploaded_by="analyst-1"):
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
        tolerance_for=matching.tolerance_for, audit=audit, uploaded_by=uploaded_by)
    return run_id, final_status


def held_invoice(uploaded_by="analyst-1"):
    """A run the rules held: $6,000 against an untouched $5,000 PO."""
    run_id, status = submit(6000.00, "INV-OVER", uploaded_by=uploaded_by)
    assert status == "NEEDS_REVIEW"
    return run_id


# --------------------------------------------------------------------------
# 1. claiming -- storage layer
# --------------------------------------------------------------------------

def test_claim_succeeds_on_an_unclaimed_needs_review_run(db):
    run_id = held_invoice()
    result = storage.claim_review(run_id, "alice")
    assert result["ok"] is True
    assert result["claimed_by"] == "alice"
    assert result["renewed"] is False
    assert storage.get_active_claim(run_id)["claimed_by"] == "alice"


def test_a_second_employee_cannot_claim_an_actively_claimed_run(db):
    run_id = held_invoice()
    storage.claim_review(run_id, "alice")
    result = storage.claim_review(run_id, "bob")
    assert result["ok"] is False
    assert result["error"] == "claimed"
    assert result["claimed_by"] == "alice"


def test_claiming_twice_with_the_same_identity_renews_rather_than_conflicts(db):
    """A retried or double-submitted claim request must not read as a conflict."""
    run_id = held_invoice()
    first = storage.claim_review(run_id, "alice")
    second = storage.claim_review(run_id, "alice")
    assert second["ok"] is True
    assert second["renewed"] is True
    assert second["claim_id"] == first["claim_id"]


def test_release_frees_the_run_for_another_employee(db):
    run_id = held_invoice()
    storage.claim_review(run_id, "alice")
    released = storage.release_review_claim(run_id, "alice")
    assert released["ok"] is True
    assert storage.get_active_claim(run_id) is None

    result = storage.claim_review(run_id, "bob")
    assert result["ok"] is True
    assert result["claimed_by"] == "bob"


def test_only_the_claim_holder_may_release_it(db):
    run_id = held_invoice()
    storage.claim_review(run_id, "alice")
    result = storage.release_review_claim(run_id, "bob")
    assert result["ok"] is False
    assert storage.get_active_claim(run_id)["claimed_by"] == "alice"


def test_an_admin_may_force_release_someone_elses_claim(db):
    run_id = held_invoice()
    storage.claim_review(run_id, "alice")
    result = storage.release_review_claim(run_id, "admin-1", is_admin=True)
    assert result["ok"] is True
    assert storage.get_active_claim(run_id) is None


def test_releasing_with_no_active_claim_is_refused_not_a_500(db):
    run_id = held_invoice()
    result = storage.release_review_claim(run_id, "alice")
    assert result["ok"] is False
    assert "no active claim" in result["error"]


def test_cannot_claim_an_approved_run(db):
    run_id, status = submit(3000.00, "INV-OK")
    assert status == "APPROVED"
    result = storage.claim_review(run_id, "alice")
    assert result["ok"] is False
    assert "NEEDS_REVIEW" in result["error"]


def test_claiming_an_unknown_run_is_refused(db):
    assert storage.claim_review(999999, "alice")["ok"] is False


# --------------------------------------------------------------------------
# 2. stale claim recovery
# --------------------------------------------------------------------------

def test_an_expired_claim_can_be_taken_over(db):
    """Simulates an abandoned tab: the lease is backdated directly, rather
    than sleeping the test for real minutes."""
    run_id = held_invoice()
    claimed = storage.claim_review(run_id, "alice", lease_minutes=15)
    assert claimed["ok"] is True

    # Backdate the lease so it reads as already expired.
    conn = storage.get_conn()
    conn.execute("UPDATE review_claims SET expires_at=%s WHERE id=%s",
                ("2000-01-01T00:00:00+00:00", claimed["claim_id"]))
    conn.commit()
    conn.close()

    assert storage.get_active_claim(run_id) is None, "an expired claim must not read as active"

    result = storage.claim_review(run_id, "bob")
    assert result["ok"] is True
    assert result["claimed_by"] == "bob"

    # The expired claim was closed out, not silently overwritten -- the
    # history still shows what happened to it.
    activity = storage.list_activity(run_id)
    expired_events = [a for a in activity if a["event_type"] == "REVIEW_RELEASED"
                      and a.get("metadata", {}).get("previous_holder") == "alice"]
    assert len(expired_events) == 1


def test_a_stale_claim_never_permanently_blocks_the_run(db):
    """However long a claim sits abandoned, the run remains claimable."""
    run_id = held_invoice()
    storage.claim_review(run_id, "alice", lease_minutes=1)
    conn = storage.get_conn()
    conn.execute("UPDATE review_claims SET expires_at=%s WHERE run_id=%s",
                ("1999-01-01T00:00:00+00:00", run_id))
    conn.commit()
    conn.close()

    result = storage.claim_review(run_id, "carol")
    assert result["ok"] is True


# --------------------------------------------------------------------------
# 3. concurrency -- the real race, under real threads, against real Postgres
# --------------------------------------------------------------------------

def test_simultaneous_claims_produce_exactly_one_winner(db):
    """Ten employees click 'start review' on the same invoice at once.

    Exactly one must win; the other nine must see a controlled conflict, not
    a 500, and not a silent double-claim. This exercises the actual
    `SELECT ... FOR UPDATE` row lock in storage.claim_review, not a mock --
    same pattern as test_po_edge_cases.py's PO-race test.
    """
    run_id = held_invoice()
    n = 10
    results, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker(i):
        barrier.wait()
        r = storage.claim_review(run_id, f"employee-{i}")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r["ok"]]
    losers = [r for r in results if not r["ok"]]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
    assert len(losers) == n - 1
    assert all(r["error"] == "claimed" for r in losers)

    winner_name = winners[0]["claimed_by"]
    assert all(r["claimed_by"] == winner_name for r in losers), \
        "every loser must be told the same, correct current holder"

    # The database agrees with the outcome the threads observed.
    assert storage.get_active_claim(run_id)["claimed_by"] == winner_name

    # Only one active claim exists -- no duplicate active rows.
    conn = storage.get_conn()
    cur = conn.execute(
        "SELECT COUNT(*) AS c FROM review_claims WHERE run_id=%s AND released_at IS NULL",
        (run_id,))
    count = cur.fetchone()["c"]
    conn.close()
    assert count == 1


# --------------------------------------------------------------------------
# 4. human decisions respect an active claim
# --------------------------------------------------------------------------

def test_the_claim_holder_can_still_submit_the_decision(db):
    run_id = held_invoice()
    storage.claim_review(run_id, "alice")
    result = storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")
    assert result["ok"] is True


def test_someone_other_than_the_claim_holder_cannot_submit_the_decision(db):
    run_id = held_invoice()
    storage.claim_review(run_id, "alice")
    result = storage.record_human_review(run_id, "ACCEPTED", reviewer="bob")
    assert result["ok"] is False
    assert result["error"] == "claimed"
    assert result["claimed_by"] == "alice"
    assert storage.get_run(run_id)["human_decision"] is None


def test_an_unclaimed_run_can_still_be_reviewed_directly(db):
    """Claiming is optional -- every review submitted before Phase D existed
    looked exactly like this, and must keep working."""
    run_id = held_invoice()
    result = storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")
    assert result["ok"] is True


def test_completing_a_review_releases_the_claim_it_was_submitted_under(db):
    run_id = held_invoice()
    storage.claim_review(run_id, "alice")
    storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")
    assert storage.get_active_claim(run_id) is None


# --------------------------------------------------------------------------
# 4b. Phase E -- decision atomicity under concurrency
#
# record_human_review()'s eligibility check and its write used to happen
# across three separate, unlocked transactions. Two callers could both read
# human_decision IS NULL before either committed, and both would then write --
# corrupting the activity history with two conflicting rulings on one run.
# Fixed by moving the whole check-then-act sequence under one
# SELECT ... FOR UPDATE on the run row, the same lock claim_review() already
# uses. These tests exercise the actual race under real threads against real
# Postgres, the same pattern test_simultaneous_claims_produce_exactly_one_winner
# already established above -- not a mock, not a sleep-based approximation.
# --------------------------------------------------------------------------

def test_concurrent_conflicting_decisions_only_one_wins(db):
    """Ten reviewers race to decide the same unclaimed run at once -- half try
    ACCEPT, half try REJECT. Exactly one decision may land; every other
    attempt must be refused, never silently applied on top."""
    run_id = held_invoice()
    n = 10
    results, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker(i):
        decision = "ACCEPTED" if i % 2 == 0 else "REJECTED"
        barrier.wait()
        r = storage.record_human_review(run_id, decision, reviewer=f"employee-{i}")
        with lock:
            results.append((decision, r))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [(d, r) for d, r in results if r["ok"]]
    losers = [(d, r) for d, r in results if not r["ok"]]
    assert len(winners) == 1, f"expected exactly one decision to land, got {len(winners)}"
    assert len(losers) == n - 1
    assert all(r["error"] == "already been reviewed" or "already been reviewed" in r["error"]
              for _, r in losers)

    # The database agrees with the one decision that actually won.
    winning_decision, _ = winners[0]
    run = storage.get_run(run_id)
    assert run["human_decision"] == winning_decision
    assert run["status"] == ("APPROVED" if winning_decision == "ACCEPTED" else "REJECTED")

    # The activity history carries exactly that one ruling -- not a mix of
    # ACCEPTED and REJECTED entries from callers that should have been refused.
    activity = storage.list_activity(run_id)
    decision_events = [a["event_type"] for a in activity if a["event_type"] in ("ACCEPTED", "REJECTED")]
    assert decision_events == [winning_decision]


def test_concurrent_duplicate_accepts_only_one_lands(db):
    """The classic double-click: the same decision, submitted at once by the
    same reviewer. Exactly one must be recorded; the ledger must not see the
    invoice approved twice or its activity doubled."""
    run_id = held_invoice()
    n = 8
    results, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker():
        barrier.wait()
        r = storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r["ok"]]
    assert len(winners) == 1

    activity = storage.list_activity(run_id)
    accepted_events = [a for a in activity if a["event_type"] == "ACCEPTED"]
    assert len(accepted_events) == 1

    # Consumption is derived from run history, so a double-application would
    # show up as the PO balance being charged twice for one invoice.
    remaining = storage.remaining_for_po(PO)
    consumed = storage.consumed_amount_for_po(PO)
    assert consumed == 6000.00, "the $6,000 invoice must be charged to the PO exactly once"
    assert remaining == round(5000.00 - 6000.00, 2)


def test_release_on_an_unknown_run_is_refused_not_a_500(db):
    result = storage.release_review_claim(999999, "alice")
    assert result["ok"] is False
    assert result["error"] == "unknown run"


# --------------------------------------------------------------------------
# 5. activity history -- chronology, immutability, actor attribution
# --------------------------------------------------------------------------

def test_processing_produces_an_activity_entry(db):
    run_id, _ = submit(3000.00, "INV-OK", uploaded_by="analyst-1")
    activity = storage.list_activity(run_id)
    types = [a["event_type"] for a in activity]
    assert "PROCESSING_COMPLETED" in types
    entry = next(a for a in activity if a["event_type"] == "PROCESSING_COMPLETED")
    assert entry["actor"] == "analyst-1"


def test_a_held_run_logs_review_required(db):
    run_id = held_invoice()
    types = [a["event_type"] for a in storage.list_activity(run_id)]
    assert "REVIEW_REQUIRED" in types


def test_an_approved_run_does_not_log_review_required(db):
    run_id, status = submit(3000.00, "INV-OK")
    assert status == "APPROVED"
    types = [a["event_type"] for a in storage.list_activity(run_id)]
    assert "REVIEW_REQUIRED" not in types


def test_claim_and_release_are_both_logged_with_the_correct_actor(db):
    run_id = held_invoice()
    storage.claim_review(run_id, "alice")
    storage.release_review_claim(run_id, "alice")

    activity = storage.list_activity(run_id)
    claimed = next(a for a in activity if a["event_type"] == "REVIEW_CLAIMED")
    released = next(a for a in activity if a["event_type"] == "REVIEW_RELEASED")
    assert claimed["actor"] == "alice"
    assert released["actor"] == "alice"


def test_a_decision_is_logged_under_its_own_event_type(db):
    run_id = held_invoice()
    storage.record_human_review(run_id, "REJECTED", reviewer="alice", note="Not authorised.")
    entry = next(a for a in storage.list_activity(run_id) if a["event_type"] == "REJECTED")
    assert entry["actor"] == "alice"
    assert entry["note"] == "Not authorised."


def test_a_comment_is_logged_without_deciding_anything(db):
    run_id = held_invoice()
    result = storage.add_comment(run_id, "alice", "Checking with procurement first.")
    assert result["ok"] is True

    run = storage.get_run(run_id)
    assert run["human_decision"] is None, "a comment must not itself be a ruling"

    entry = next(a for a in storage.list_activity(run_id) if a["event_type"] == "COMMENT_ADDED")
    assert entry["actor"] == "alice"
    assert entry["note"] == "Checking with procurement first."


def test_an_empty_comment_is_refused(db):
    run_id = held_invoice()
    result = storage.add_comment(run_id, "alice", "   ")
    assert result["ok"] is False


def test_activity_is_chronological(db):
    run_id = held_invoice()
    storage.claim_review(run_id, "alice")
    storage.add_comment(run_id, "alice", "Looks fine.")
    storage.record_human_review(run_id, "ACCEPTED", reviewer="alice")

    activity = storage.list_activity(run_id)
    timestamps = [a["created_at"] for a in activity]
    assert timestamps == sorted(timestamps)
    ids = [a["id"] for a in activity]
    assert ids == sorted(ids)


def test_history_survives_a_cascade_and_records_the_auto_approval(db):
    """A reversal that frees budget and auto-approves a held invoice must
    still show up in that invoice's own history, attributed to the system."""
    id1, s1 = submit(4000.00, "INV-A")
    id2, s2 = submit(3000.00, "INV-B")
    assert (s1, s2) == ("APPROVED", "NEEDS_REVIEW")

    storage.set_run_status(id1, "REJECTED", "Reversed: goods returned.")
    cascaded = rules.reevaluate_po_queue(PO, triggered_by=id1)
    assert [c["run_id"] for c in cascaded] == [id2]

    activity = storage.list_activity(id2)
    auto = next(a for a in activity if a["event_type"] == "AUTO_APPROVED")
    assert auto["actor"] is None
    assert auto["metadata"]["po_number"] == PO


def test_a_claim_open_when_the_run_is_auto_approved_is_released(db):
    id1, s1 = submit(4000.00, "INV-A")
    id2, s2 = submit(3000.00, "INV-B")
    assert (s1, s2) == ("APPROVED", "NEEDS_REVIEW")

    storage.claim_review(id2, "alice")
    storage.set_run_status(id1, "REJECTED", "Reversed.")
    rules.reevaluate_po_queue(PO, triggered_by=id1)

    assert storage.get_active_claim(id2) is None


# --------------------------------------------------------------------------
# 6. HTTP layer -- authentication, authorization, endpoint behaviour
# --------------------------------------------------------------------------

def test_claim_requires_authentication(db):
    from fastapi.testclient import TestClient
    run_id = held_invoice()
    with TestClient(main.app) as c:
        assert storage.PG_SCHEMA == db
        r = c.post(f"/api/runs/{run_id}/review/claim")
    assert r.status_code == 401


def test_a_viewer_cannot_claim_a_review(db):
    from conftest import auth_headers
    from fastapi.testclient import TestClient
    run_id = held_invoice()
    with TestClient(main.app, headers=auth_headers("viewer")) as c:
        assert storage.PG_SCHEMA == db
        r = c.post(f"/api/runs/{run_id}/review/claim")
    assert r.status_code == 403


def test_claim_and_conflict_over_http(client):
    from conftest import auth_headers
    run_id = held_invoice()
    r1 = client.post(f"/api/runs/{run_id}/review/claim", headers=auth_headers("reviewer", "alice"))
    assert r1.status_code == 200
    assert r1.json()["claimed_by"] == "alice"

    r2 = client.post(f"/api/runs/{run_id}/review/claim", headers=auth_headers("reviewer", "bob"))
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"]["claimed_by"] == "alice"


def test_reviewer_identity_over_http_is_always_the_authenticated_caller(client):
    """No request body field can override who the claim belongs to -- the
    claim endpoint takes no body at all, so there is nothing to spoof."""
    run_id = held_invoice()
    r = client.post(f"/api/runs/{run_id}/review/claim", json={"claimed_by": "someone-else"})
    assert r.status_code == 200
    assert r.json()["claimed_by"] == "a.singh"


def test_release_and_reclaim_over_http(client):
    from conftest import auth_headers
    run_id = held_invoice()
    client.post(f"/api/runs/{run_id}/review/claim", headers=auth_headers("reviewer", "alice"))

    r = client.post(f"/api/runs/{run_id}/review/release", headers=auth_headers("reviewer", "bob"))
    assert r.status_code == 409, "bob does not hold the claim"

    r = client.post(f"/api/runs/{run_id}/review/release", headers=auth_headers("reviewer", "alice"))
    assert r.status_code == 200

    r = client.post(f"/api/runs/{run_id}/review/claim", headers=auth_headers("reviewer", "bob"))
    assert r.status_code == 200


def test_admin_can_force_release_over_http(client):
    from conftest import auth_headers
    run_id = held_invoice()
    client.post(f"/api/runs/{run_id}/review/claim", headers=auth_headers("reviewer", "alice"))
    r = client.post(f"/api/runs/{run_id}/review/release", headers=auth_headers("admin", "the-admin"))
    assert r.status_code == 200


def test_review_endpoint_refuses_when_claimed_by_someone_else(client):
    from conftest import auth_headers
    run_id = held_invoice()
    client.post(f"/api/runs/{run_id}/review/claim", headers=auth_headers("reviewer", "alice"))

    r = client.post(f"/api/runs/{run_id}/review", headers=auth_headers("reviewer", "bob"),
                    json={"decision": "ACCEPTED"})
    assert r.status_code == 409
    assert "alice" in r.json()["detail"]["error"]


def test_review_endpoint_refuses_a_second_submission_not_a_500(client):
    """A retried or double-clicked Accept over HTTP: the first call decides,
    the second is refused with a 409 naming the conflict, never a 500 and
    never a second ruling applied on top."""
    run_id = held_invoice()
    r1 = client.post(f"/api/runs/{run_id}/review", json={"decision": "ACCEPTED"})
    assert r1.status_code == 200

    r2 = client.post(f"/api/runs/{run_id}/review", json={"decision": "REJECTED"})
    assert r2.status_code == 409
    assert "already been reviewed" in r2.json()["error"]

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["human_decision"] == "ACCEPTED"
    assert run["status"] == "APPROVED"


def test_release_on_an_unknown_run_is_404_over_http(client):
    r = client.post("/api/runs/999999/review/release")
    assert r.status_code == 404


def test_comment_endpoint_over_http(client):
    run_id = held_invoice()
    r = client.post(f"/api/runs/{run_id}/comment", json={"note": "Vendor confirmed by phone."})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_comment_endpoint_requires_review_scope(db):
    from conftest import auth_headers
    from fastapi.testclient import TestClient
    run_id = held_invoice()
    with TestClient(main.app, headers=auth_headers("viewer")) as c:
        assert storage.PG_SCHEMA == db
        r = c.post(f"/api/runs/{run_id}/comment", json={"note": "hi"})
    assert r.status_code == 403


def test_activity_endpoint_over_http(client):
    run_id = held_invoice()
    client.post(f"/api/runs/{run_id}/review/claim")
    r = client.get(f"/api/runs/{run_id}/activity")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert body["current_claim"]["claimed_by"] == "a.singh"
    assert any(a["event_type"] == "REVIEW_CLAIMED" for a in body["activity"])


def test_activity_endpoint_readable_by_viewer_scope(db):
    from conftest import auth_headers
    from fastapi.testclient import TestClient
    run_id = held_invoice()
    with TestClient(main.app, headers=auth_headers("viewer")) as c:
        assert storage.PG_SCHEMA == db
        r = c.get(f"/api/runs/{run_id}/activity")
    assert r.status_code == 200


def test_activity_endpoint_requires_authentication(db):
    from fastapi.testclient import TestClient
    run_id = held_invoice()
    with TestClient(main.app) as c:
        assert storage.PG_SCHEMA == db
        r = c.get(f"/api/runs/{run_id}/activity")
    assert r.status_code == 401


def test_activity_for_an_unknown_run_is_404(client):
    r = client.get("/api/runs/999999/activity")
    assert r.status_code == 404


def test_run_detail_exposes_the_current_claim(client):
    run_id = held_invoice()
    client.post(f"/api/runs/{run_id}/review/claim")
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["current_claim"]["claimed_by"] == "a.singh"


def test_document_views_and_downloads_are_logged(db):
    """Regression-adjacent: document endpoints (Phase C) keep working and now
    also contribute to the activity timeline."""
    import io
    import json as jsonlib
    from conftest import auth_headers
    from fastapi.testclient import TestClient

    sample = os.path.join(ROOT, "sample_invoices", "01_happy_path_acme.pdf")
    with TestClient(main.app) as c:
        assert storage.PG_SCHEMA == db
        with open(sample, "rb") as f:
            r = c.post("/api/runs/stream",
                      files={"file": ("01_happy_path_acme.pdf", io.BytesIO(f.read()),
                                      "application/pdf")},
                      headers=auth_headers("analyst", "uploader-1"))
        assert r.status_code == 200
        run_id = None
        for line in r.text.splitlines():
            if line.startswith("data: "):
                evt = jsonlib.loads(line[6:])
                if evt.get("type") == "final":
                    run_id = evt["result"]["run_id"]
        assert run_id is not None

        c.get(f"/api/runs/{run_id}/document", headers=auth_headers("viewer", "viewer-1"))
        c.get(f"/api/runs/{run_id}/document/download",
             headers=auth_headers("viewer", "viewer-1"))

        activity = storage.list_activity(run_id)

    viewed = next(a for a in activity if a["event_type"] == "DOCUMENT_VIEWED")
    downloaded = next(a for a in activity if a["event_type"] == "DOCUMENT_DOWNLOADED")
    assert viewed["actor"] == "viewer-1"
    assert downloaded["actor"] == "viewer-1"
    assert any(a["event_type"] == "PROCESSING_COMPLETED" and a["actor"] == "uploader-1"
              for a in activity)


def test_status_override_is_logged(client):
    from conftest import auth_headers
    run_id = held_invoice()
    r = client.post(f"/api/runs/{run_id}/status", headers=auth_headers("admin", "the-admin"),
                    json={"status": "REJECTED", "note": "Confirmed fraudulent."})
    assert r.status_code == 200

    activity = client.get(f"/api/runs/{run_id}/activity").json()["activity"]
    entry = next(a for a in activity if a["event_type"] == "STATUS_OVERRIDDEN")
    assert entry["actor"] == "the-admin"
    assert entry["metadata"]["to"] == "REJECTED"


# --------------------------------------------------------------------------
# 7. regression -- reset-demo clears activity and claims with their runs
# --------------------------------------------------------------------------

def test_reset_demo_clears_activity_and_claims(client):
    from conftest import auth_headers
    run_id = held_invoice()
    client.post(f"/api/runs/{run_id}/review/claim")
    assert storage.list_activity(run_id) != []
    assert storage.get_active_claim(run_id) is not None

    r = client.post("/api/admin/reset-demo", headers=auth_headers("admin"))
    assert r.status_code == 200

    assert storage.list_activity(run_id) == []
    assert storage.get_active_claim(run_id) is None
    assert storage.get_run(run_id) is None
