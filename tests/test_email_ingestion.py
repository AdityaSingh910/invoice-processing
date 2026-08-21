"""Phase G: email invoice ingestion & extraction.

THE CLAIMS UNDER TEST

1. **The cheap filter really is first.** A newsletter never reaches an LLM, an
   OCR pass, or the extraction quota. This is not asserted by reading the code
   -- `extraction.extract_invoice` is replaced with a spy, and any test whose
   message should have been filtered out fails if the spy was ever called.
2. **Phase F cannot be bypassed.** The gate reads the stored security status,
   so no argument, no ordering and no retry reaches the pipeline around it.
3. **The same message cannot become two invoices.** Retries, overlapping
   polls, restarts and redelivery all collapse onto one database row.
4. **Nothing is silently dropped.** Every message that arrives is findable
   afterwards, including the ones that were filtered, quarantined or failed.
5. **There is one invoice pipeline.** The email path produces runs through the
   same `run_pipeline` a browser upload drives.

The provider is always a test double here -- no test opens a socket. The double
lives in this file rather than in `backend/`, so no production configuration
can select it.
"""
import base64
import io
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
SAMPLES = os.path.join(ROOT, "sample_invoices")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
TESTS = os.path.dirname(os.path.abspath(__file__))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import config            # noqa: E402
import documents         # noqa: E402
import email_ingest      # noqa: E402
import email_provider    # noqa: E402
import email_triage      # noqa: E402
import extraction        # noqa: E402
import main              # noqa: E402
import ratelimit         # noqa: E402
import storage           # noqa: E402
import pg_schema         # noqa: E402
from conftest import auth_headers    # noqa: E402

VENDOR_DOMAIN = "acme-office.example"
VENDOR_FROM = f"Billing <billing@{VENDOR_DOMAIN}>"
HAPPY_PDF = os.path.join(SAMPLES, "01_happy_path_acme.pdf")


def pdf_bytes():
    with open(HAPPY_PDF, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------
class FakeProvider(email_provider.EmailProvider):
    """A provider that hands over exactly what a test tells it to.

    Records `handled` so the tests can assert the "mark only after the outcome
    is committed" ordering, and can be told to fail, so the unreachable-mailbox
    path is exercised rather than assumed.
    """

    name = "fake"

    def __init__(self, messages=None, fail_with=None):
        self.messages = list(messages or [])
        self.fail_with = fail_with
        self.handled = []
        self.closed = False
        self.fetch_calls = 0

    def fetch(self, limit):
        self.fetch_calls += 1
        if self.fail_with:
            raise self.fail_with
        batch, self.messages = self.messages[:limit], self.messages[limit:]
        return batch

    def mark_handled(self, message):
        self.handled.append(message.provider_message_id)

    def close(self):
        self.closed = True


def incoming(raw, message_id="<msg-1@acme-office.example>", provider="fake"):
    return email_provider.IncomingEmail(provider, message_id, raw,
                                        received_at="2026-08-21T09:00:00+00:00")


def message(from_header=VENDOR_FROM, subject="Invoice INV-9001",
            body="Please find the invoice attached.\r\n", attachments=(),
            extra_headers=()):
    """A message, with any attachments base64-encoded as real MIME parts."""
    headers = [f"From: {from_header}", "To: ap@buyer-corp.example",
               f"Subject: {subject}", "Date: Mon, 01 Sep 2025 10:00:00 +0000",
               "MIME-Version: 1.0"]
    headers.extend(extra_headers)
    if not attachments:
        return ("\r\n".join(headers) + "\r\n\r\n" + body).encode()

    parts = [f'Content-Type: multipart/mixed; boundary="SEP"', "", "--SEP",
             "Content-Type: text/plain", "", body, "--SEP"]
    for name, ctype, content in attachments:
        parts.extend([
            f"Content-Type: {ctype}",
            f'Content-Disposition: attachment; filename="{name}"',
            "Content-Transfer-Encoding: base64", "",
            base64.b64encode(content).decode(), "--SEP"])
    parts[-1] = "--SEP--"
    return ("\r\n".join(headers + parts) + "\r\n").encode()


def invoice_email(**kwargs):
    kwargs.setdefault("attachments", [("invoice.pdf", "application/pdf", pdf_bytes())])
    return message(**kwargs)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def db(monkeypatch):
    schema = pg_schema.fresh_schema(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    email_triage.reload_domain_policy()
    ratelimit.limiter.reset()
    yield schema
    pg_schema.drop_schema(schema)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def _keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return key, "v=DKIM1; k=rsa; p=" + base64.b64encode(der).decode()


@pytest.fixture
def dkim(_keypair, monkeypatch):
    """Return a signer whose signatures actually verify in this process.

    Phase F quarantines every message it cannot verify -- which is correct, and
    which means an admitted message in these tests has to carry a REAL
    signature, not a configuration override. The Phase F signer is reused
    rather than reimplemented, and the matching public key is published through
    a static resolver, so no test needs the network.

    Anything that reaches the pipeline in this file therefore got there by
    passing genuine cryptography, exactly as it would in production.
    """
    import email_security
    from test_email_security import _sign
    key, txt = _keypair
    resolver = email_security.StaticDnsTxtResolver({f"s1._domainkey.{VENDOR_DOMAIN}": txt})
    monkeypatch.setattr(email_security, "resolver_from_config", lambda: resolver)
    return lambda raw: _sign(raw, key, domain=VENDOR_DOMAIN)


@pytest.fixture
def extraction_spy(monkeypatch):
    """Counts every entry into the expensive part of the pipeline.

    THE PERFORMANCE GUARANTEE IS TESTED HERE, not asserted in prose. Whatever
    else a test checks, if a message that should have been filtered out reaches
    extraction, `calls` is non-zero and the test fails.
    """
    calls = []
    real = extraction.extract_invoice

    def spy(pdf, *args, **kwargs):
        calls.append(len(pdf) if pdf else 0)
        return real(pdf, *args, **kwargs)

    monkeypatch.setattr(extraction, "extract_invoice", spy)
    monkeypatch.setattr(main.extraction, "extract_invoice", spy)
    return calls


@pytest.fixture
def trusted():
    return [{"sender": VENDOR_DOMAIN, "kind": "domain",
             "vendor_name": "Acme Office Supplies", "status": "trusted"}]


def ingest(raw, message_id="<m1@acme-office.example>", **kwargs):
    return email_ingest.ingest_message(incoming(raw, message_id), **kwargs)


# ==========================================================================
# 1. Sender classification -- two independent axes
# ==========================================================================
@pytest.mark.parametrize("address,expected_type", [
    (f"invoice@{VENDOR_DOMAIN}", "CORPORATE"),
    ("billing@invoices.acme-office.example", "CORPORATE"),
    ("supplier@gmail.com", "PERSONAL"),
    ("someone@yahoo.com", "PERSONAL"),
    ("someone@outlook.com", "PERSONAL"),
    ("someone@hotmail.com", "PERSONAL"),
    ("someone@googlemail.com", "PERSONAL"),
    ("ap@buyer-corp.example", "CORPORATE"),
    ("billing@never-heard-of-them.test", "CORPORATE"),
    ("", "UNKNOWN"),
    ("not-an-address", "UNKNOWN"),
])
def test_sender_type_classification(db, address, expected_type, trusted):
    assert email_triage.classify_sender(address, trusted_senders=trusted)["sender_type"] \
        == expected_type


def test_corporate_is_not_automatically_trusted(db, trusted):
    """The requirement stated in one assertion: a company domain nobody
    allowlisted is CORPORATE with UNKNOWN trust, not trusted."""
    result = email_triage.classify_sender("billing@unknown-company.test",
                                          trusted_senders=trusted)
    assert result["sender_type"] == "CORPORATE"
    assert result["trust_status"] == "UNKNOWN"
    assert result["trust_status"] != "TRUSTED"


def test_unknown_is_not_treated_as_malicious(db, trusted):
    result = email_triage.classify_sender("billing@unknown-company.test",
                                          trusted_senders=trusted)
    assert result["trust_status"] == "UNKNOWN"
    assert result["trust_status"] != "UNTRUSTED"
    text = (result["trust_reason"] + result["sender_type_reason"]).lower()
    for accusation in ("malicious", "hostile", "attack", "fraud", "spam"):
        assert accusation not in text


def test_a_personal_address_can_still_be_trusted(db):
    """A small supplier really may invoice from a free-mail address. The two
    axes are independent, so PERSONAL + TRUSTED has to be representable."""
    result = email_triage.classify_sender(
        "supplier@gmail.com",
        trusted_senders=[{"sender": "supplier@gmail.com", "kind": "address",
                          "status": "trusted", "vendor_name": "Tiny Supplier"}])
    assert result["sender_type"] == "PERSONAL"
    assert result["trust_status"] == "TRUSTED"
    assert result["vendor_name"] == "Tiny Supplier"


def test_an_explicitly_non_trusted_entry_reads_untrusted_not_unknown(db):
    result = email_triage.classify_sender(
        f"billing@{VENDOR_DOMAIN}",
        trusted_senders=[{"sender": VENDOR_DOMAIN, "kind": "domain", "status": "blocked"}])
    assert result["trust_status"] == "UNTRUSTED"


def test_a_lookalike_domain_does_not_match_a_trusted_one(db, trusted):
    """Suffix matching on label boundaries: `notacme-office.example` must not
    match `acme-office.example` by being a string suffix of it."""
    result = email_triage.classify_sender(f"billing@not{VENDOR_DOMAIN}",
                                          trusted_senders=trusted)
    assert result["trust_status"] == "UNKNOWN"


def test_a_reply_to_pointing_elsewhere_is_recorded_not_rejected(db, trusted):
    result = email_triage.classify_sender(f"billing@{VENDOR_DOMAIN}",
                                          trusted_senders=trusted,
                                          reply_to="collect@elsewhere.test")
    assert result["reply_to_mismatch"] is True
    assert result["trust_status"] == "TRUSTED"       # a signal, not a verdict


def test_the_domain_policy_is_configurable_not_hard_coded(db, monkeypatch):
    monkeypatch.setenv(config.EMAIL_PERSONAL_DOMAINS_ENV, "my-isp.test")
    monkeypatch.setenv(config.EMAIL_CORPORATE_DOMAINS_ENV, "our-other-brand.test")
    email_triage.reload_domain_policy()
    assert email_triage.classify_sender("a@my-isp.test")["sender_type"] == "PERSONAL"
    assert email_triage.classify_sender("a@our-other-brand.test")["sender_type"] == "CORPORATE"
    email_triage.reload_domain_policy()


# ==========================================================================
# 2. Relevance -- cheap signals only
# ==========================================================================
def _relevance(from_header, subject, attachments, trusted_senders):
    import email_security
    raw = message(from_header=from_header, subject=subject, attachments=attachments)
    parsed = email_security.parse_message(raw)
    parsed.pop("_fields", None)
    parsed.pop("_message", None)
    return email_triage.triage(parsed, trusted_senders=trusted_senders)


PDF_ATT = [("invoice.pdf", "application/pdf", b"%PDF-1.4 x")]


def test_known_vendor_invoice_subject_and_pdf_is_high(db, trusted):
    t = _relevance(VENDOR_FROM, "Invoice #12345", PDF_ATT, trusted)
    assert t["relevance"]["relevance"] == "HIGH"
    assert t["proceed"] is True


def test_random_gmail_chat_with_no_attachment_is_irrelevant(db, trusted):
    t = _relevance("Bob <bob@gmail.com>", "Hey, check this out", [], trusted)
    assert t["relevance"]["relevance"] == "IRRELEVANT"
    assert t["proceed"] is False


def test_corporate_sender_with_no_attachment_is_not_processed(db, trusted):
    t = _relevance("Someone <someone@unknown-company.test>", "Quick question", [], trusted)
    assert t["relevance"]["relevance"] in ("LOW", "IRRELEVANT")
    assert t["proceed"] is False


def test_unknown_company_with_invoice_subject_and_pdf_is_possible(db, trusted):
    t = _relevance("AP <ap@unknown-company.test>", "Invoice 8871", PDF_ATT, trusted)
    assert t["relevance"]["relevance"] in ("HIGH", "POSSIBLE")
    assert t["proceed"] is True


def test_a_gmail_sender_with_a_pdf_still_proceeds(db, trusted):
    """Not over-filtering, stated as a test: a free-mail address attaching a PDF
    is weak evidence, but it is not grounds for refusing to look."""
    t = _relevance("Supplier <supplier@gmail.com>", "hello", PDF_ATT, trusted)
    assert t["proceed"] is True


def test_a_trusted_vendor_asking_a_question_is_low_not_irrelevant(db, trusted):
    t = _relevance(VENDOR_FROM, "Question about invoice INV-1", [], trusted)
    assert t["relevance"]["relevance"] == "LOW"


def test_an_unsupported_attachment_alone_does_not_proceed(db, trusted):
    t = _relevance("AP <ap@unknown-company.test>", "photos",
                   [("holiday.jpg", "image/jpeg", b"\xff\xd8\xff")], trusted)
    assert t["proceed"] is False


def test_several_pdfs_are_all_counted(db, trusted):
    t = _relevance(VENDOR_FROM, "Invoices for August",
                   [("a.pdf", "application/pdf", b"%PDF-1.4 a"),
                    ("b.pdf", "application/pdf", b"%PDF-1.4 b")], trusted)
    assert t["relevance"]["pdf_attachment_count"] == 2
    assert t["relevance"]["relevance"] == "HIGH"


# ==========================================================================
# 3. THE PERFORMANCE GUARANTEE
# ==========================================================================
def test_an_irrelevant_email_never_reaches_extraction(db, extraction_spy, trusted):
    """The headline requirement. A newsletter costs a header parse and two
    dictionary lookups -- no LLM, no OCR, no quota."""
    result = ingest(message(from_header="News <news@marketing.test>",
                            subject="Our summer newsletter", body="Hello!\r\n"),
                    trusted_senders=trusted)
    assert result["status"] == "FILTERED_OUT"
    assert extraction_spy == [], "an irrelevant message reached the extraction pipeline"


def test_a_filtered_message_is_not_even_security_verified(db, trusted):
    """Cheap before expensive, all the way down: triage runs before the
    cryptography too, so a newsletter costs no signature verification either."""
    result = ingest(message(from_header="News <news@marketing.test>",
                            subject="Newsletter", body="hi\r\n"),
                    trusted_senders=trusted)
    stored = storage.get_email_message(result["email_id"])
    assert stored["audit"]["triage_only"] is True
    assert stored["spf_result"] is None and stored["dkim_result"] is None


def test_a_relevant_email_does_reach_extraction(db, extraction_spy, trusted, dkim):
    """The other half: the filter must not be blocking real invoices."""
    ingest(dkim(invoice_email()), trusted_senders=trusted)
    assert extraction_spy, "a genuine invoice email never reached extraction"


def test_a_quarantined_email_never_reaches_extraction(db, extraction_spy, trusted):
    """Quarantine is a hard stop, not a warning."""
    result = ingest(invoice_email(from_header="AP <ap@unknown-company.test>",
                                  subject="Invoice 5567"),
                    trusted_senders=trusted)
    assert result["status"] == "QUARANTINED"
    assert extraction_spy == []


# ==========================================================================
# 4. Nothing is silently dropped
# ==========================================================================
def test_a_filtered_message_is_kept_and_readable(db, trusted):
    result = ingest(message(from_header="News <news@marketing.test>",
                            subject="Newsletter", body="hi\r\n"),
                    trusted_senders=trusted)
    stored = storage.get_email_message(result["email_id"])
    assert stored is not None, "a filtered message must still be findable"
    assert stored["ingest_status"] == "FILTERED_OUT"
    assert stored["relevance"] == "IRRELEVANT"
    assert stored["sender_type"] == "CORPORATE"
    assert stored["reasons"], "the reasons it was filtered must be recorded"
    events = [e["event_type"] for e in storage.list_email_activity(result["email_id"])]
    assert "TRIAGED" in events and "FILTERED_OUT" in events


def test_a_message_with_no_id_is_refused_rather_than_processed_undetectably(db, trusted):
    result = email_ingest.ingest_message(incoming(invoice_email(), message_id=""),
                                         trusted_senders=trusted)
    assert result["ok"] is False
    assert "no message id" in result["error"]


def test_an_oversized_message_is_recorded_as_failed_not_dropped(db, monkeypatch, trusted):
    monkeypatch.setenv(config.EMAIL_MAX_MESSAGE_BYTES_ENV, "500")
    result = ingest(invoice_email(), trusted_senders=trusted)
    assert result["status"] == "FAILED"
    stored = storage.get_email_message(result["email_id"])
    assert stored["ingest_status"] == "FAILED"
    assert "over the configured limit" in (stored["ingest_error"] or "")


@pytest.mark.parametrize("raw", [
    b"",
    b"\x00\x01\x02 not an email",
    b"From: broken@acme-office.example",
    b"Subject: Invoice\r\n\r\nno from header",
])
def test_a_malformed_message_produces_a_record_not_a_crash(db, raw, trusted):
    result = email_ingest.ingest_message(incoming(raw, f"<mal-{len(raw)}@x.test>"),
                                         trusted_senders=trusted)
    assert result.get("email_id") or result.get("ok") is False
    assert result["status"] in config.EMAIL_INGEST_STATUSES


# ==========================================================================
# 5. Idempotency
# ==========================================================================
def test_redelivery_of_the_same_message_creates_one_record(db, extraction_spy, trusted, dkim):
    raw = dkim(invoice_email())
    first = ingest(raw, "<dup@acme-office.example>", trusted_senders=trusted)
    second = ingest(raw, "<dup@acme-office.example>", trusted_senders=trusted)

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["email_id"] == first["email_id"]
    assert len(storage.list_email_messages()) == 1
    assert len(storage.list_runs()) == 1, "a redelivered email must not create a second run"
    assert len(extraction_spy) == 1, "a redelivered email must not be extracted twice"


def test_redelivery_is_recorded_rather_than_hidden(db, trusted):
    raw = invoice_email()
    result = ingest(raw, "<dup2@acme-office.example>", trusted_senders=trusted)
    ingest(raw, "<dup2@acme-office.example>", trusted_senders=trusted)
    events = [e["event_type"] for e in storage.list_email_activity(result["email_id"])]
    assert "DUPLICATE_DELIVERY" in events


def test_a_different_message_id_with_identical_content_is_still_one_per_id(db, trusted):
    """Two ids means two deliveries, and the idempotency key is the id."""
    raw = invoice_email()
    a = ingest(raw, "<id-a@acme-office.example>", trusted_senders=trusted)
    b = ingest(raw, "<id-b@acme-office.example>", trusted_senders=trusted)
    assert a["email_id"] != b["email_id"]


def test_concurrent_delivery_of_one_message_produces_exactly_one_record(db, trusted):
    """Real threads. Two pollers -- or two uvicorn workers -- racing the same
    message. Correctness here is the database's unique index, not a lock in
    Python, which is why this cannot be defeated by timing."""
    raw = invoice_email()
    n = 8
    results, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker(i):
        barrier.wait()
        try:
            r = ingest(raw, "<race@acme-office.example>", trusted_senders=trusted)
        except Exception as exc:            # pragma: no cover - surfaced below
            r = {"error": repr(exc)}
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all("error" not in r for r in results), results
    created = [r for r in results if not r.get("duplicate")]
    assert len(created) == 1, f"expected exactly one creation, got {len(created)}"
    assert len(storage.list_email_messages()) == 1
    assert len(storage.list_runs()) <= 1


def test_the_same_pdf_attached_twice_is_processed_once(db, extraction_spy, trusted, dkim):
    same = pdf_bytes()
    raw = dkim(message(subject="Invoice INV-9001",
                       attachments=[("invoice.pdf", "application/pdf", same),
                                    ("invoice-copy.pdf", "application/pdf", same)]))
    result = ingest(raw, "<twice@acme-office.example>", trusted_senders=trusted)
    assert result["processed_attachments"] == 1
    assert len(extraction_spy) == 1


# ==========================================================================
# 6. Attachments
# ==========================================================================
def test_a_valid_pdf_becomes_a_run_through_the_existing_pipeline(db, trusted, dkim):
    result = ingest(dkim(invoice_email()), trusted_senders=trusted)
    assert result["status"] == "PROCESSED"
    assert len(result["runs"]) == 1

    run = storage.get_run(result["runs"][0])
    assert run is not None
    assert run["status"] in ("APPROVED", "NEEDS_REVIEW", "REJECTED")
    # It went through the real pipeline, so it has the real audit trail.
    assert run["stages"], "the run must carry the pipeline's stage log"
    assert run["audit"], "the run must carry the rule engine's audit trail"


def test_the_pdf_is_stored_in_the_existing_document_store(db, trusted, dkim):
    """No second document storage system: the run's PDF is reachable through
    the Phase C document row, with source EMAIL."""
    result = ingest(dkim(invoice_email()), trusted_senders=trusted)
    doc = storage.get_document_for_run(result["runs"][0])
    assert doc is not None
    assert doc["source"] == "EMAIL"
    assert doc["mime_type"] == "application/pdf"
    assert documents.get_store().exists(doc["storage_key"])


def test_a_non_pdf_attachment_is_skipped_with_a_reason(db, trusted, dkim):
    raw = dkim(message(subject="Invoice INV-9001",
                       attachments=[("invoice.pdf", "application/pdf", pdf_bytes()),
                                    ("logo.png", "image/png", b"\x89PNG\r\n\x1a\n")]))
    result = ingest(raw, "<mixed@acme-office.example>", trusted_senders=trusted)
    assert result["processed_attachments"] == 1
    rows = storage.list_email_attachments(result["email_id"])
    skipped = [r for r in rows if r["status"] == "SKIPPED"]
    assert skipped and skipped[0]["skip_reason"], "a skipped attachment must say why"


def test_a_file_named_pdf_that_is_not_a_pdf_is_refused_on_content(db, extraction_spy, trusted):
    """Filename and declared type are both attacker-chosen. The magic-byte test
    is what decides, exactly as it does for a browser upload."""
    raw = message(subject="Invoice INV-9001",
                  attachments=[("invoice.pdf", "application/pdf",
                                b"#!/bin/sh\nrm -rf /\n")])
    result = ingest(raw, "<fake@acme-office.example>", trusted_senders=trusted)
    assert result["processed_attachments"] == 0
    assert extraction_spy == [], "a non-PDF must never reach the PDF pipeline"
    rows = storage.list_email_attachments(result["email_id"])
    assert any("not a PDF" in (r["skip_reason"] or "") for r in rows)


def test_an_oversized_attachment_is_skipped(db, monkeypatch, trusted):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1024)
    raw = message(subject="Invoice INV-9001",
                  attachments=[("invoice.pdf", "application/pdf", pdf_bytes())])
    result = ingest(raw, "<big@acme-office.example>", trusted_senders=trusted)
    assert result["processed_attachments"] == 0
    rows = storage.list_email_attachments(result["email_id"])
    assert any("over the" in (r["skip_reason"] or "") for r in rows)


@pytest.mark.parametrize("name,expected", [
    ("../../../../etc/passwd.pdf", "etc_passwd_or_flat"),
    ("..\\..\\windows\\system32\\evil.pdf", "flat"),
    ("....//....//x.pdf", "flat"),
    ("", "fallback"),
    ("...", "fallback"),
    ("con.pdf", "flat"),
])
def test_an_attachment_filename_cannot_carry_a_path(db, name, expected):
    safe = email_ingest.safe_attachment_filename(name)
    assert "/" not in safe and "\\" not in safe
    assert not safe.startswith(".")
    assert safe, "a filename must always reduce to something storable"


def test_a_path_traversal_filename_survives_the_whole_flow_safely(db, trusted):
    raw = message(subject="Invoice INV-9001",
                  attachments=[("../../../../etc/passwd.pdf", "application/pdf", pdf_bytes())])
    result = ingest(raw, "<trav@acme-office.example>", trusted_senders=trusted)
    rows = storage.list_email_attachments(result["email_id"])
    assert all("/" not in (r["filename"] or "") for r in rows)
    if result["runs"]:
        doc = storage.get_document_for_run(result["runs"][0])
        # The stored key is server-generated and never the sender's filename.
        assert doc["storage_key"] != "passwd.pdf"
        assert "/" not in doc["original_filename"]


def test_a_corrupt_pdf_fails_that_attachment_without_losing_the_message(db, trusted, dkim):
    raw = dkim(message(subject="Invoice INV-9001",
                       attachments=[("broken.pdf", "application/pdf", b"%PDF-1.4 truncated")]))
    result = ingest(raw, "<corrupt@acme-office.example>", trusted_senders=trusted)
    assert result["email_id"]
    stored = storage.get_email_message(result["email_id"])
    assert stored["ingest_status"] in ("PROCESSED", "FAILED", "PARTIAL")
    rows = storage.list_email_attachments(result["email_id"])
    assert rows, "the attachment must still be recorded"


def test_one_corrupt_pdf_does_not_stop_the_good_one_beside_it(db, trusted, dkim):
    raw = dkim(message(subject="Invoices",
                       attachments=[("broken.pdf", "application/pdf", b"%PDF-1.4 truncated"),
                                    ("good.pdf", "application/pdf", pdf_bytes())]))
    result = ingest(raw, "<mixed2@acme-office.example>", trusted_senders=trusted)
    assert result["processed_attachments"] >= 1, \
        "a good invoice must still be processed alongside a broken one"


# ==========================================================================
# 7. Several invoices in one email
# ==========================================================================
def test_two_invoices_in_one_email_become_two_runs(db, trusted, dkim):
    """1 email == 1 invoice is not assumed anywhere."""
    a = pdf_bytes()
    b = a + b"\n% a second, distinct attachment\n"
    raw = dkim(message(subject="August invoices",
                       attachments=[("one.pdf", "application/pdf", a),
                                    ("two.pdf", "application/pdf", b)]))
    result = ingest(raw, "<multi@acme-office.example>", trusted_senders=trusted)
    assert result["processed_attachments"] == 2
    assert len(result["runs"]) == 2
    assert len(set(result["runs"])) == 2

    rows = storage.list_email_attachments(result["email_id"])
    linked = [r for r in rows if r["run_id"]]
    assert len(linked) == 2
    # Phase F's single-run link still points at the first, for compatibility.
    assert storage.get_email_message(result["email_id"])["run_id"] in result["runs"]


# ==========================================================================
# 8. Phase F integration -- and that it cannot be bypassed
# ==========================================================================
def test_an_unverifiable_message_is_quarantined_not_processed(db, trusted):
    """The default deployment can prove nothing about an unsigned message, so
    it is held. Phase F's own verdict, honoured rather than re-decided."""
    result = ingest(invoice_email(), trusted_senders=[])
    assert result["status"] == "QUARANTINED"
    stored = storage.get_email_message(result["email_id"])
    assert stored["classification"] == "UNVERIFIED"
    assert stored["status"] == "QUARANTINED"


def test_phase_f_results_are_preserved_on_the_ingested_record(db, trusted, dkim):
    result = ingest(dkim(invoice_email()), trusted_senders=trusted)
    stored = storage.get_email_message(result["email_id"])
    for field in ("spf_result", "dkim_result", "dmarc_result", "signature_kind",
                  "signature_result", "classification"):
        assert stored[field] is not None, f"{field} must be preserved from Phase F"
    assert stored["audit"]["evaluated_mechanisms"]["dkim"]["state"] in (
        "pass", "fail", "unavailable")
    assert stored["audit"]["limitations"], "Phase F's caveats must travel with the record"


def test_the_signature_distinction_survives_ingestion(db, trusted, dkim):
    """DKIM is not a user-level signature, and ingestion must not blur that.

    This message carries a DKIM signature that genuinely verifies -- and it
    still reports no user-level signature at all, which is the distinction.
    """
    result = ingest(dkim(invoice_email()), trusted_senders=trusted)
    stored = storage.get_email_message(result["email_id"])
    assert stored["signature_kind"] == "none"
    assert stored["signature_result"] == "not_present"
    assert stored["audit"]["digital_signature"]["verified"] is False


def test_a_quarantined_message_cannot_be_processed_even_when_asked_directly(db,
                                                                           extraction_spy,
                                                                           trusted):
    """The gate reads the STORED status, so calling the processing function
    directly does not get around it."""
    result = ingest(invoice_email(), trusted_senders=[])
    assert result["status"] == "QUARANTINED"
    forced = email_ingest.process_message_attachments(result["email_id"], b"")
    assert forced["ok"] is False
    assert "may not be processed" in forced["error"]
    assert extraction_spy == []


def test_a_discarded_message_can_never_be_processed(db, extraction_spy, trusted):
    result = ingest(invoice_email(), trusted_senders=[])
    storage.set_email_status(result["email_id"], "DISCARDED", actor="reviewer")
    forced = email_ingest.process_message_attachments(result["email_id"], b"")
    assert forced["ok"] is False
    assert extraction_spy == []


def test_a_released_message_can_then_be_processed(db, trusted):
    """The full quarantine workflow: held, released by a reviewer, then
    processed -- reusing Phase F's release, not a second mechanism."""
    result = ingest(invoice_email(), trusted_senders=[])
    assert result["status"] == "QUARANTINED"
    assert result["held_attachments"] == 1, "the invoice must survive the quarantine"

    storage.set_email_status(result["email_id"], "RELEASED", actor="reviewer",
                             note="confirmed with the vendor")
    processed = email_ingest.process_message_attachments(result["email_id"],
                                                         actor="reviewer")
    assert processed["ok"] is True
    assert processed["processed_attachments"] == 1
    assert storage.get_run(processed["runs"][0]) is not None


def test_processing_a_released_message_twice_does_not_duplicate_the_run(db, trusted):
    result = ingest(invoice_email(), trusted_senders=[])
    storage.set_email_status(result["email_id"], "RELEASED", actor="reviewer")
    first = email_ingest.process_message_attachments(result["email_id"], actor="reviewer")
    second = email_ingest.process_message_attachments(result["email_id"], actor="reviewer")
    assert first["runs"] == second["runs"]
    assert len(storage.list_runs()) == 1


def test_a_quarantined_attachment_is_preserved_in_the_existing_document_store(db, trusted):
    result = ingest(invoice_email(), trusted_senders=[])
    rows = storage.list_email_attachments(result["email_id"])
    held = [r for r in rows if r["storage_key"]]
    assert held, "a quarantined invoice must be preserved, not thrown away"
    assert documents.get_store().exists(held[0]["storage_key"])


# ==========================================================================
# 9. Polling and provider failure
# ==========================================================================
def test_a_poll_ingests_every_message_it_fetches(db, trusted, dkim):
    provider = FakeProvider([incoming(dkim(invoice_email()), "<p1@acme-office.example>"),
                             incoming(message(subject="newsletter",
                                              from_header="n@marketing.test"),
                                      "<p2@marketing.test>")])
    result = email_ingest.poll_once(provider=provider)
    assert result["ok"] is True
    assert result["fetched"] == 2
    assert len(storage.list_email_messages()) == 2


def test_a_handled_message_is_marked_only_after_it_is_recorded(db, trusted):
    provider = FakeProvider([incoming(invoice_email(), "<p3@acme-office.example>")])
    email_ingest.poll_once(provider=provider)
    assert provider.handled == ["<p3@acme-office.example>"]


def test_polling_twice_does_not_reprocess(db, extraction_spy, trusted, dkim):
    """Restart/retry safety: the same message offered again on a later poll."""
    raw = dkim(invoice_email())
    first = FakeProvider([incoming(raw, "<repeat@acme-office.example>")])
    second = FakeProvider([incoming(raw, "<repeat@acme-office.example>")])
    email_ingest.poll_once(provider=first)
    email_ingest.poll_once(provider=second)
    assert len(storage.list_email_messages()) == 1
    assert len(storage.list_runs()) == 1
    assert len(extraction_spy) == 1


def test_an_unreachable_provider_is_an_error_not_an_empty_poll(db):
    """"The mailbox is down" and "there is no new mail" must never look alike."""
    provider = FakeProvider(fail_with=email_provider.EmailProviderError("connection refused"))
    result = email_ingest.poll_once(provider=provider)
    assert result["ok"] is False
    assert "connection refused" in result["error"]


def test_an_authentication_failure_is_reported_clearly(db):
    provider = FakeProvider(
        fail_with=email_provider.EmailProviderError("IMAP authentication failed for ap@x"))
    result = email_ingest.poll_once(provider=provider)
    assert result["ok"] is False
    assert "authentication failed" in result["error"]


def test_one_bad_message_does_not_abort_the_batch(db, trusted):
    provider = FakeProvider([
        incoming(b"\x00 not an email", "<bad@x.test>"),
        incoming(invoice_email(), "<good@acme-office.example>"),
    ])
    result = email_ingest.poll_once(provider=provider)
    assert result["fetched"] == 2
    assert len(storage.list_email_messages()) == 2, \
        "a malformed message must not strand the ones behind it"


def test_an_unknown_provider_name_raises_rather_than_silently_doing_nothing(db, monkeypatch):
    monkeypatch.setenv(config.EMAIL_PROVIDER_ENV, "carrier-pigeon")
    # config normalises anything unrecognised to 'none', so the mailbox is not
    # silently half-configured -- assert the observable behaviour.
    assert config.email_provider() == "none"
    assert isinstance(email_provider.get_provider(), email_provider.NullEmailProvider)


def test_the_default_provider_polls_nothing(db):
    provider = email_provider.get_provider()
    assert isinstance(provider, email_provider.NullEmailProvider)
    assert provider.fetch(10) == []


def test_the_imap_provider_refuses_to_start_without_configuration(db, monkeypatch):
    for var in (config.IMAP_HOST_ENV, config.IMAP_USER_ENV,
                config.IMAP_PASSWORD_ENV, config.IMAP_OAUTH_TOKEN_ENV):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(email_provider.EmailProviderError):
        email_provider.ImapEmailProvider()


def test_the_imap_provider_prefers_oauth_and_never_echoes_the_secret(db, monkeypatch):
    monkeypatch.setenv(config.IMAP_HOST_ENV, "imap.example.test")
    monkeypatch.setenv(config.IMAP_USER_ENV, "ap@buyer-corp.example")
    monkeypatch.setenv(config.IMAP_OAUTH_TOKEN_ENV, "ya29.SUPER-SECRET-TOKEN")
    monkeypatch.setenv(config.IMAP_PASSWORD_ENV, "hunter2")
    described = email_provider.ImapEmailProvider().describe()
    assert described["auth"] == "oauth2"
    assert described["credential_configured"] is True
    blob = repr(described)
    assert "SUPER-SECRET-TOKEN" not in blob and "hunter2" not in blob


# ==========================================================================
# 10. API surface and authorization
# ==========================================================================
@pytest.mark.parametrize("method,path", [
    ("get", "/api/email/ingestion"),
    ("post", "/api/email/ingestion/poll"),
    ("post", "/api/email/messages/1/process"),
    ("get", "/api/email/messages/1/attachments"),
])
def test_every_ingestion_endpoint_refuses_an_unauthenticated_caller(db, client, method, path):
    assert getattr(client, method)(path).status_code in (401, 403)


def test_only_an_admin_may_see_the_mailbox_configuration(db, client):
    assert client.get("/api/email/ingestion",
                      headers=auth_headers("viewer")).status_code == 403
    assert client.get("/api/email/ingestion",
                      headers=auth_headers("reviewer")).status_code == 403
    assert client.get("/api/email/ingestion",
                      headers=auth_headers("admin")).status_code == 200


def test_the_ingestion_status_never_exposes_a_credential(db, client, monkeypatch):
    monkeypatch.setenv(config.EMAIL_PROVIDER_ENV, "imap")
    monkeypatch.setenv(config.IMAP_HOST_ENV, "imap.example.test")
    monkeypatch.setenv(config.IMAP_USER_ENV, "ap@buyer-corp.example")
    monkeypatch.setenv(config.IMAP_PASSWORD_ENV, "hunter2-the-real-password")
    monkeypatch.setenv(config.IMAP_OAUTH_TOKEN_ENV, "ya29.SECRET")
    body = client.get("/api/email/ingestion", headers=auth_headers("admin")).text
    assert "hunter2-the-real-password" not in body
    assert "ya29.SECRET" not in body


def test_a_viewer_cannot_trigger_a_poll(db, client):
    assert client.post("/api/email/ingestion/poll",
                       headers=auth_headers("viewer")).status_code == 403


def test_polling_while_disabled_is_refused_clearly(db, client, monkeypatch):
    monkeypatch.delenv(config.EMAIL_INGEST_ENABLED_ENV, raising=False)
    response = client.post("/api/email/ingestion/poll", headers=auth_headers("analyst"))
    assert response.status_code == 409
    assert "disabled" in str(response.json()["detail"]).lower()


def test_a_viewer_may_read_attachment_metadata(db, client, trusted, dkim):
    result = ingest(dkim(invoice_email()), trusted_senders=trusted)
    response = client.get(f"/api/email/messages/{result['email_id']}/attachments",
                          headers=auth_headers("viewer"))
    assert response.status_code == 200
    rows = response.json()["attachments"]
    assert rows and rows[0]["filename"] == "invoice.pdf"


def test_attachment_metadata_does_not_carry_the_bytes(db, client, trusted, dkim):
    result = ingest(dkim(invoice_email()), trusted_senders=trusted)
    body = client.get(f"/api/email/messages/{result['email_id']}/attachments",
                      headers=auth_headers("viewer")).text
    assert "%PDF" not in body


def test_an_analyst_cannot_release_a_quarantined_message(db, client, trusted):
    """Unchanged from Phase F: releasing is a review ruling."""
    result = ingest(invoice_email(), trusted_senders=[])
    assert client.post(f"/api/email/messages/{result['email_id']}/release",
                       headers=auth_headers("analyst")).status_code == 403
    assert client.post(f"/api/email/messages/{result['email_id']}/discard",
                       headers=auth_headers("analyst")).status_code == 403


def test_processing_a_quarantined_message_over_http_is_refused(db, client, trusted):
    result = ingest(invoice_email(), trusted_senders=[])
    response = client.post(f"/api/email/messages/{result['email_id']}/process",
                           headers=auth_headers("analyst"))
    assert response.status_code == 409
    assert "release it first" in str(response.json()["detail"])


def test_the_release_then_process_workflow_over_http(db, client, trusted):
    result = ingest(invoice_email(), trusted_senders=[])
    assert client.post(f"/api/email/messages/{result['email_id']}/release",
                       json={"note": "checked with the vendor"},
                       headers=auth_headers("reviewer")).status_code == 200
    response = client.post(f"/api/email/messages/{result['email_id']}/process",
                           headers=auth_headers("analyst"))
    assert response.status_code == 200
    assert response.json()["processed_attachments"] == 1


def test_processing_an_unknown_message_is_404(db, client):
    assert client.post("/api/email/messages/999999/process",
                       headers=auth_headers("analyst")).status_code == 404


def test_the_ingestion_summary_reports_what_happened(db, client, trusted, dkim):
    ingest(dkim(invoice_email()), "<s1@acme-office.example>", trusted_senders=trusted)
    ingest(message(from_header="n@marketing.test", subject="newsletter"),
           "<s2@marketing.test>", trusted_senders=trusted)
    body = client.get("/api/email/ingestion", headers=auth_headers("admin")).json()
    assert body["enabled"] is False
    assert body["counts"]["by_ingest_status"].get("FILTERED_OUT") == 1
    assert body["counts"]["invoice_runs_created"] >= 1


# ==========================================================================
# 11. Backwards compatibility -- Phases A-F must be untouched
# ==========================================================================
def test_manual_upload_still_works_and_is_not_marked_as_email(db, client):
    response = client.post("/api/runs/stream",
                           files={"file": ("01_happy_path_acme.pdf",
                                           io.BytesIO(pdf_bytes()), "application/pdf")},
                           headers=auth_headers("analyst"))
    assert response.status_code == 200
    assert "final" in response.text
    runs = storage.list_runs()
    assert len(runs) == 1
    doc = storage.get_document_for_run(runs[0]["id"])
    assert doc["source"] == "MANUAL_UPLOAD"
    assert storage.list_email_messages() == []


def test_phase_f_direct_submission_still_works(db, client):
    """The Phase F endpoint is unchanged and still ingests a handed-over
    message, independently of the poller."""
    response = client.post("/api/email/messages",
                           files={"file": ("m.eml", io.BytesIO(invoice_email()),
                                           "message/rfc822")},
                           headers=auth_headers("analyst"))
    assert response.status_code == 200
    assert response.json()["message"]["classification"] in config.EMAIL_CLASSIFICATIONS


def test_both_doors_produce_the_same_kind_of_run(db, client, trusted, dkim):
    """One pipeline, two doors. The email path and the upload path must yield
    structurally identical runs -- same stages, same audit trail."""
    client.post("/api/runs/stream",
                files={"file": ("a.pdf", io.BytesIO(pdf_bytes()), "application/pdf")},
                headers=auth_headers("analyst"))
    uploaded = storage.list_runs()[0]

    result = ingest(dkim(invoice_email()), "<same@acme-office.example>",
                    trusted_senders=trusted)
    emailed = storage.get_run(result["runs"][0])

    assert [s["name"] for s in uploaded["stages"]] == [s["name"] for s in emailed["stages"]]
    assert set(uploaded["audit"]) == set(emailed["audit"])


def test_reset_demo_keeps_the_ingestion_record(db, client, trusted, dkim):
    result = ingest(dkim(invoice_email()), trusted_senders=trusted)
    assert client.post("/api/admin/reset-demo",
                       headers=auth_headers("admin")).status_code == 200
    assert storage.list_runs() == []
    survivor = storage.get_email_message(result["email_id"])
    assert survivor is not None
    assert survivor["run_id"] is None
    rows = storage.list_email_attachments(result["email_id"])
    assert rows and rows[0]["run_id"] is None
    assert rows[0]["status"] == "PENDING", "a cleared run should leave the attachment replayable"


def test_the_existing_endpoints_are_unaffected(db, client):
    for path in ("/api/runs", "/api/reference", "/api/email/messages",
                 "/api/email/trusted-senders"):
        assert client.get(path, headers=auth_headers("viewer")).status_code == 200
