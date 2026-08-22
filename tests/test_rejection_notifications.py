"""Rejection notification emails: compose, preview, send, audit.

WHAT THIS FILE PROVES

1. A rejection notice is built from the SAME vendor-safe reasons the supplier
   portal already shows (portal.client_state), never a second vocabulary.
2. Nothing is sent until an explicit, separate confirm step.
3. Both a successful and a failed send are recorded in invoice_activity, and
   neither leaves a token or secret anywhere near that record.
4. A second send is refused unless the caller explicitly forces a resend.
5. A run with no known vendor email has no default recipient and cannot be
   sent to a made-up one.
6. Sending needs the `gmail.send` scope on the LIVE connection, not merely a
   connected mailbox -- and a mailbox connected before this feature existed
   (no send scope) is refused with a clear reason, not a crash.

The provider is mocked at `oauth_google.api_post_json`, the one new function
this feature adds that opens a socket -- the same "mock only the function
that talks to Google" discipline test_gmail_oauth.py already established for
`_post_form`/`api_get`.
"""
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

import audit_export      # noqa: E402
import config             # noqa: E402
import email_outbound     # noqa: E402
import main               # noqa: E402
import matching           # noqa: E402
import notifications      # noqa: E402
import oauth_google       # noqa: E402
import portal             # noqa: E402
import ratelimit          # noqa: E402
import rules              # noqa: E402
import storage            # noqa: E402
import pg_schema          # noqa: E402
from conftest import auth_headers          # noqa: E402
from test_human_review import PO, VENDOR, held_invoice, submit    # noqa: E402

VENDOR_EMAIL_DOMAIN = "acme-office.example"


@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    ratelimit.limiter.reset()
    yield schema
    pg_schema.drop_schema(schema)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


def rejected_invoice():
    """A run held by the rules, then rejected by a human reviewer -- REJECTED
    with a real audit["rules_failed"] (over the PO balance), same as any
    invoice a reviewer would actually be looking at when they send a notice."""
    run_id = held_invoice()
    result = storage.record_human_review(run_id, "REJECTED", reviewer="a.singh")
    assert result["ok"] is True
    return run_id


def rejected_invoice_two_reasons():
    """REJECTED automatically, with TWO rules failing at once: the vendor is
    not on the approved list AND the invoice arithmetic does not add up."""
    extracted = {
        "vendor_name": "Totally Unknown Vendor Ltd", "invoice_number": "INV-BAD-1",
        "total": 500.0, "subtotal": 100.0, "tax": 1.0,   # 100 + 1 != 500
        "po_references": [], "currency": "USD",
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
    assert status == "REJECTED"
    assert len(audit["rules_failed"]) >= 2
    run_id, final_status, _ = storage.save_run_checked(
        "INV-BAD-1.pdf", status, extracted, po_match, [], reasons,
        tolerance_for=matching.tolerance_for, audit=audit)
    return run_id


def approved_invoice():
    run_id, status = submit(1000.00, "INV-OK")
    assert status == "APPROVED"
    return run_id


def link_email_sender(run_id: int, address: str = f"billing@{VENDOR_EMAIL_DOMAIN}") -> int:
    """Attach a real email_messages row to a run, the way Phase G's ingestion
    does, so `notifications.resolve_default_recipient` has something to find."""
    record = {
        "sha256": f"sha-{run_id}", "message_id": f"<m{run_id}@x>",
        "from_address": address, "from_domain": address.split("@", 1)[1],
        "subject": "Invoice", "classification": "VERIFIED", "status": "ADMITTED",
        "reasons": ["aligned"],
    }
    email_id = storage.save_email_message(record, submitted_by="poller", source="EMAIL")
    result = storage.link_email_to_run(email_id, run_id)
    assert result["ok"] is True
    return email_id


def gmail_connected(scopes=(config.GMAIL_SCOPE_READONLY, config.GMAIL_SCOPE_SEND)):
    """A live, non-expired Gmail connection -- so `oauth_google.gmail_access_token()`
    returns a token straight from storage with no refresh call needed."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    storage.save_oauth_connection(
        "gmail", "ap@buyer-corp.example", " ".join(scopes),
        refresh_token_encrypted=oauth_google.encrypt_token("refresh-token-value"),
        access_token_encrypted=oauth_google.encrypt_token("access-token-value"),
        access_token_expires_at=future, connected_by="admin")


@pytest.fixture
def auth_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "a-stable-test-signing-secret-value-32b")


# ==========================================================================
# 1-2. Preview: reasons come from the real rejection, and match the portal's
# ==========================================================================
def test_preview_uses_the_real_rejection_reasons(db):
    run_id = rejected_invoice()
    run = storage.get_run(run_id)
    draft = notifications.compose_rejection_email(run)
    expected_state, _headline, expected_lines = portal.client_state(
        run["status"], run["audit"])
    assert draft["reasons"] == expected_lines
    assert draft["reasons"], "an over-budget rejection must explain itself"
    assert f"Invoice Rejected" in draft["subject"]
    assert draft["invoice_number"] in draft["subject"]


def test_preview_over_http_does_not_send_anything(db, client):
    run_id = rejected_invoice()
    response = client.get(f"/api/runs/{run_id}/rejection-email",
                          headers=auth_headers("viewer"))
    assert response.status_code == 200
    body = response.json()
    assert body["already_sent"] is False
    assert body["history"] == []
    assert notifications.last_successful_send(run_id) is None


def test_multiple_rejection_reasons_all_appear(db):
    run_id = rejected_invoice_two_reasons()
    run = storage.get_run(run_id)
    draft = notifications.compose_rejection_email(run)
    assert len(draft["reasons"]) >= 2
    # Every reason line lands in the composed body as its own bullet.
    for reason in draft["reasons"]:
        assert f"- {reason}" in draft["body"]


def test_a_preview_is_refused_for_a_non_rejected_run(db, client):
    run_id = approved_invoice()
    response = client.get(f"/api/runs/{run_id}/rejection-email",
                          headers=auth_headers("viewer"))
    assert response.status_code == 409


# ==========================================================================
# 3-4. Confirmation required; a successful send is audited
# ==========================================================================
def test_sending_requires_a_separate_confirmed_call(db, client, auth_secret, monkeypatch):
    run_id = rejected_invoice()
    link_email_sender(run_id)
    gmail_connected()
    monkeypatch.setattr(oauth_google, "api_post_json",
                        lambda url, token, payload: {"id": "gmail-msg-1"})

    draft = client.get(f"/api/runs/{run_id}/rejection-email",
                       headers=auth_headers("reviewer")).json()["draft"]
    assert notifications.last_successful_send(run_id) is None    # nothing sent yet

    response = client.post(f"/api/runs/{run_id}/rejection-email/send",
                           headers=auth_headers("reviewer"),
                           json={"recipient": draft["recipient"], "subject": draft["subject"],
                                "body": draft["body"]})
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["message_id"] == "gmail-msg-1"

    sent = notifications.last_successful_send(run_id)
    assert sent is not None
    assert sent["actor"] == "test-reviewer"     # the auth_headers() default username
    assert sent["metadata"]["recipient"] == draft["recipient"]
    assert sent["metadata"]["message_id"] == "gmail-msg-1"


def test_only_a_reviewer_may_send(db, client, auth_secret, monkeypatch):
    run_id = rejected_invoice()
    link_email_sender(run_id)
    gmail_connected()
    monkeypatch.setattr(oauth_google, "api_post_json",
                        lambda url, token, payload: {"id": "m"})
    response = client.post(f"/api/runs/{run_id}/rejection-email/send",
                           headers=auth_headers("analyst"),
                           json={"recipient": "v@x.example", "subject": "s", "body": "b"})
    assert response.status_code == 403


# ==========================================================================
# 5. A failed send is audited correctly, and the run stays REJECTED
# ==========================================================================
def test_a_failed_send_is_audited_and_the_run_stays_rejected(db, client, auth_secret,
                                                              monkeypatch):
    run_id = rejected_invoice()
    link_email_sender(run_id)
    gmail_connected()

    def boom(url, token, payload):
        raise oauth_google.OAuthError("Gmail API refused the request (HTTP 500)",
                                      code="http_500")
    monkeypatch.setattr(oauth_google, "api_post_json", boom)

    response = client.post(f"/api/runs/{run_id}/rejection-email/send",
                           headers=auth_headers("reviewer"),
                           json={"recipient": "billing@acme-office.example",
                                "subject": "s", "body": "b"})
    assert response.status_code == 502

    assert notifications.last_successful_send(run_id) is None
    history = notifications.rejection_email_history(run_id)
    assert len(history) == 1
    assert history[0]["event_type"] == "REJECTION_EMAIL_FAILED"
    assert history[0]["metadata"]["error_category"] == "http_500"
    assert storage.get_run(run_id)["status"] == "REJECTED"


# ==========================================================================
# 6-7. Duplicate-send protection, and resend requires an explicit force
# ==========================================================================
def test_a_second_send_is_refused_without_force(db, client, auth_secret, monkeypatch):
    run_id = rejected_invoice()
    link_email_sender(run_id)
    gmail_connected()
    monkeypatch.setattr(oauth_google, "api_post_json",
                        lambda url, token, payload: {"id": "m1"})
    body = {"recipient": "billing@acme-office.example", "subject": "s", "body": "b"}
    first = client.post(f"/api/runs/{run_id}/rejection-email/send",
                        headers=auth_headers("reviewer"), json=body)
    assert first.status_code == 200

    second = client.post(f"/api/runs/{run_id}/rejection-email/send",
                         headers=auth_headers("reviewer"), json=body)
    assert second.status_code == 409
    assert second.json()["detail"]["previous"]["recipient"] == body["recipient"]

    assert len(notifications.rejection_email_history(run_id)) == 1, \
        "a refused duplicate must not itself be recorded as a second attempt"


def test_resend_with_force_is_permitted_and_recorded(db, client, auth_secret, monkeypatch):
    run_id = rejected_invoice()
    link_email_sender(run_id)
    gmail_connected()
    ids = iter(["m1", "m2"])
    monkeypatch.setattr(oauth_google, "api_post_json",
                        lambda url, token, payload: {"id": next(ids)})
    body = {"recipient": "billing@acme-office.example", "subject": "s", "body": "b"}
    client.post(f"/api/runs/{run_id}/rejection-email/send",
               headers=auth_headers("reviewer"), json=body)
    resend = client.post(f"/api/runs/{run_id}/rejection-email/send",
                         headers=auth_headers("reviewer"), json={**body, "force": True})
    assert resend.status_code == 200
    assert resend.json()["message_id"] == "m2"
    sent = [a for a in notifications.rejection_email_history(run_id)
           if a["event_type"] == "REJECTION_EMAIL_SENT"]
    assert len(sent) == 2
    assert sent[-1]["metadata"]["resend"] is True


# ==========================================================================
# 8-9. Missing / invalid recipient
# ==========================================================================
def test_a_run_with_no_known_vendor_email_has_no_default_recipient(db):
    run_id = rejected_invoice()      # never linked to an email_messages row
    run = storage.get_run(run_id)
    draft = notifications.compose_rejection_email(run)
    assert draft["recipient"] is None


def test_sending_with_no_recipient_supplied_is_refused(db, client, auth_secret, monkeypatch):
    run_id = rejected_invoice()
    gmail_connected()
    monkeypatch.setattr(oauth_google, "api_post_json",
                        lambda url, token, payload: {"id": "m"})
    response = client.post(f"/api/runs/{run_id}/rejection-email/send",
                           headers=auth_headers("reviewer"),
                           json={"recipient": "", "subject": "s", "body": "b"})
    assert response.status_code == 422


@pytest.mark.parametrize("hostile", [
    "not-an-email", "missing-at.example", "a@b",
    "a@b.com\r\nBcc: attacker@evil.example", "a@b.com\ninjected",
])
def test_an_invalid_or_hostile_recipient_is_refused(db, client, auth_secret, monkeypatch,
                                                     hostile):
    run_id = rejected_invoice()
    gmail_connected()
    monkeypatch.setattr(oauth_google, "api_post_json",
                        lambda url, token, payload: {"id": "m"})
    response = client.post(f"/api/runs/{run_id}/rejection-email/send",
                           headers=auth_headers("reviewer"),
                           json={"recipient": hostile, "subject": "s", "body": "b"})
    assert response.status_code == 422
    assert notifications.last_successful_send(run_id) is None


# ==========================================================================
# Gmail send scope -- the least-privilege boundary this feature adds
# ==========================================================================
def test_a_mailbox_connected_before_this_feature_cannot_send(db, client, auth_secret,
                                                              monkeypatch):
    """A connection granted only gmail.readonly (every mailbox connected
    before this feature existed) must be refused a send with a clear reason,
    not a crash -- and nothing is sent."""
    run_id = rejected_invoice()
    gmail_connected(scopes=(config.GMAIL_SCOPE_READONLY,))
    called = []
    monkeypatch.setattr(oauth_google, "api_post_json",
                        lambda *a, **k: called.append(1) or {"id": "m"})
    response = client.post(f"/api/runs/{run_id}/rejection-email/send",
                           headers=auth_headers("reviewer"),
                           json={"recipient": "billing@acme-office.example",
                                "subject": "s", "body": "b"})
    assert response.status_code == 502
    assert "permission to send" in response.json()["detail"]
    assert not called, "Gmail must never be called when the scope is missing"


def test_no_gmail_connection_at_all_is_a_clear_failure(db, client, auth_secret):
    run_id = rejected_invoice()
    response = client.post(f"/api/runs/{run_id}/rejection-email/send",
                           headers=auth_headers("reviewer"),
                           json={"recipient": "billing@acme-office.example",
                                "subject": "s", "body": "b"})
    assert response.status_code == 502
    assert "no connected Gmail mailbox" in response.json()["detail"]


def test_the_preview_reports_whether_sending_is_available(db, client, auth_secret):
    run_id = rejected_invoice()
    not_connected = client.get(f"/api/runs/{run_id}/rejection-email",
                               headers=auth_headers("viewer")).json()
    assert not_connected["sender"]["available"] is False

    gmail_connected()
    connected = client.get(f"/api/runs/{run_id}/rejection-email",
                           headers=auth_headers("viewer")).json()
    assert connected["sender"]["available"] is True


# ==========================================================================
# 11. Nothing about a token/secret ever reaches the audit trail
# ==========================================================================
def test_no_secret_reaches_the_activity_record(db, client, auth_secret, monkeypatch):
    run_id = rejected_invoice()
    link_email_sender(run_id)
    gmail_connected()
    monkeypatch.setattr(oauth_google, "api_post_json",
                        lambda url, token, payload: {"id": "m1"})
    client.post(f"/api/runs/{run_id}/rejection-email/send",
               headers=auth_headers("reviewer"),
               json={"recipient": "billing@acme-office.example", "subject": "s", "body": "b"})

    dump = str(storage.list_activity(run_id))
    for secret in ("access-token-value", "refresh-token-value", "a-stable-test-signing-secret",
                  "AUTH_SECRET"):
        assert secret not in dump


def test_a_failed_send_also_carries_no_secret(db, client, auth_secret, monkeypatch):
    run_id = rejected_invoice()
    link_email_sender(run_id)
    gmail_connected()

    def boom(url, token, payload):
        raise oauth_google.OAuthError(
            f"Google refused the request (invalid_grant, token={token})", code="http_401")
    monkeypatch.setattr(oauth_google, "api_post_json", boom)

    client.post(f"/api/runs/{run_id}/rejection-email/send",
               headers=auth_headers("reviewer"),
               json={"recipient": "billing@acme-office.example", "subject": "s", "body": "b"})
    dump = str(storage.list_activity(run_id))
    assert "access-token-value" not in dump


# ==========================================================================
# Export -- PDF and CSV
# ==========================================================================
def test_pdf_export_succeeds_and_contains_invoice_and_reasons(db, client):
    run_id = rejected_invoice()
    pdf_bytes = audit_export.build_pdf(run_id)
    assert pdf_bytes[:4] == b"%PDF"

    import pdfplumber, io
    text = "\n".join(p.extract_text() or "" for p in pdfplumber.open(io.BytesIO(pdf_bytes)).pages)
    run = storage.get_run(run_id)
    assert VENDOR in text
    reasons = notifications.rejection_reasons(run)
    assert reasons
    assert any(r in text for r in reasons)
    assert "Audit History" in text


def test_pdf_contains_rejection_email_section_after_a_send(db, client, auth_secret, monkeypatch):
    run_id = rejected_invoice()
    link_email_sender(run_id, address="vendor@acme-office.example")
    gmail_connected()
    monkeypatch.setattr(oauth_google, "api_post_json",
                        lambda url, token, payload: {"id": "m1"})
    client.post(f"/api/runs/{run_id}/rejection-email/send",
               headers=auth_headers("reviewer"),
               json={"recipient": "vendor@acme-office.example", "subject": "Invoice Rejected",
                    "body": "body text"})

    import pdfplumber, io
    pdf_bytes = audit_export.build_pdf(run_id)
    text = "\n".join(p.extract_text() or "" for p in pdfplumber.open(io.BytesIO(pdf_bytes)).pages)
    assert "vendor@acme-office.example" in text


def test_csv_export_contains_expected_fields(db):
    run_id = rejected_invoice()
    csv_text = audit_export.build_csv(run_id)
    import csv, io
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert rows
    for field in ("invoice_id", "invoice_number", "vendor", "invoice_status",
                  "rejection_reason", "audit_event", "audit_actor"):
        assert field in rows[0]
    assert all(r["invoice_id"] == str(run_id) for r in rows)
    assert any(r["rejection_reason"] for r in rows)


def test_export_works_for_an_approved_invoice_too(db):
    run_id = approved_invoice()
    pdf_bytes = audit_export.build_pdf(run_id)
    csv_text = audit_export.build_csv(run_id)
    assert pdf_bytes[:4] == b"%PDF"
    assert "APPROVED" in csv_text


def test_export_over_http_requires_authorization(db, client):
    run_id = rejected_invoice()
    assert client.get(f"/api/runs/{run_id}/audit-report.pdf").status_code == 401
    assert client.get(f"/api/runs/{run_id}/audit-report.csv").status_code == 401
    ok = client.get(f"/api/runs/{run_id}/audit-report.pdf", headers=auth_headers("viewer"))
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "application/pdf"


def test_export_filenames_are_sanitised_against_a_hostile_invoice_number(db):
    extracted = {
        "vendor_name": VENDOR, "invoice_number": "../../../etc/passwd; DROP TABLE runs;",
        "total": 100.0, "subtotal": 100.0, "tax": 0.0,
        "po_references": [], "currency": "USD", "extraction_method": "groq (text)",
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
    run_id, _, _ = storage.save_run_checked(
        "hostile.pdf", status, extracted, po_match, [], reasons,
        tolerance_for=matching.tolerance_for, audit=audit)
    run = storage.get_run(run_id)
    stub = audit_export.safe_filename_stub(run)
    assert "/" not in stub and ".." not in stub and ";" not in stub and " " not in stub


def test_no_secret_appears_in_either_export(db, client, auth_secret, monkeypatch):
    run_id = rejected_invoice()
    link_email_sender(run_id)
    gmail_connected()
    monkeypatch.setattr(oauth_google, "api_post_json",
                        lambda url, token, payload: {"id": "m1"})
    client.post(f"/api/runs/{run_id}/rejection-email/send",
               headers=auth_headers("reviewer"),
               json={"recipient": "billing@acme-office.example", "subject": "s", "body": "b"})

    pdf_bytes = audit_export.build_pdf(run_id)
    csv_text = audit_export.build_csv(run_id)
    for secret in (b"access-token-value", b"refresh-token-value"):
        assert secret not in pdf_bytes
    for secret in ("access-token-value", "refresh-token-value"):
        assert secret not in csv_text


# ==========================================================================
# A client token (Phase J) reaches none of this -- structural, via the
# existing internal-route sweep in test_client_portal.py; spot-checked here.
# ==========================================================================
def test_a_portal_client_token_cannot_reach_rejection_email_or_export(db, client):
    headers = auth_headers("client", "acme")
    run_id = rejected_invoice()
    for path in (f"/api/runs/{run_id}/rejection-email",
                f"/api/runs/{run_id}/audit-report.pdf",
                f"/api/runs/{run_id}/audit-report.csv"):
        assert client.get(path, headers=headers).status_code in (401, 403)
    assert client.post(f"/api/runs/{run_id}/rejection-email/send", headers=headers,
                       json={"recipient": "x@y.com", "subject": "s", "body": "b"}
                       ).status_code in (401, 403)
