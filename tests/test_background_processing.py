"""Processing survives the browser going away.

THE BUG THIS FILE PINS

`POST /api/runs/stream` drove the nine-stage pipeline from inside the body of
its own StreamingResponse. Starlette runs that body and a disconnect listener
in one task group and cancels the group the moment `http.disconnect` arrives
(starlette/responses.py, StreamingResponse.__call__), so a browser refresh
cancelled the pipeline part-way through -- and since `runs` is written once, at
the DECISION stage, nothing at all was persisted. The upload had not failed; it
had never happened.

WHAT IS UNDER TEST

That the upload endpoint and the work are now separate: `POST /api/runs`
returns a job id immediately, the reading happens on a worker that holds no
reference to the request, and the DATABASE answers what happened. Every claim
here is driven over real HTTP through the real app, because the claim is about
what a browser gets back.

No provider keys are set (the `db` fixture removes them), so extraction takes
the deterministic regex route. This file is about the job lifecycle, not about
extraction quality -- which is exactly the point: nothing in the fix touches
how an invoice is read or judged.
"""
import io
import json
import os
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
SAMPLES = os.path.join(ROOT, "sample_invoices")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import config      # noqa: E402
import jobs        # noqa: E402
import main        # noqa: E402
import ratelimit   # noqa: E402
import storage     # noqa: E402
import pg_schema   # noqa: E402
from conftest import auth_headers   # noqa: E402

HAPPY = os.path.join(SAMPLES, "01_happy_path_acme.pdf")
SECOND = os.path.join(SAMPLES, "02_split_po_globex_a.pdf")


def pdf_bytes(path=HAPPY):
    with open(path, "rb") as f:
        return f.read()


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    ratelimit.limiter.reset()
    yield schema
    pg_schema.drop_schema(schema)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


def upload(client, name="invoice.pdf", data=None, user="alice", role="analyst"):
    return client.post(
        "/api/runs",
        files={"file": (name, io.BytesIO(pdf_bytes() if data is None else data),
                        "application/pdf")},
        headers=auth_headers(role, username=user))


def settle(client, job_id, timeout=90.0):
    """Poll the job the way a returning browser does, and return it settled.

    Polling rather than reaching into the worker, deliberately: the guarantee
    being tested is that the DATABASE knows, and reading it any other way would
    prove something else.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get("/api/jobs/" + job_id, headers=auth_headers("viewer"))
        assert r.status_code == 200, r.text
        job = r.json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.15)
    pytest.fail("job " + job_id + " never settled: " + json.dumps(job))


# ==========================================================================
# 1. the upload returns before the work does
# ==========================================================================

def test_upload_returns_a_job_id_and_does_not_wait_for_the_verdict(client):
    r = upload(client)
    assert r.status_code == 202          # accepted, nothing decided yet
    job = r.json()
    assert job["job_id"]
    assert job["status"] in ("queued", "processing")
    assert job["filename"] == "invoice.pdf"
    assert job["run_id"] is None         # no run exists yet, by definition
    assert job["duplicate"] is False


def test_the_response_carries_no_extraction_result(client):
    """The point of 202 is that there is nothing to report yet. A response that
    already carried the verdict would mean the request had waited for it."""
    job = upload(client).json()
    assert job["result"] is None
    assert job["error_message"] is None


# ==========================================================================
# 2. TEST 1 / TEST 4 -- the work outlives the request that started it
# ==========================================================================

def test_processing_completes_after_the_uploading_request_is_gone(client):
    """The uploading request returns and is finished; the invoice is read
    anyway, and a completely separate request finds the result.

    This is the refresh case with the refresh removed: there is no connection
    left over from the upload for anything to depend on.
    """
    job_id = upload(client, name="01_happy_path_acme.pdf").json()["job_id"]

    job = settle(client, job_id)
    assert job["status"] == "completed"
    assert job["run_id"] is not None
    assert job["run_status"] in ("APPROVED", "NEEDS_REVIEW", "REJECTED")
    assert job["processing_started_at"] and job["processing_completed_at"]

    # And the run really is in the ledger, readable by the ordinary endpoint
    # every screen already uses.
    run = client.get("/api/runs/" + str(job["run_id"]),
                     headers=auth_headers("viewer")).json()
    assert run["id"] == job["run_id"]
    assert run["status"] == job["run_status"]


def test_a_returning_reader_finds_the_job_without_having_kept_anything(client):
    """TEST 4: close the tab, come back later, ask again.

    Modelled honestly -- a brand new TestClient shares no state at all with the
    one that uploaded (no connection, no client-side memory), which is a
    stronger condition than a page reload.
    """
    from fastapi.testclient import TestClient
    job_id = upload(client).json()["job_id"]
    settle(client, job_id)

    with TestClient(main.app) as returning:
        r = returning.get("/api/jobs/" + job_id, headers=auth_headers("viewer"))
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        assert r.json()["run_id"] is not None


def test_an_in_flight_job_is_listable_as_active(client):
    """What a reloaded page actually asks: is anything of mine still running?"""
    seen_active = []

    def watch():
        # Poll the list from the moment the upload is accepted. The pipeline
        # paces itself with small sleeps, so an active job is observable.
        for _ in range(200):
            r = client.get("/api/jobs?active=1&mine=1",
                           headers=auth_headers("viewer", username="alice"))
            if r.status_code == 200 and r.json():
                seen_active.append(r.json())
                return
            time.sleep(0.05)

    watcher = threading.Thread(target=watch)
    watcher.start()
    job_id = upload(client).json()["job_id"]
    watcher.join(timeout=30)
    settle(client, job_id)

    assert seen_active, "an in-flight job was never visible as active"
    assert any(j["job_id"] == job_id for j in seen_active[0])
    assert all(j["status"] in ("queued", "processing") for j in seen_active[0])


def test_stage_progress_is_readable_while_the_job_runs(client):
    """The stages a browser would have watched are written as they happen, so a
    reconnecting page shows progress rather than a blank."""
    job_id = upload(client).json()["job_id"]
    job = settle(client, job_id)
    names = [s["name"] for s in job["stages"]]
    assert names[0] == "INGEST"
    assert "DECISION" in names
    # The same stage list the run itself recorded -- one pipeline, one account
    # of what it did.
    run = client.get("/api/runs/" + str(job["run_id"]),
                     headers=auth_headers("viewer")).json()
    assert [s["name"] for s in run["stages"]] == names


# ==========================================================================
# 3. TEST 2 -- one upload is one job, however many times it is submitted
# ==========================================================================

def test_resubmitting_the_same_upload_returns_the_same_job(client):
    """A double-clicked button, a retried fetch, a re-rendered form."""
    body = pdf_bytes()
    first = upload(client, data=body).json()
    second = upload(client, data=body).json()

    assert second["job_id"] == first["job_id"]
    assert second["duplicate"] is True
    assert first["duplicate"] is False

    settle(client, first["job_id"])
    assert len(storage.list_processing_jobs()) == 1


def test_concurrent_identical_submissions_produce_exactly_one_job(client):
    """The guard is the database's, so it holds however the race is timed.

    Ten real threads, released together, all posting the same PDF as the same
    user. A check-then-insert in Python would let several through.
    """
    body = pdf_bytes()
    barrier = threading.Barrier(10)
    ids, errors = [], []

    def submit():
        try:
            barrier.wait(timeout=30)
            r = upload(client, data=body)
            assert r.status_code == 202, r.text
            ids.append(r.json()["job_id"])
        except Exception as exc:            # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert len(set(ids)) == 1, "the same upload produced several jobs"
    settle(client, ids[0])
    assert len(storage.list_processing_jobs()) == 1
    # And exactly one run came out of it.
    assert len(storage.list_runs()) == 1


def test_a_job_can_only_be_claimed_once(client):
    """The second half of the guard, at the level it protects: even if a job
    reached two workers, only one of them may do the work."""
    job, _ = storage.create_processing_job(
        job_id="claim-race", filename="x.pdf", size_bytes=1,
        idempotency_key="k-claim-race", submitted_by="alice")
    assert job["status"] == "queued"

    won = []
    barrier = threading.Barrier(8)

    def race():
        barrier.wait(timeout=30)
        if storage.claim_processing_job("claim-race"):
            won.append(1)

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(won) == 1
    assert storage.get_processing_job("claim-race")["status"] == "processing"


def test_a_job_handed_to_two_workers_is_read_only_once(client):
    """The worker's own half of the guard, exercised where it lives.

    ADDED BECAUSE A MUTATION FOUND IT MISSING. Removing the claim check from
    `jobs._work` broke nothing: the claim test above drives
    `storage.claim_processing_job` directly, and nothing drove the WORKER
    twice. So this submits one job id to the pool twice -- the shape a retry
    or a redelivery would take -- and requires exactly one run to come out.
    """
    body = pdf_bytes()
    job, _ = storage.create_processing_job(
        job_id="double-submit", filename="d.pdf", size_bytes=len(body),
        idempotency_key="k-double-submit", submitted_by="alice")
    assert job["status"] == "queued"

    jobs.submit("double-submit", "d.pdf", body, uploaded_by="alice")
    jobs.submit("double-submit", "d.pdf", body, uploaded_by="alice")

    settled = settle(client, "double-submit")
    assert settled["status"] == "completed"
    # Give the loser every chance to have done work it should not have.
    time.sleep(1.0)
    assert len(storage.list_runs()) == 1, "the same job produced two runs"


def test_a_settled_job_frees_the_key_so_the_duplicate_rule_still_applies(client):
    """The dedupe window is deliberately narrow.

    Uploading the same invoice again LATER must create a real second run and be
    caught by rules.duplicate_check -- the AP control -- rather than silently
    handed back the old job. Merging the two would have quietly replaced a
    business rule with a plumbing convenience.
    """
    body = pdf_bytes()
    first = settle(client, upload(client, data=body).json()["job_id"])
    assert first["status"] == "completed"

    again = upload(client, data=body).json()
    assert again["duplicate"] is False
    assert again["job_id"] != first["job_id"]

    second = settle(client, again["job_id"])
    assert second["status"] == "completed"
    assert second["run_id"] != first["run_id"]
    assert second["run_status"] == "REJECTED"        # the duplicate rule, intact


# ==========================================================================
# 4. TEST 3 -- concurrent invoices stay separate
# ==========================================================================

def test_two_invoices_uploaded_together_stay_independent(client):
    """Two different PDFs, submitted at once. Neither may take the other's run,
    status, filename or extracted data."""
    a_bytes = pdf_bytes(HAPPY)
    b_bytes = pdf_bytes(SECOND)
    results = {}
    barrier = threading.Barrier(2)

    def go(label, body, name):
        barrier.wait(timeout=30)
        results[label] = upload(client, name=name, data=body).json()

    threads = [
        threading.Thread(target=go, args=("a", a_bytes, "01_happy_path_acme.pdf")),
        threading.Thread(target=go, args=("b", b_bytes, "02_split_po_globex_a.pdf")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    a = settle(client, results["a"]["job_id"])
    b = settle(client, results["b"]["job_id"])

    assert a["job_id"] != b["job_id"]
    assert a["run_id"] != b["run_id"]
    assert a["filename"] == "01_happy_path_acme.pdf"
    assert b["filename"] == "02_split_po_globex_a.pdf"
    assert a["status"] == b["status"] == "completed"

    # The result attached to each job is the run that job produced, not the
    # other one's.
    for job in (a, b):
        run = client.get("/api/runs/" + str(job["run_id"]),
                         headers=auth_headers("viewer")).json()
        assert run["filename"] == job["filename"]
        assert run["status"] == job["run_status"]
    assert a["result"]["filename"] != b["result"]["filename"]


# ==========================================================================
# 5. TEST 5 -- a failure is recorded, and stays recorded
# ==========================================================================

def test_a_failing_pipeline_leaves_the_job_failed_with_a_message(client, monkeypatch):
    """Forced failure: the pipeline raises. The job must end `failed`, with
    something a person can read, and must not be retried behind their back."""
    def boom(*args, **kwargs):
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(main.extraction, "extract_text", boom)

    job_id = upload(client).json()["job_id"]
    job = settle(client, job_id)

    assert job["status"] == "failed"
    assert job["run_id"] is None
    assert job["error_message"]
    assert job["processing_completed_at"]

    # Asked again -- the refresh -- it says the same thing, and nothing has
    # started a second attempt.
    again = client.get("/api/jobs/" + job_id, headers=auth_headers("viewer")).json()
    assert again["status"] == "failed"
    assert again["error_message"] == job["error_message"]
    assert len(storage.list_processing_jobs()) == 1


def test_an_interrupted_job_is_closed_out_at_startup_not_left_running(client):
    """A container replaced mid-invoice. The worker pool lives in the process
    that went away, so a job still marked `processing` is nobody's work.

    Reporting "still processing" for ever would be the worse answer: it is not
    true, and there is nothing a reader could do about it.
    """
    storage.create_processing_job(job_id="orphan", filename="o.pdf", size_bytes=1,
                                  idempotency_key="k-orphan", submitted_by="alice")
    assert storage.claim_processing_job("orphan")
    assert storage.get_processing_job("orphan")["status"] == "processing"

    closed = storage.abandon_stale_jobs("The server restarted.")
    assert closed == 1
    orphan = storage.get_processing_job("orphan")
    assert orphan["status"] == "failed"
    assert "restarted" in orphan["error_message"]


# ==========================================================================
# 6. TEST 6 -- a finished job stays finished
# ==========================================================================

def test_a_completed_job_still_carries_its_result_when_asked_again(client):
    job_id = upload(client).json()["job_id"]
    first = settle(client, job_id)
    assert first["status"] == "completed"

    for _ in range(3):
        again = client.get("/api/jobs/" + job_id,
                           headers=auth_headers("viewer")).json()
        assert again["status"] == "completed"
        assert again["run_id"] == first["run_id"]
        assert again["result"]["extracted"] == first["result"]["extracted"]
        assert again["result"]["status"] == first["run_status"]

    # Exactly one run, still. Asking about a finished job does not re-run it.
    assert len(storage.list_runs()) == 1


# ==========================================================================
# 7. authorization -- the new surface is guarded like the old one
# ==========================================================================

def test_uploading_needs_the_processing_scope(client):
    r = client.post("/api/runs",
                    files={"file": ("x.pdf", io.BytesIO(pdf_bytes()),
                                    "application/pdf")},
                    headers=auth_headers("viewer"))
    assert r.status_code == 403
    assert storage.list_processing_jobs() == []


def test_the_job_endpoints_need_a_token(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs/anything").status_code == 401
    assert client.post("/api/runs", files={
        "file": ("x.pdf", io.BytesIO(pdf_bytes()), "application/pdf")}).status_code == 401


def test_a_non_pdf_is_refused_before_any_job_exists(client):
    r = client.post("/api/runs",
                    files={"file": ("x.pdf", io.BytesIO(b"not a pdf at all"),
                                    "application/pdf")},
                    headers=auth_headers("analyst"))
    assert r.status_code == 415
    assert storage.list_processing_jobs() == []


def test_an_unknown_job_is_a_404(client):
    assert client.get("/api/jobs/nope", headers=auth_headers("viewer")).status_code == 404


def test_the_job_record_never_carries_the_dedupe_key(client):
    """The key is a hash of the invoice's own bytes. It is an internal
    mechanism, not a field anybody is owed, and it is not selected into any
    response."""
    job_id = upload(client).json()["job_id"]
    settle(client, job_id)
    body = client.get("/api/jobs/" + job_id, headers=auth_headers("viewer")).text
    assert "idempotency_key" not in body


# ==========================================================================
# 8. nothing that already worked stopped working
# ==========================================================================

def test_the_streaming_endpoint_still_works_unchanged(client):
    """Kept exactly as it was: it is a working API for a caller that does hold
    the connection open, and it is what most of this suite drives."""
    r = client.post("/api/runs/stream",
                    files={"file": ("s.pdf", io.BytesIO(pdf_bytes()),
                                    "application/pdf")},
                    headers=auth_headers("analyst"))
    assert r.status_code == 200
    final = None
    for line in r.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if payload["type"] == "final":
                final = payload["result"]
    assert final and final["run_id"]
    # And it created no job -- the two doors are separate, and the streaming
    # one is unchanged.
    assert storage.list_processing_jobs() == []


def test_both_doors_produce_the_same_shape_of_run(client):
    """One pipeline, two doors. The background path must not be a second,
    quietly-drifting implementation."""
    streamed = client.post("/api/runs/stream",
                           files={"file": ("01_happy_path_acme.pdf",
                                           io.BytesIO(pdf_bytes()),
                                           "application/pdf")},
                           headers=auth_headers("analyst"))
    stream_final = [json.loads(l[6:]) for l in streamed.text.splitlines()
                    if l.startswith("data: ")]
    stream_result = [p["result"] for p in stream_final if p["type"] == "final"][0]

    job = settle(client, upload(client, name="01_happy_path_acme.pdf",
                                data=pdf_bytes(SECOND)).json()["job_id"])
    background = job["result"]

    assert [s["name"] for s in background["stages"]] == \
           [s["name"] for s in stream_result["stages"]]
    assert set(background["audit"]) == set(stream_result["audit"])


def test_resetting_the_demo_clears_the_jobs_with_the_runs(client):
    """A job is a record OF a run being produced. Once the run is gone the job
    is answering a question about nothing."""
    settle(client, upload(client).json()["job_id"])
    assert storage.list_processing_jobs()

    storage.clear_run_history()
    assert storage.list_processing_jobs() == []
    assert storage.list_runs() == []


def test_the_worker_pool_is_not_started_by_importing_anything(client):
    """Importing the app opens no threads -- the suite imports it hundreds of
    times, and a pool per import would be a pool per import."""
    assert jobs.in_flight() == 0
