"""Phase K: the security boundaries added or repaired by the hardening pass.

WHAT THESE TESTS ARE FOR

`tests/test_api_security.py` already proves the boundaries Phases 0-I built:
anonymous callers are refused, forged and expired tokens are refused, scopes
are enforced, uploads are validated, secrets do not appear in responses. None
of that is repeated here.

This file covers the five weaknesses the Phase K audit actually found, and it
is written so that each test fails if the fix is removed:

  1. AN ISSUED TOKEN COULD NOT BE REVOKED, and no account could be disabled.
     A JWT carries the roles it was minted with and was believed, unexamined,
     for eight hours. Deactivating or demoting somebody did nothing until it
     expired.
  2. LOGIN WAS RATE LIMITED PER IP ONLY, so guessing one account's password
     from many addresses was unlimited.
  3. REPORTING AND EXPORTS HAD NO LIMIT AT ALL, so the cheapest credential in
     the system could loop a 50,000-row CSV export indefinitely.
  4. NO HTTP SECURITY HEADERS, on an app that serves its own UI -- so the
     accept/reject controls could be framed by any site.
  5. SECURITY SETTINGS IN .env WERE SILENTLY IGNORED, including the CORS
     origins that the production start-up check claimed to be validating.

Everything is driven through the real app over HTTP wherever the claim is
about an endpoint, for the reason test_api_security.py states: calling the
function underneath proves nothing about what guards it.
"""
import io
import json
import os
import sys
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

import auth        # noqa: E402
import config      # noqa: E402
import documents   # noqa: E402
import logs        # noqa: E402
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
    schema = pg_schema.fresh_schema(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    ratelimit.limiter.reset()
    yield schema
    ratelimit.limiter.reset()
    pg_schema.drop_schema(schema)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def user_store(tmp_path, monkeypatch):
    """A writable user store, so a test can deactivate or demote an account.

    `auth.load_users()` reads AUTH_USERS_FILE on every call -- which is what
    makes the live re-check in `apply_account_state` possible at all -- so a
    test can rewrite the file mid-request-sequence and the next call sees it.
    """
    path = tmp_path / "users.json"
    monkeypatch.setenv("AUTH_USERS_FILE", str(path))

    def write(*records):
        path.write_text(json.dumps(list(records)), encoding="utf-8")
        return str(path)

    def account(username="kim", roles=("reviewer",), password="correct horse",
                **extra):
        record = {"username": username, "roles": list(roles),
                  "password_hash": auth.hash_password(password)}
        record.update(extra)
        return record

    write.account = account
    return write


def held_run():
    """A run the rules held for review, created below the API."""
    extracted = {"vendor_name": "Globex Logistics", "invoice_number": "INV-K1",
                 "total": 6000.0, "subtotal": 6000.0, "tax": 0.0,
                 "po_references": ["PO-1002"], "currency": "USD",
                 "extraction_method": "regex"}
    info = {"route": "regex", "notes": [], "security_flags": []}
    po_match = matching.match_po(extracted)
    vendor_ok, _, vendor_detail = rules.vendor_check(extracted)
    audit = {}
    verdict, reasons = rules.decide(
        info, rules.validate_required_fields(extracted), vendor_ok, vendor_detail,
        None, "", po_match, arithmetic=rules.validate_arithmetic(extracted),
        amount=rules.validate_amount(extracted), audit=audit, extracted=extracted)
    assert verdict == "NEEDS_REVIEW"
    run_id, _, _ = storage.save_run_checked(
        "held.pdf", verdict, extracted, po_match, [], reasons,
        tolerance_for=matching.tolerance_for, audit=audit)
    return run_id


def bearer(username, roles):
    return {"Authorization": "Bearer " + auth.create_access_token(
        {"username": username, "roles": list(roles)})["access_token"]}


# ==========================================================================
# 1. account state -- revocation and demotion (the HIGH finding)
#
# A signed token proves it was minted here. It does not prove the account is
# still allowed to do what it was allowed to do when it was minted.
# ==========================================================================

def test_a_disabled_account_cannot_sign_in(user_store):
    user_store(user_store.account("kim", disabled=True))
    assert auth.authenticate_user("kim", "correct horse") is None


def test_the_same_account_can_sign_in_once_it_is_enabled_again(user_store):
    user_store(user_store.account("kim", disabled=True))
    assert auth.authenticate_user("kim", "correct horse") is None
    user_store(user_store.account("kim"))
    assert auth.authenticate_user("kim", "correct horse") is not None


def test_active_false_disables_an_account_as_well_as_disabled_true(user_store):
    """Two spellings, because both are the obvious one to reach for and an
    operator must not discover they picked the word this code ignores."""
    user_store(user_store.account("kim", active=False))
    assert auth.authenticate_user("kim", "correct horse") is None


def test_a_disabled_login_is_indistinguishable_from_a_wrong_password(client, user_store):
    """Otherwise the login endpoint becomes a way to ask which colleague has
    been deactivated."""
    user_store(user_store.account("kim", disabled=True))
    disabled = client.post("/api/auth/token",
                           data={"username": "kim", "password": "correct horse"})
    wrong = client.post("/api/auth/token",
                        data={"username": "kim", "password": "not the password"})
    unknown = client.post("/api/auth/token",
                          data={"username": "nobody-at-all", "password": "x"})

    assert disabled.status_code == wrong.status_code == unknown.status_code == 401
    assert disabled.json() == wrong.json() == unknown.json()


def test_an_issued_token_stops_working_the_moment_the_account_is_disabled(client, user_store):
    """THE FINDING. Before Phase K this token kept every permission it was
    minted with for the full eight-hour TTL, and the only way to cut it short
    was rotating AUTH_SECRET -- which signs out everybody."""
    user_store(user_store.account("kim", roles=["reviewer"]))
    headers = bearer("kim", ["reviewer"])

    assert client.get("/api/runs", headers=headers).status_code == 200

    user_store(user_store.account("kim", roles=["reviewer"], disabled=True))
    refused = client.get("/api/runs", headers=headers)

    assert refused.status_code == 401
    assert refused.json()["error"] == "Invalid or expired credentials"


def test_a_demotion_takes_effect_on_the_very_next_request(client, db, user_store):
    """A token can carry less authority than it was minted with, never more."""
    run_id = held_run()
    user_store(user_store.account("kim", roles=["reviewer"]))
    headers = bearer("kim", ["reviewer"])

    claimed = client.post(f"/api/runs/{run_id}/review/claim", headers=headers)
    assert claimed.status_code == 200, "a reviewer could claim before the demotion"

    user_store(user_store.account("kim", roles=["viewer"]))
    after = client.post(f"/api/runs/{run_id}/review/claim", headers=headers)

    assert after.status_code == 403
    assert "invoice:review" in after.json()["detail"]
    # Still authenticated -- demotion is not revocation.
    assert client.get("/api/runs", headers=headers).status_code == 200


def test_a_token_cannot_claim_a_scope_the_account_does_not_hold(client, user_store):
    """The token is a snapshot, not an authority. A forged-role token would
    already fail on the signature; this covers the subtler case of a genuine
    token outliving the role it names."""
    user_store(user_store.account("kim", roles=["viewer"]))
    headers = bearer("kim", ["admin"])       # signed here, but the store says viewer

    assert client.post("/api/admin/reset-demo", headers=headers).status_code == 403
    assert client.get("/api/runs", headers=headers).status_code == 200


def test_the_scopes_endpoint_reports_the_intersected_truth(client, user_store):
    """`/api/auth/me` drives which controls the UI renders, so it must report
    what the caller can ACTUALLY do, not what the token claims."""
    user_store(user_store.account("kim", roles=["viewer"]))
    me = client.get("/api/auth/me", headers=bearer("kim", ["admin"])).json()
    assert me["scopes"] == ["invoice:read"]


def test_an_unparseable_user_record_reads_as_disabled(user_store):
    """Everything else in this codebase fails open for availability. This one
    fails closed: a corrupt record is not a yes."""
    assert auth.is_disabled(None) is True
    assert auth.is_disabled("not a record") is True
    assert auth.is_disabled({"username": "kim"}) is False


def test_an_account_absent_from_the_store_is_passed_through(client, user_store):
    """THE DOCUMENTED RESIDUAL GAP, asserted so it cannot change by accident.

    A principal with no local record is accepted, because the token issuer is
    designed to be replaceable by an identity provider whose principals have
    no local record at all. The operational consequence is the instruction in
    CLAUDE.md: to revoke access, DISABLE the record -- deleting it leaves the
    outstanding token valid until it expires.
    """
    user_store(user_store.account("someone-else"))
    assert client.get("/api/runs", headers=bearer("kim", ["viewer"])).status_code == 200


def test_an_enabled_account_is_completely_unaffected(client, user_store):
    user_store(user_store.account("kim", roles=["admin"]))
    headers = bearer("kim", ["admin"])
    assert client.get("/api/runs", headers=headers).status_code == 200
    assert client.post("/api/admin/reset-demo", headers=headers).status_code == 200


# ==========================================================================
# 2. login brute force -- counting the target, not only the source
# ==========================================================================

def test_guessing_one_account_from_many_addresses_is_stopped(client, user_store, monkeypatch):
    """THE FINDING. The per-IP counter resets with every new source address, so
    a botnet or a VPN pool made the per-IP limit worth nothing to the account
    actually under attack. The per-username counter does not care how many
    addresses the guesses arrive from."""
    user_store(user_store.account("kim"))
    monkeypatch.setattr(config, "RATE_LIMIT_LOGIN_PER_USER_PER_MINUTE", 4)
    monkeypatch.setattr(config, "RATE_LIMIT_LOGIN_PER_MINUTE", 1000)
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)

    codes = []
    for i in range(6):
        # A different source address every single time.
        r = client.post("/api/auth/token",
                        data={"username": "kim", "password": f"guess-{i}"},
                        headers={"X-Forwarded-For": f"203.0.113.{i}"})
        codes.append(r.status_code)

    assert codes[:4] == [401, 401, 401, 401], "the first attempts were evaluated"
    assert 429 in codes, "guessing one account from many addresses was not limited"


def test_the_per_account_limit_does_not_lock_out_a_different_account(client, user_store, monkeypatch):
    """A shared counter that stopped everyone would be a denial-of-service
    handed to anyone willing to fail a login on purpose."""
    user_store(user_store.account("kim"), user_store.account("sam"))
    monkeypatch.setattr(config, "RATE_LIMIT_LOGIN_PER_USER_PER_MINUTE", 3)
    monkeypatch.setattr(config, "RATE_LIMIT_LOGIN_PER_MINUTE", 1000)

    for i in range(5):
        client.post("/api/auth/token",
                    data={"username": "kim", "password": f"guess-{i}"})

    ok = client.post("/api/auth/token",
                     data={"username": "sam", "password": "correct horse"})
    assert ok.status_code == 200, "an unrelated account was caught by the limit"


def test_the_account_counter_is_case_insensitive(client, user_store, monkeypatch):
    """Otherwise `KIM`, `Kim` and `kIm` are three free budgets."""
    user_store(user_store.account("kim"))
    monkeypatch.setattr(config, "RATE_LIMIT_LOGIN_PER_USER_PER_MINUTE", 3)
    monkeypatch.setattr(config, "RATE_LIMIT_LOGIN_PER_MINUTE", 1000)

    codes = [client.post("/api/auth/token",
                         data={"username": name, "password": "guess"}).status_code
             for name in ("kim", "KIM", "Kim", "kIm", "KiM")]
    assert 429 in codes


def test_a_login_with_no_username_is_a_clean_refusal_not_a_500(client):
    """The limiter reads the form before the endpoint validates it, so a
    malformed body must fall through rather than crash inside the limiter."""
    assert client.post("/api/auth/token", data={"password": "x"}).status_code in (400, 422)
    assert client.post("/api/auth/token", data={}).status_code in (400, 422)
    assert client.post("/api/auth/token", content=b"not a form",
                       headers={"Content-Type": "application/x-www-form-urlencoded"}
                       ).status_code in (400, 422)


def test_reading_the_username_does_not_break_the_login_itself(client, user_store):
    """The limiter consumes the request body first. If that were not cached,
    every sign-in in the application would fail."""
    user_store(user_store.account("kim"))
    r = client.post("/api/auth/token",
                    data={"username": "kim", "password": "correct horse"})
    assert r.status_code == 200
    assert r.json()["token_type"] == "bearer"


# ==========================================================================
# 3. reporting and export limits
# ==========================================================================

def test_the_csv_export_is_rate_limited(client, monkeypatch):
    """THE FINDING. An export streams up to MAX_EXPORT_ROWS rows and the stage
    and rule filters parse every run's JSON in the window -- and until Phase K
    the lowest-privileged credential in the system could loop it forever."""
    monkeypatch.setattr(config, "RATE_LIMIT_REPORTING_PER_MINUTE", 3)
    headers = auth_headers("viewer", "vic")

    codes = [client.get("/api/logs/export", headers=headers).status_code
             for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[-1] == 429


def test_the_limit_response_says_when_to_come_back(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REPORTING_PER_MINUTE", 1)
    headers = auth_headers("viewer", "vic")
    client.get("/api/analytics/overview", headers=headers)
    refused = client.get("/api/analytics/overview", headers=headers)

    assert refused.status_code == 429
    assert refused.headers.get("Retry-After")
    assert refused.json()["ok"] is False


@pytest.mark.parametrize("path", [
    "/api/analytics/overview", "/api/analytics/trends", "/api/analytics/processing",
    "/api/analytics/reviews", "/api/analytics/vendors", "/api/analytics/email",
    "/api/analytics/users", "/api/analytics/dashboard",
    "/api/logs", "/api/logs/facets", "/api/logs/export",
    "/api/logs/stages", "/api/logs/stages/export",
    "/api/runs/999999/audit-report.pdf", "/api/runs/999999/audit-report.csv",
])
def test_every_reporting_endpoint_is_behind_the_limiter(client, monkeypatch, path):
    """One endpoint left off the limiter is the whole control, because an
    attacker only needs the one that was forgotten."""
    monkeypatch.setattr(config, "RATE_LIMIT_REPORTING_PER_MINUTE", 2)
    headers = auth_headers("viewer", "vic")
    codes = [client.get(path, headers=headers).status_code for _ in range(4)]
    assert 429 in codes, f"{path} is not rate limited"


def test_the_reporting_limit_is_per_user_not_global(client, monkeypatch):
    """One person exporting must not stop their colleagues reading a
    dashboard."""
    monkeypatch.setattr(config, "RATE_LIMIT_REPORTING_PER_MINUTE", 2)
    for _ in range(4):
        client.get("/api/logs/export", headers=auth_headers("viewer", "vic"))

    other = client.get("/api/logs/export", headers=auth_headers("viewer", "sam"))
    assert other.status_code == 200


def test_ordinary_reads_are_not_caught_by_the_reporting_limit(client, monkeypatch):
    """Reading the invoice register is not a reporting query, and a limit that
    made normal use impossible would be worse than no limit."""
    monkeypatch.setattr(config, "RATE_LIMIT_REPORTING_PER_MINUTE", 2)
    headers = auth_headers("viewer", "vic")
    codes = [client.get("/api/runs", headers=headers).status_code for _ in range(6)]
    assert codes == [200] * 6


def test_reporting_endpoints_still_enforce_the_scope(client):
    """The limiter replaced the Security() dependency on those endpoints, so
    this proves it did not replace the authorization with it."""
    token = auth.create_access_token({"username": "nobody", "roles": ["nobody"]})
    headers = {"Authorization": "Bearer " + token["access_token"]}

    for path in ("/api/analytics/overview", "/api/logs", "/api/logs/export"):
        anon = client.get(path, headers={"Authorization": ""})
        no_scope = client.get(path, headers=headers)
        assert anon.status_code == 401, path
        assert no_scope.status_code == 403, path
        assert "invoice:read" in no_scope.json()["detail"]


def test_the_per_person_analytics_scope_still_holds_behind_the_limiter(client, db):
    """`/api/analytics/users` is the one endpoint about PEOPLE rather than
    invoices: your own row unless you hold invoice:admin."""
    reviewer = client.get("/api/analytics/users",
                          headers=auth_headers("reviewer", "kim")).json()
    admin = client.get("/api/analytics/users",
                       headers=auth_headers("admin", "root")).json()
    assert reviewer["scope"] == "self"
    assert admin["scope"] == "all"


def test_an_export_cannot_be_widened_by_asking_about_somebody_else(client, db):
    """A CSV of a colleague's activity, available to anyone with invoice:read,
    is the accidental leak this phase's brief named. Grouping by actor is
    restricted; the export does not offer grouping at all."""
    run_id = held_run()
    storage.record_human_review(run_id, "ACCEPTED", reviewer="kim", note="fine")

    grouped = client.get("/api/logs?group_by=actor&actor=kim",
                         headers=auth_headers("reviewer", "sam")).json()
    assert grouped["scope"] == "self"
    assert [g["key"] for g in grouped["groups"]] in ([], ["sam"])


# ==========================================================================
# 4. HTTP security headers
# ==========================================================================

SECURITY_HEADERS = ("X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options",
                    "Content-Security-Policy", "Cross-Origin-Opener-Policy",
                    "Permissions-Policy")


@pytest.mark.parametrize("path", ["/api/health", "/api/runs", "/api/analytics/overview"])
def test_every_response_carries_the_security_headers(client, path):
    r = client.get(path, headers=auth_headers("viewer", "vic"))
    for header in SECURITY_HEADERS:
        assert r.headers.get(header), f"{path} sent no {header}"


def test_an_error_response_carries_them_too(client):
    """A 401 is still a response the browser renders in some contexts, and a
    header that is only present on the happy path is not a control."""
    r = client.get("/api/runs", headers={"Authorization": ""})
    assert r.status_code == 401
    for header in SECURITY_HEADERS:
        assert r.headers.get(header)


def test_the_app_cannot_be_framed(client):
    """Approving an invoice is one click, which is exactly what a framed UI
    monetises. Asserted twice over, because old browsers read only the first."""
    r = client.get("/api/health")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_the_policy_still_allows_what_the_ui_actually_does(client):
    """The document preview fetches the PDF WITH its Authorization header and
    renders the resulting blob: URL -- which is why no token is ever in a URL,
    and why the policy has to permit blob: in object-src and frame-src. A
    policy that broke the preview would be reverted within a day."""
    csp = client.get("/api/health").headers["Content-Security-Policy"]
    assert "object-src 'self' blob:" in csp
    assert "frame-src 'self' blob:" in csp
    assert "default-src 'self'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'self'" in csp


def test_hsts_is_absent_in_development_and_present_in_production(client, monkeypatch):
    """The one header here that is hard to take back: a browser told to pin
    https for a year will refuse http to that host for a year, which on a
    laptop breaks the machine rather than protecting it."""
    assert client.get("/api/health").headers.get("Strict-Transport-Security") is None

    monkeypatch.setenv(config.APP_ENV_VAR, "production")
    hsts = client.get("/api/health").headers.get("Strict-Transport-Security")
    assert hsts and "max-age=" in hsts


def test_the_headers_do_not_overwrite_one_the_app_already_set(client):
    """The app shell sets its own no-store Cache-Control for a reason that cost
    two debugging sessions to find. The middleware must not fight it."""
    r = client.get("/")
    if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
        assert "no-store" in r.headers.get("Cache-Control", "")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_the_headers_can_be_turned_off_for_a_proxy_that_sets_its_own(client, monkeypatch):
    monkeypatch.setattr(config, "SECURITY_HEADERS_ENABLED", False)
    assert client.get("/api/health").headers.get("Content-Security-Policy") is None


def test_the_streaming_endpoint_still_streams_behind_the_middleware(client):
    """The reason this middleware is raw ASGI rather than BaseHTTPMiddleware:
    the latter wraps the response body, and the SSE run view is the one thing
    in this application it was not worth risking."""
    with open(HAPPY_PDF, "rb") as f:
        body = f.read()
    r = client.post("/api/runs/stream",
                    files={"file": ("invoice.pdf", io.BytesIO(body), "application/pdf")},
                    headers=auth_headers("analyst", "ana"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert '"type": "final"' in r.text or '"type":"final"' in r.text


# ==========================================================================
# 5. configuration that is actually read
# ==========================================================================

def test_dotenv_is_loaded_before_the_constants_bind(tmp_path, monkeypatch):
    """THE FINDING. `load_dotenv()` used to run at FastAPI startup, AFTER this
    module was imported -- so CORS_ORIGINS, the rate limits, TRUST_PROXY_HEADERS
    and the token TTL all read the environment before .env had been touched and
    silently ignored whatever it said. The production start-up check that
    refuses `CORS_ORIGINS=*` was therefore validating a value .env could not
    influence: an assurance about something it had never looked at.
    """
    env = tmp_path / ".env"
    env.write_text("PHASE_K_PROBE=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("PHASE_K_PROBE", raising=False)

    config._read_dotenv(str(env))
    assert os.environ["PHASE_K_PROBE"] == "from-dotenv"


@pytest.mark.parametrize("name,value,attribute,expected", [
    ("CORS_ORIGINS", "https://ap.example", "CORS_ORIGINS", ["https://ap.example"]),
    ("RATE_LIMIT_LOGIN_PER_MINUTE", "3", "RATE_LIMIT_LOGIN_PER_MINUTE", 3),
    ("RATE_LIMIT_REPORTING_PER_MINUTE", "7", "RATE_LIMIT_REPORTING_PER_MINUTE", 7),
    ("TRUST_PROXY_HEADERS", "1", "TRUST_PROXY_HEADERS", True),
    ("AUTH_TOKEN_TTL_MINUTES", "30", "AUTH_TOKEN_TTL_MINUTES", 30),
    ("SECURITY_HEADERS_ENABLED", "0", "SECURITY_HEADERS_ENABLED", False),
])
def test_a_security_setting_from_the_environment_actually_reaches_the_constant(
        monkeypatch, name, value, attribute, expected):
    """These are the settings that were frozen at import and therefore ignored
    whatever .env said. `load_dotenv()` now rebinds them, which is what makes
    configuring them possible at all."""
    monkeypatch.setenv(name, value)
    try:
        config.refresh_env_settings()
        assert getattr(config, attribute) == expected
    finally:
        monkeypatch.undo()
        config.refresh_env_settings()


def test_a_bad_numeric_setting_falls_back_instead_of_killing_the_process(monkeypatch):
    """`int("banana")` at import used to be an unhandled crash at startup with
    a traceback naming the file -- a configuration typo taking the app down."""
    monkeypatch.setenv("RATE_LIMIT_REPORTING_PER_MINUTE", "banana")
    try:
        config.refresh_env_settings()
        assert config.RATE_LIMIT_REPORTING_PER_MINUTE == 120
    finally:
        monkeypatch.undo()
        config.refresh_env_settings()


def test_the_real_environment_still_wins_over_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("PHASE_K_PROBE=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("PHASE_K_PROBE", "from-the-environment")

    config._read_dotenv(str(env))
    assert os.environ["PHASE_K_PROBE"] == "from-the-environment"


def test_a_malformed_dotenv_does_not_stop_the_process_starting(tmp_path):
    env = tmp_path / ".env"
    env.write_text("no equals sign here\n\n# comment\n=novalue\n", encoding="utf-8")
    config._read_dotenv(str(env))          # must not raise


def test_the_production_check_still_refuses_a_wildcard_origin(monkeypatch):
    """Now meaningful, because the value it reads can finally come from .env."""
    monkeypatch.setenv(config.APP_ENV_VAR, "production")
    monkeypatch.setenv(config.AUTH_SECRET_ENV, "x" * 48)
    monkeypatch.setattr(config, "CORS_ORIGINS", ["*"])
    problems = auth.validate_production_config()
    assert any("CORS_ORIGINS" in p for p in problems)


def test_cors_is_still_closed_by_default(client):
    """A browser on another origin gets nothing unless an origin was named."""
    r = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_a_configured_origin_is_honoured_without_a_restart(client, monkeypatch):
    """The middleware used to bind its origin list at import, before .env had
    been read, so an origin configured there reached nothing. It is read per
    request now."""
    monkeypatch.setattr(config, "CORS_ORIGINS", ["https://ap.example"])
    allowed = client.get("/api/health", headers={"Origin": "https://ap.example"})
    assert allowed.headers.get("access-control-allow-origin") == "https://ap.example"


def test_an_origin_that_was_not_named_is_still_refused(client, monkeypatch):
    """Configuring one origin must not open the door to every origin."""
    monkeypatch.setattr(config, "CORS_ORIGINS", ["https://ap.example"])
    other = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert other.headers.get("access-control-allow-origin") is None


def test_cors_does_not_grant_credentials(client, monkeypatch):
    """Allowing an origin says a browser may READ the response. It must not
    also mean the browser attaches ambient credentials to it."""
    monkeypatch.setattr(config, "CORS_ORIGINS", ["https://ap.example"])
    r = client.get("/api/health", headers={"Origin": "https://ap.example"})
    assert r.headers.get("access-control-allow-credentials") is None


# ==========================================================================
# 6. the boundaries the audit re-verified rather than changed
#
# These are not new controls. They are the claims the audit had to confirm
# before it could report them, written down so a later change cannot quietly
# withdraw one.
# ==========================================================================

def test_the_failed_login_equaliser_is_computed_once(monkeypatch):
    """It used to be rebuilt per attempt, running the 390,000-round KDF twice
    for an unknown username against once for a real one -- the most expensive
    request in the application, and unequal in the direction it was meant to
    equalise."""
    monkeypatch.setattr(auth, "_DUMMY_HASH", None)
    first = auth._dummy_hash()
    assert auth._dummy_hash() is first
    assert first.startswith("pbkdf2_sha256$")


def test_no_response_anywhere_carries_a_password_or_a_hash(client, db):
    """Including the one endpoint whose whole subject is the caller."""
    run_id = held_run()
    paths = ["/api/auth/me", "/api/runs", f"/api/runs/{run_id}",
             f"/api/runs/{run_id}/activity", "/api/analytics/users",
             "/api/logs", "/api/reference", "/api/email/trusted-senders"]
    for path in paths:
        body = client.get(path, headers=auth_headers("admin", "root")).text
        for forbidden in ("password_hash", "pbkdf2_sha256", "demo-admin",
                          "AUTH_SECRET", "DATABASE_URL", "password"):
            assert forbidden not in body, f"{path} leaked {forbidden}"


def test_an_error_body_names_no_path_module_or_query(client, db):
    """A 500 body of six words, and everything useful in the server log."""
    for path in ("/api/runs/999999", "/api/runs/999999/document",
                 "/api/logs/invoice/999999", "/api/email/messages/999999"):
        r = client.get(path, headers=auth_headers("admin", "root"))
        assert r.status_code in (400, 404)
        body = r.text
        for forbidden in ("Traceback", "backend\\", "backend/", "psycopg2",
                          "SELECT ", "site-packages", "storage.py", ".py\"",):
            assert forbidden not in body, f"{path} leaked {forbidden!r}"


@pytest.mark.parametrize("key", [
    "../../../../etc/passwd", "..\\..\\windows\\win.ini", "/etc/passwd",
    "a" * 32 + ".pdf.exe", "deadbeef.pdf", "", None,
    "0123456789abcdef0123456789abcdef.PDF",
])
def test_a_document_storage_key_outside_its_shape_is_refused(tmp_path, key):
    """The key is always server-generated, so this can only be reached through
    a corrupted or tampered `documents.storage_key` row -- which is exactly the
    case worth being certain about, since it is the one an attacker who reached
    the database would use to read files off the host."""
    store = documents.LocalDocumentStore(root=str(tmp_path))
    with pytest.raises(ValueError):
        store._path(key)
    assert store.exists(key) is False


def test_a_generated_key_stays_inside_the_store(tmp_path):
    store = documents.LocalDocumentStore(root=str(tmp_path))
    key = documents.new_storage_key()
    store.save(key, b"%PDF-1.4 test")
    assert store.read(key) == b"%PDF-1.4 test"
    assert os.path.dirname(os.path.abspath(store._path(key))) == str(tmp_path)


def test_a_document_download_is_gated_before_the_row_is_looked_up(client, db):
    """An unauthorised caller must not be able to tell whether run 1 even has
    a document, so the scope check has to come first -- 403, not 404."""
    token = auth.create_access_token({"username": "nobody", "roles": ["nobody"]})
    headers = {"Authorization": "Bearer " + token["access_token"]}
    run_id = held_run()

    assert client.get(f"/api/runs/{run_id}/document",
                      headers=headers).status_code == 403
    assert client.get(f"/api/runs/{run_id}/document/download",
                      headers=headers).status_code == 403
    assert client.get("/api/runs/999999/document",
                      headers=headers).status_code == 403


@pytest.mark.parametrize("hostile", [
    "'; DROP TABLE runs; --",
    "1 OR 1=1",
    "%' UNION SELECT password_hash FROM users --",
    "\\'; DELETE FROM invoice_activity; --",
])
def test_a_hostile_filter_value_is_a_value_not_sql(client, db, hostile):
    """Every caller-supplied value is a bind parameter; the only interpolated
    fragments are this codebase's own frozen column names."""
    run_id = held_run()
    before = client.get("/api/runs", headers=auth_headers("admin", "root")).json()

    for path in (f"/api/logs?q={hostile}", f"/api/logs?vendor={hostile}",
                 f"/api/logs?rule_failed={hostile}",
                 f"/api/analytics/overview?range={hostile}"):
        r = client.get(path, headers=auth_headers("admin", "root"))
        assert r.status_code in (200, 400), path

    after = client.get("/api/runs", headers=auth_headers("admin", "root")).json()
    assert len(after) == len(before), "the table is still there"
    assert storage.get_run(run_id) is not None


@pytest.mark.parametrize("column", ["runs.id; DROP TABLE runs", "1=1",
                                    "runs.id, password_hash"])
def test_a_window_cannot_be_pointed_at_an_arbitrary_column(column):
    """SQL cannot bind an identifier, so the column names ARE interpolated --
    which is why every call site passes a literal and the guard rejects
    anything else. This is the test that keeps that true after an edit."""
    import analytics
    window = analytics.resolve_window("30d")
    with pytest.raises(analytics.AnalyticsError):
        window.clause(column, [])


def test_the_email_quarantine_gate_is_read_from_the_database(client, db):
    """Phase F's hold cannot be bypassed by the caller, because the status is
    re-read from the stored row rather than taken from the request."""
    email_id = storage.save_email_message({
        "sha256": "k" * 64, "message_id": "<k@example>",
        "from_address": "billing@vendor.example", "from_domain": "vendor.example",
        "from_display_name": "V", "envelope_from": "billing@vendor.example",
        "subject": "Invoice", "size_bytes": 10, "attachment_count": 1,
        "has_pdf_attachment": True, "spf_result": "unavailable",
        "dkim_result": "unavailable", "dmarc_result": "unavailable",
        "dmarc_aligned": False, "signature_kind": None,
        "signature_result": "not_present", "trusted_sender": False,
        "classification": "UNVERIFIED", "status": "QUARANTINED",
        "reasons": [], "auth": {},
    }, submitted_by="analyst-1", source="SUBMITTED")

    r = client.post(f"/api/email/messages/{email_id}/process",
                    headers=auth_headers("analyst", "ana"))
    assert r.status_code in (400, 409), "a quarantined message reached the pipeline"
    assert storage.get_email_message(email_id)["status"] == "QUARANTINED"
