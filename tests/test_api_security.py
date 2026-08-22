"""API security: authentication, authorization, rate limiting, input, secrets.

THE CLAIM UNDER TEST

"A curl request that knows the endpoint should not get further than one that
does not." Everything here is driven over HTTP through the real app, because
that is the only level at which the claim means anything -- calling
`storage.record_human_review()` directly proves nothing about whether the
endpoint in front of it is guarded.

No live Groq or Gemini call is made. Uploads are rejected at validation, or run
through the deterministic regex route with both keys stripped.
"""
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
SAMPLES = os.path.join(ROOT, "sample_invoices")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import auth        # noqa: E402
import config      # noqa: E402
import main        # noqa: E402
import matching    # noqa: E402
import ratelimit   # noqa: E402
import rules       # noqa: E402
import storage     # noqa: E402
import pg_schema   # noqa: E402
from conftest import auth_headers, token_for   # noqa: E402

HAPPY_PDF = os.path.join(SAMPLES, "01_happy_path_acme.pdf")


@pytest.fixture
def db(monkeypatch):
    # fresh_schema() calls the REAL config.load_dotenv() as part of setting up
    # (it needs DATABASE_URL from .env to reach Postgres at all), so it runs
    # BEFORE load_dotenv is stubbed out below -- otherwise DATABASE_URL would
    # never be loaded and every test in this file would fail before even
    # reaching what they are actually testing.
    schema = pg_schema.fresh_schema(monkeypatch)
    # Keep every test in this file off the network and off both quotas.
    #
    # Deleting the variables is not enough on its own: entering the TestClient
    # context fires FastAPI's startup event, which calls config.load_dotenv()
    # and puts the real keys straight back from .env. Found the hard way --
    # these tests were quietly making live Groq calls and taking 90 seconds.
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


def pdf_bytes():
    with open(HAPPY_PDF, "rb") as f:
        return f.read()


def upload(client, headers=None, data=None, name="invoice.pdf", ctype="application/pdf"):
    # `data is not None`, not `data or ...`: b"" is falsy, and the earlier
    # version silently substituted a real PDF for the empty-upload case -- the
    # test passed against a file it was not sending.
    body = pdf_bytes() if data is None else data
    return client.post("/api/runs/stream",
                       files={"file": (name, io.BytesIO(body), ctype)},
                       headers=headers or {})


def held_run():
    """A run the rules held for review, created below the API."""
    extracted = {"vendor_name": "Globex Logistics", "invoice_number": "INV-HELD",
                 "total": 6000.0, "subtotal": 6000.0, "tax": 0.0,
                 "po_references": ["PO-1002"], "currency": "USD",
                 "extraction_method": "regex"}
    info = {"route": "regex", "notes": [], "security_flags": []}
    po_match = matching.match_po(extracted)
    vendor_ok, _, vendor_detail = rules.vendor_check(extracted)
    audit = {}
    status, reasons = rules.decide(
        info, rules.validate_required_fields(extracted), vendor_ok, vendor_detail,
        None, "", po_match, arithmetic=rules.validate_arithmetic(extracted),
        amount=rules.validate_amount(extracted), audit=audit, extracted=extracted)
    assert status == "NEEDS_REVIEW"
    run_id, _, _ = storage.save_run_checked(
        "held.pdf", status, extracted, po_match, [], reasons,
        tolerance_for=matching.tolerance_for, audit=audit)
    return run_id


# --------------------------------------------------------------------------
# 1. authentication
# --------------------------------------------------------------------------

PROTECTED = [
    ("get", "/api/runs"),
    ("get", "/api/runs/1"),
    ("get", "/api/reference"),
    ("get", "/api/sample-invoices"),
    ("get", "/api/sample-invoices/01_happy_path_acme.pdf"),
    ("post", "/api/runs/1/review"),
    ("post", "/api/runs/1/status"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_sensitive_endpoint_refuses_an_anonymous_caller(client, method, path):
    r = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
    assert r.status_code == 401, f"{method.upper()} {path} was reachable anonymously"


def test_processing_endpoint_refuses_an_anonymous_caller(client):
    assert upload(client).status_code == 401


def test_a_401_does_not_leak_whether_the_resource_exists(client):
    """An unauthenticated caller learns nothing about run ids."""
    a = client.get("/api/runs/1")
    b = client.get("/api/runs/999999")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


@pytest.mark.parametrize("header", [
    {"Authorization": "Bearer not-a-token"},
    {"Authorization": "Bearer "},
    {"Authorization": "Basic YWRtaW46YWRtaW4="},
    {"Authorization": "token " + "x" * 40},
])
def test_malformed_or_forged_tokens_are_refused(client, header):
    assert client.get("/api/runs", headers=header).status_code == 401


def test_a_token_signed_with_the_wrong_key_is_refused(client):
    import jwt
    forged = jwt.encode(
        {"sub": "attacker", "scope": "invoice:read invoice:review",
         "iss": config.AUTH_ISSUER, "exp": 9999999999},
        "not-the-signing-secret", algorithm="HS256")
    r = client.get("/api/runs", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


def test_an_expired_token_is_refused(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN_TTL_MINUTES", -1)
    stale = auth.create_access_token({"username": "x", "roles": ["admin"]})["access_token"]
    assert client.get("/api/runs", headers={"Authorization": f"Bearer {stale}"}).status_code == 401


def test_a_token_from_another_issuer_is_refused(client):
    import jwt
    other = jwt.encode({"sub": "x", "scope": "invoice:read", "iss": "some-other-system",
                        "exp": 9999999999},
                       auth.signing_secret(), algorithm="HS256")
    assert client.get("/api/runs", headers={"Authorization": f"Bearer {other}"}).status_code == 401


def test_an_authenticated_authorized_caller_is_allowed(client):
    assert client.get("/api/runs", headers=auth_headers("viewer")).status_code == 200
    assert client.get("/api/reference", headers=auth_headers("viewer")).status_code == 200


def test_health_is_public_and_says_nothing_sensitive(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# --------------------------------------------------------------------------
# 2. the login flow itself
# --------------------------------------------------------------------------

def test_password_grant_issues_a_usable_token(client):
    r = client.post("/api/auth/token", data={"username": "reviewer",
                                             "password": "demo-reviewer"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["access_token"]
    assert "invoice:review" in body["scope"]

    me = client.get("/api/auth/me",
                    headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.json()["username"] == "reviewer"


def test_a_wrong_password_is_refused(client):
    r = client.post("/api/auth/token", data={"username": "reviewer", "password": "wrong"})
    assert r.status_code == 401


def test_login_does_not_reveal_whether_the_account_exists(client):
    a = client.post("/api/auth/token", data={"username": "reviewer", "password": "wrong"})
    b = client.post("/api/auth/token", data={"username": "no-such-person", "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_passwords_are_not_stored_in_the_clear():
    users = auth.load_users()
    assert users, "the demo user store should load"
    for name, u in users.items():
        assert u["password_hash"].startswith("pbkdf2_sha256$")
        assert "demo-" not in u["password_hash"], f"{name}'s password is recoverable"


# --------------------------------------------------------------------------
# 3. authorization
# --------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["ACCEPTED", "REJECTED"])
@pytest.mark.parametrize("role", ["viewer", "analyst"])
def test_a_user_without_the_review_permission_cannot_rule(client, role, decision):
    run_id = held_run()
    r = client.post(f"/api/runs/{run_id}/review", json={"decision": decision},
                    headers=auth_headers(role))
    assert r.status_code == 403, "authenticated but unauthorized must be 403, not 401"
    assert storage.get_run(run_id)["human_decision"] is None, "nothing may have changed"


@pytest.mark.parametrize("decision,final", [("ACCEPTED", "HUMAN_APPROVED"),
                                            ("REJECTED", "HUMAN_REJECTED")])
def test_an_authorized_reviewer_can_rule_on_a_held_invoice(client, decision, final):
    run_id = held_run()
    r = client.post(f"/api/runs/{run_id}/review", json={"decision": decision},
                    headers=auth_headers("reviewer", "r.patel"))
    assert r.status_code == 200
    assert r.json()["final_decision"] == final
    assert storage.get_run(run_id)["automated_decision"] == "NEEDS_REVIEW"


def test_a_reviewer_cannot_rule_on_an_invoice_that_is_not_held(client):
    run_id = held_run()
    hdr = auth_headers("reviewer")
    assert client.post(f"/api/runs/{run_id}/review", json={"decision": "ACCEPTED"},
                       headers=hdr).status_code == 200
    # Second ruling on a run that is no longer NEEDS_REVIEW.
    again = client.post(f"/api/runs/{run_id}/review", json={"decision": "REJECTED"},
                        headers=hdr)
    assert again.status_code == 409


def test_reviewing_an_unknown_run_is_404(client):
    r = client.post("/api/runs/424242/review", json={"decision": "ACCEPTED"},
                    headers=auth_headers("reviewer"))
    assert r.status_code == 404


def test_a_reviewer_cannot_use_the_broad_status_override(client):
    """Reviewing is not the same authority as overriding any run's status."""
    run_id = held_run()
    r = client.post(f"/api/runs/{run_id}/status", json={"status": "APPROVED"},
                    headers=auth_headers("reviewer"))
    assert r.status_code == 403
    assert client.post(f"/api/runs/{run_id}/status", json={"status": "APPROVED"},
                       headers=auth_headers("admin")).status_code == 200


def test_processing_requires_more_than_read_access(client):
    assert upload(client, auth_headers("viewer")).status_code == 403
    assert upload(client, auth_headers("analyst")).status_code == 200


# --------------------------------------------------------------------------
# 4. reviewer identity comes from the token, never the body
# --------------------------------------------------------------------------

def test_the_body_cannot_choose_who_the_reviewer_was(client):
    """The headline audit-security property."""
    run_id = held_run()
    r = client.post(f"/api/runs/{run_id}/review",
                    json={"decision": "ACCEPTED", "reviewer": "admin"},
                    headers=auth_headers("reviewer", "r.patel"))
    assert r.status_code == 200

    run = storage.get_run(run_id)
    assert run["reviewed_by"] == "r.patel", "identity must come from the token"
    assert run["reviewed_by"] != "admin"


def test_the_recorded_review_carries_identity_time_and_both_decisions(client):
    run_id = held_run()
    client.post(f"/api/runs/{run_id}/review", json={"decision": "ACCEPTED"},
                headers=auth_headers("reviewer", "r.patel"))

    run = storage.get_run(run_id)
    assert run["reviewed_by"] == "r.patel"
    assert run["reviewed_at"]
    assert run["automated_decision"] == "NEEDS_REVIEW"
    assert run["human_decision"] == "ACCEPTED"
    assert run["final_decision"] == "HUMAN_APPROVED"


def test_a_client_cannot_rewrite_a_recorded_review(client):
    """History is append-only as far as the API is concerned: there is no
    endpoint that edits an audit record, and a second ruling is refused."""
    run_id = held_run()
    hdr = auth_headers("reviewer", "r.patel")
    client.post(f"/api/runs/{run_id}/review", json={"decision": "ACCEPTED"}, headers=hdr)
    before = storage.get_run(run_id)

    client.post(f"/api/runs/{run_id}/review", json={"decision": "REJECTED"}, headers=hdr)
    after = storage.get_run(run_id)

    assert (after["reviewed_by"], after["human_decision"], after["final_decision"]) == \
           (before["reviewed_by"], before["human_decision"], before["final_decision"])


# --------------------------------------------------------------------------
# 5. rate limiting
# --------------------------------------------------------------------------

def test_requests_below_the_limit_are_allowed(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PROCESS_PER_MINUTE", 5)
    hdr = auth_headers("analyst", "u1")
    for i in range(4):
        assert upload(client, hdr).status_code == 200, f"request {i + 1} should pass"


def test_exceeding_the_limit_returns_429(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_PROCESS_PER_MINUTE", 3)
    hdr = auth_headers("analyst", "u2")
    for _ in range(3):
        assert upload(client, hdr).status_code == 200
    r = upload(client, hdr)
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_the_limit_is_per_user_not_global(client, monkeypatch):
    """One user exhausting their budget must not lock everyone else out."""
    monkeypatch.setattr(config, "RATE_LIMIT_PROCESS_PER_MINUTE", 2)
    monkeypatch.setattr(config, "RATE_LIMIT_IP_PER_MINUTE", 100)
    noisy = auth_headers("analyst", "noisy")
    for _ in range(2):
        upload(client, noisy)
    assert upload(client, noisy).status_code == 429
    assert upload(client, auth_headers("analyst", "quiet")).status_code == 200


def test_an_unauthenticated_flood_never_reaches_the_limiter(client, monkeypatch):
    """401 before 429: an anonymous caller must not be able to burn a real
    user's budget, nor learn anything from which error comes back."""
    monkeypatch.setattr(config, "RATE_LIMIT_PROCESS_PER_MINUTE", 2)
    for _ in range(6):
        assert upload(client).status_code == 401


def test_login_attempts_are_rate_limited(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_LOGIN_PER_MINUTE", 3)
    for _ in range(3):
        client.post("/api/auth/token", data={"username": "reviewer", "password": "no"})
    r = client.post("/api/auth/token", data={"username": "reviewer", "password": "no"})
    assert r.status_code == 429


def test_rate_limiting_can_be_disabled_for_a_demo(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "RATE_LIMIT_PROCESS_PER_MINUTE", 1)
    hdr = auth_headers("analyst", "u3")
    for _ in range(4):
        assert upload(client, hdr).status_code == 200


def test_reading_is_not_rate_limited(client, monkeypatch):
    """The limiter guards extraction quota, not ordinary reads."""
    monkeypatch.setattr(config, "RATE_LIMIT_PROCESS_PER_MINUTE", 1)
    hdr = auth_headers("viewer")
    for _ in range(30):
        assert client.get("/api/runs", headers=hdr).status_code == 200


# --------------------------------------------------------------------------
# 6. input and file validation
# --------------------------------------------------------------------------

def test_a_non_pdf_upload_is_rejected(client):
    r = upload(client, auth_headers("analyst"), data=b"#!/bin/sh\nrm -rf /\n",
               name="payload.sh", ctype="application/x-sh")
    assert r.status_code == 415


def test_a_file_renamed_to_pdf_is_still_rejected(client):
    """Validation is on content, not on the name or the declared type."""
    r = upload(client, auth_headers("analyst"), data=b"GIF89a not a pdf",
               name="invoice.pdf", ctype="application/pdf")
    assert r.status_code == 415


def test_an_empty_upload_is_rejected(client):
    assert upload(client, auth_headers("analyst"), data=b"").status_code == 400


def test_an_oversized_upload_is_rejected(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 64 * 1024)
    big = b"%PDF-1.4\n" + b"0" * (128 * 1024)
    assert upload(client, auth_headers("analyst"), data=big).status_code == 413


def test_a_hostile_filename_cannot_escape_or_inject(client):
    r = upload(client, auth_headers("analyst"), name="../../../../etc/passwd.pdf")
    assert r.status_code == 200
    stored = storage.list_runs()[0]["filename"]
    assert ".." not in stored and "/" not in stored and "\\" not in stored
    assert stored == "passwd.pdf"


@pytest.mark.parametrize("name", [
    "../data/purchase_orders.json",
    "..\\data\\app.db",
    "../../README.md",
])
def test_the_sample_endpoint_cannot_serve_files_outside_its_directory(client, name):
    r = client.get(f"/api/sample-invoices/{name}", headers=auth_headers("viewer"))
    assert r.status_code == 404


def test_a_malformed_review_body_is_a_400_not_a_500(client):
    run_id = held_run()
    hdr = auth_headers("reviewer")
    for body in ({}, {"decision": None}, {"decision": "MAYBE"}, {"decision": 42}):
        r = client.post(f"/api/runs/{run_id}/review", json=body, headers=hdr)
        assert r.status_code in (400, 422), f"{body} produced {r.status_code}"


# --------------------------------------------------------------------------
# 7. secrets
# --------------------------------------------------------------------------

SECRET_MARKERS = ["GROQ_API_KEY", "GEMINI_API_KEY", "AUTH_SECRET",
                  "password_hash", "pbkdf2_sha256", "gsk_", "AIza"]


def test_no_endpoint_response_contains_a_secret(client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_totally_secret_value")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSecretGeminiValue")
    monkeypatch.setenv("AUTH_SECRET", "super-secret-signing-key")
    hdr = auth_headers("admin")

    upload(client, hdr)
    bodies = [client.get(p, headers=hdr).text for p in
              ("/api/runs", "/api/reference", "/api/sample-invoices",
               "/api/runs/1", "/api/auth/me")]
    bodies.append(client.get("/api/health").text)

    for body in bodies:
        for marker in SECRET_MARKERS + ["gsk_totally_secret_value",
                                        "AIzaSecretGeminiValue",
                                        "super-secret-signing-key"]:
            assert marker not in body, f"response leaked {marker}"


def test_the_stored_audit_trail_contains_no_secret(client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_totally_secret_value")
    monkeypatch.setenv("AUTH_SECRET", "super-secret-signing-key")
    upload(client, auth_headers("analyst"))

    import json
    for run in storage.list_runs():
        blob = json.dumps(run)
        for marker in ["gsk_totally_secret_value", "super-secret-signing-key", "AIza"]:
            assert marker not in blob, f"audit record leaked {marker}"


def test_a_provider_failure_is_not_echoed_to_the_client(client, monkeypatch):
    """Provider errors can quote request content and key names back at you."""
    import extraction

    def explode(*a, **k):
        raise RuntimeError("groq said: invalid api key gsk_totally_secret_value")

    monkeypatch.setenv("GROQ_API_KEY", "gsk_totally_secret_value")
    monkeypatch.setattr(extraction, "groq_extract_text", explode)

    r = upload(client, auth_headers("analyst"))
    assert r.status_code == 200          # degrades to regex, as designed
    assert "gsk_totally_secret_value" not in r.text


def test_an_unhandled_error_leaks_nothing_to_the_client(client, monkeypatch):
    """A crash mid-pipeline cannot become a 500 -- the 200 and the SSE headers
    are already on the wire -- so what matters is that the client gets a clean
    event and no internal detail."""
    import extraction

    def explode(*a, **k):
        raise RuntimeError("internal detail: /home/secret/path.py line 42")

    monkeypatch.setattr(extraction, "extract_text", explode)
    r = upload(client, auth_headers("analyst"))
    body = r.text

    assert "Traceback" not in body
    assert "line 42" not in body and "/home/secret" not in body
    assert "Processing failed" in body


def test_an_unhandled_error_before_the_stream_is_a_clean_500(client, monkeypatch):
    """Where a 500 IS still possible, it carries no detail either."""
    import main as main_mod

    def explode(*a, **k):
        raise RuntimeError("internal detail: /home/secret/path.py line 42")

    monkeypatch.setattr(main_mod, "_validate_pdf", explode)

    # raise_server_exceptions=False so the client observes the response the app
    # actually returns, instead of TestClient re-raising the exception for
    # debugging. A real HTTP client always sees the response.
    from fastapi.testclient import TestClient
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = upload(c, auth_headers("analyst"))

    assert r.status_code == 500
    assert r.json() == {"error": "Internal server error", "detail": "Internal server error"}
    assert "line 42" not in r.text and "Traceback" not in r.text


def test_the_frontend_bundle_contains_no_secret():
    """Nothing server-side should ever have been copied into the client."""
    for name in ("app.js", "index.html"):
        src = open(os.path.join(ROOT, "frontend", name), encoding="utf-8").read()
        for marker in ["gsk_", "AIza", "AUTH_SECRET", "GROQ_API_KEY",
                       "GEMINI_API_KEY", "password_hash"]:
            assert marker not in src, f"frontend/{name} references {marker}"


def test_cors_is_not_wide_open_by_default():
    """CORS is not a security boundary, but a wildcard invites treating it as one."""
    assert config.CORS_ORIGINS == [] or "*" not in config.CORS_ORIGINS


# --------------------------------------------------------------------------
# 7a. a burst of ordinary reads must not fail, and must not end the session
#
# THE BUG THIS PINS. `ThreadedConnectionPool.getconn()` raises the instant
# every connection is checked out -- it does not queue -- and the ceiling was
# hard-coded at 10 while Starlette runs sync endpoints on a threadpool of 40.
# So a handful of simultaneous reads, which is all that pressing Refresh a few
# times amounts to, could ask for more connections than existed and the surplus
# came back 500.
#
# On its own that is a bad error. What made it a SIGN-OUT is that the browser's
# AuthProvider treated any failure of /api/auth/me as a dead session and threw
# the token away -- so a 500 on that one endpoint, caused by nothing but haste,
# logged the user out. Both halves are fixed; this is the server half, which is
# the one that can be tested here.
#
# Real threads against real Postgres, the same technique the ledger and review
# races already use -- a simulated pool would prove nothing about the pool.
# --------------------------------------------------------------------------

def test_a_burst_of_concurrent_reads_never_fails_and_never_401s(client, monkeypatch):
    """Twelve threads, four rounds, on the three endpoints a page load hits.

    THE POOL IS DELIBERATELY SQUEEZED TO TWO CONNECTIONS FIRST, and without
    that this test cannot fail: a dozen short reads rarely hold ten connections
    at once, so it passed just as happily against the refuse-instantly pool
    that caused the bug. A test that cannot go red is not a guard. At a ceiling
    of two, twenty-four concurrent reads MUST contend, so the only way through
    is for the surplus to wait its turn.

    Every response must be a 200. A 500 is the pool refusing to queue; a 401
    would be worse still, because that is the status the frontend acts on by
    ending the session -- and nothing about being busy makes a token invalid.
    """
    import threading

    original_pool = storage._POOL
    monkeypatch.setenv(config.DB_POOL_MAX_ENV, "2")
    storage._POOL = None                    # force a rebuild at the new ceiling
    try:
        headers = auth_headers("reviewer")
        paths = ["/api/auth/me", "/api/runs", "/api/reference"]
        n = 12
        results, lock, barrier = [], threading.Lock(), threading.Barrier(n)

        def worker(i):
            path = paths[i % len(paths)]
            barrier.wait()                  # everyone starts together
            for _ in range(4):
                r = client.get(path, headers=headers)
                with lock:
                    results.append((path, r.status_code))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == n * 4
        bad = [r for r in results if r[1] != 200]
        assert not bad, f"a burst of ordinary reads produced non-200s: {sorted(set(bad))}"
    finally:
        squeezed, storage._POOL = storage._POOL, original_pool
        if squeezed is not None and squeezed is not original_pool:
            try:
                squeezed.closeall()
            except Exception:
                pass


def test_the_pool_waits_for_a_connection_instead_of_refusing(db):
    """Hold every connection, then ask for one more.

    The request must BLOCK and then succeed once a connection comes back --
    not raise. Checked by releasing one from another thread after a delay the
    borrower could not have satisfied by luck.
    """
    import threading
    import time

    pool = storage._pool()
    held = [storage.get_conn() for _ in range(config.db_pool_max())]

    outcome = {}

    def borrow():
        started = time.monotonic()
        try:
            conn = storage.get_conn()
            outcome["waited"] = time.monotonic() - started
            conn.close()
            outcome["ok"] = True
        except Exception as exc:                      # pragma: no cover
            outcome["ok"] = False
            outcome["error"] = exc

    t = threading.Thread(target=borrow)
    t.start()
    time.sleep(0.3)                       # the borrower is now genuinely waiting
    held.pop().close()                    # ... and this is what lets it through
    t.join(timeout=10)

    assert outcome.get("ok") is True, f"getconn refused instead of waiting: {outcome}"
    assert outcome["waited"] >= 0.25, "it did not actually wait for the release"

    for conn in held:
        conn.close()
    assert pool is storage._pool(), "the pool must not have been rebuilt"


def test_the_pool_ceiling_is_configurable_and_refuses_a_nonsense_value(monkeypatch):
    """The right ceiling is a property of the database, not of this code -- a
    hosted Postgres shares one connection limit across every client. But a typo
    in the variable must not stop the process starting, so anything
    unparseable or absurd falls back to the default."""
    monkeypatch.setenv(config.DB_POOL_MAX_ENV, "32")
    assert config.db_pool_max() == 32
    for nonsense in ("0", "-1", "9999", "sixteen", ""):
        monkeypatch.setenv(config.DB_POOL_MAX_ENV, nonsense)
        assert config.db_pool_max() == 16, nonsense


# --------------------------------------------------------------------------
# 8. regression: the pipeline still works behind all of this
# --------------------------------------------------------------------------

def test_an_authorized_upload_still_produces_a_full_decision(client):
    r = upload(client, auth_headers("analyst"))
    assert r.status_code == 200

    import json
    final = None
    for line in r.text.splitlines():
        if line.startswith("data: "):
            evt = json.loads(line[6:])
            if evt.get("type") == "final":
                final = evt["result"]

    assert final is not None
    assert final["status"] in {"APPROVED", "NEEDS_REVIEW", "REJECTED"}
    assert final["audit"]["rules"], "the audit trail must survive the security layer"
    assert final["extracted"]["extraction_method"] == "regex"   # keys stripped
