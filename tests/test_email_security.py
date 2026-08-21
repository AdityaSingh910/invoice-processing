"""Phase F: email trusted-source verification.

THE CLAIM UNDER TEST

Nothing an incoming message says about itself is believed unless it was
either (a) stamped by a boundary this deployment named in advance, or
(b) proven cryptographically here. Everything else is recorded as evidence
and ignored. And the three outcomes -- verified, failed, and *could not
check* -- stay distinct all the way to the stored record, so an honest sender
whose infrastructure we cannot evaluate is never filed as hostile.

WHY THE DKIM TESTS GENERATE A REAL KEYPAIR

Because a DKIM test that mocks the verification proves nothing about the
verification. `_sign()` below performs a genuine RFC 6376 signing pass --
real canonicalisation, real body hash, a real RSA signature -- and the
verifier is given the matching public key through a `StaticDnsTxtResolver`,
which is how the whole path runs offline with no network and no `dkimpy`.
When these tests say a signature verified, an actual signature actually
verified.

Authorization claims are driven over HTTP, exactly as test_api_security.py
and test_documents.py do: calling a storage function directly proves nothing
about whether the endpoint in front of it is guarded.
"""
import base64
import hashlib
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

import config           # noqa: E402
import email_security as es   # noqa: E402
import email_signature as esig  # noqa: E402
import main             # noqa: E402
import ratelimit        # noqa: E402
import storage          # noqa: E402
import pg_schema        # noqa: E402
from conftest import auth_headers   # noqa: E402

from cryptography.hazmat.primitives import hashes                     # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa    # noqa: E402
from cryptography.hazmat.primitives.serialization import (            # noqa: E402
    Encoding, PublicFormat)


# --------------------------------------------------------------------------
# Fixtures and message-building helpers
# --------------------------------------------------------------------------
VENDOR_DOMAIN = "acme-office.example"
VENDOR_FROM = f"Billing <billing@{VENDOR_DOMAIN}>"
TRUSTED = [{"sender": VENDOR_DOMAIN, "kind": "domain",
            "vendor_name": "Acme Office Supplies", "status": "trusted"}]
BOUNDARY = "mx.buyer.example"       # our own receiving gateway, named in config


@pytest.fixture(scope="module")
def keypair():
    """One 2048-bit RSA key for the whole module -- generating one per test
    is seconds of CPU for no extra confidence."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return key, "v=DKIM1; k=rsa; p=" + base64.b64encode(der).decode()


def message(from_header=VENDOR_FROM, subject="Invoice INV-9001", extra_headers=(),
            body="Please find invoice INV-9001 attached.\r\n"):
    """A minimal, well-formed RFC 5322 message."""
    lines = [f"From: {from_header}",
             "To: ap@buyer.example",
             f"Subject: {subject}",
             "Date: Mon, 01 Sep 2025 10:00:00 +0000",
             f"Message-ID: <{abs(hash(subject)) % 10**10}@{VENDOR_DOMAIN}>"]
    lines.extend(extra_headers)
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


def with_pdf_attachment(from_header=VENDOR_FROM, filename="invoice.pdf",
                        content=b"%PDF-1.4 fake"):
    """A multipart message carrying a PDF, for the attachment-metadata tests."""
    b64 = base64.b64encode(content).decode()
    return (
        f"From: {from_header}\r\n"
        "To: ap@buyer.example\r\n"
        "Subject: Invoice INV-9001\r\n"
        "Date: Mon, 01 Sep 2025 10:00:00 +0000\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="SEP"\r\n'
        "\r\n"
        "--SEP\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "Invoice attached.\r\n"
        "--SEP\r\n"
        "Content-Type: application/pdf\r\n"
        f'Content-Disposition: attachment; filename="{filename}"\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{b64}\r\n"
        "--SEP--\r\n").encode()


def _sign(raw, private_key, domain=VENDOR_DOMAIN, selector="s1",
          headers=("from", "to", "subject", "date"), canon="relaxed/relaxed",
          break_signature=False, extra_tags=""):
    """A real RFC 6376 signing pass. See the module docstring.

    `extra_tags` goes into the signature header before `b=`, so it is covered
    by the signature like every other tag -- which is what makes an expiry
    test meaningful rather than a string the verifier could just ignore.
    """
    raw = es.normalise_eol(raw)
    header_block, body = es.split_raw(raw)
    fields = es.raw_header_fields(header_block)
    hc, _, bc = canon.partition("/")
    body_canon = (es._canon_body_relaxed if bc == "relaxed" else es._canon_body_simple)(body)
    bh = base64.b64encode(hashlib.sha256(body_canon).digest()).decode()
    sig_header = (f"DKIM-Signature: v=1; a=rsa-sha256; c={canon}; d={domain}; "
                  f"s={selector}; {extra_tags}h={':'.join(headers)}; bh={bh}; b=")
    canon_h = es._canon_header_relaxed if hc == "relaxed" else es._canon_header_simple
    signed = es._selected_headers(fields, list(headers), canon_h)
    signed += canon_h((sig_header + "\r\n").encode()).rstrip(b"\r\n")
    sig = private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    if break_signature:
        sig = b"\x00" * len(sig)
    return ((sig_header + base64.b64encode(sig).decode() + "\r\n").encode()
            + header_block + b"\r\n" + body)


def resolver_for(txt_record, domain=VENDOR_DOMAIN, selector="s1", extra=None):
    records = {f"{selector}._domainkey.{domain}": txt_record}
    records.update(extra or {})
    return es.StaticDnsTxtResolver(records)


def classify(raw, trusted_senders=TRUSTED, resolver=None, authserv_ids=()):
    """Always pass configuration explicitly, so a test never depends on what
    happens to be in the ambient environment."""
    return es.classify(raw, trusted_senders=trusted_senders,
                       resolver=resolver or es.NullDnsTxtResolver(),
                       trusted_authserv_ids=authserv_ids)


def ar(methods, authserv=BOUNDARY):
    """An Authentication-Results header, as a boundary would stamp it."""
    return f"Authentication-Results: {authserv}; {methods}"


def make_run(status="APPROVED"):
    """One committed run, so the email-to-run link has something real to point
    at. Built through storage directly rather than by driving the pipeline --
    what is under test here is the link, not extraction."""
    extracted = {"vendor_name": "Globex Logistics", "invoice_number": "INV-EMAIL-1",
                 "total": 1000.0, "subtotal": 1000.0, "tax": 0.0,
                 "po_references": ["PO-1002"], "currency": "USD",
                 "extraction_method": "groq (text)"}
    run_id, _, _ = storage.save_run_checked(
        "INV-EMAIL-1.pdf", status, extracted, {"matched": False}, [], [],
        tolerance_for=None, audit={})
    return run_id


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


def submit(client, raw, role="analyst", filename="message.eml"):
    return client.post("/api/email/messages",
                       files={"file": (filename, io.BytesIO(raw), "message/rfc822")},
                       headers=auth_headers(role))


# ==========================================================================
# 1. A valid, authenticated email
# ==========================================================================
def test_a_dkim_signed_aligned_message_from_a_trusted_sender_is_verified(keypair):
    key, txt = keypair
    record = classify(_sign(message(), key), resolver=resolver_for(txt))

    assert record["classification"] == "VERIFIED"
    assert record["status"] == "ADMITTED"
    assert record["dkim_result"] == "pass"
    assert record["dmarc_result"] == "pass"
    assert record["dmarc_aligned"] is True
    assert record["trusted_sender"] is True
    # The pass came from OUR cryptography, not from a header claiming it.
    assert record["audit"]["evaluated_mechanisms"]["dkim"]["source"] == "local_dkim_verification"


@pytest.mark.parametrize("canon", ["relaxed/relaxed", "simple/simple",
                                   "relaxed/simple", "simple/relaxed"])
def test_every_canonicalisation_combination_verifies(keypair, canon):
    """All four c= combinations, because getting canonicalisation subtly wrong
    is the classic way a hand-rolled verifier rejects perfectly valid mail --
    which would quarantine real vendors."""
    key, txt = keypair
    record = classify(_sign(message(), key, canon=canon), resolver=resolver_for(txt))
    assert record["dkim_result"] == "pass", f"{canon} should verify"


def test_a_trusted_boundary_reporting_dmarc_pass_is_believed():
    """No local signature at all, but our own named gateway evaluated it."""
    raw = message(extra_headers=[ar(f"spf=pass smtp.mailfrom={VENDOR_DOMAIN}; "
                                    f"dkim=pass header.d={VENDOR_DOMAIN}; "
                                    f"dmarc=pass header.from={VENDOR_DOMAIN}")])
    record = classify(raw, authserv_ids=(BOUNDARY,))
    assert record["classification"] == "VERIFIED"
    assert record["spf_result"] == "pass"
    assert record["audit"]["evaluated_mechanisms"]["spf"]["source"] == \
        "trusted_authentication_results"


def test_a_verified_message_records_attachment_metadata_but_not_content(keypair):
    key, txt = keypair
    record = classify(_sign(with_pdf_attachment(), key), resolver=resolver_for(txt))
    assert record["has_pdf_attachment"] is True
    assert record["attachment_count"] == 1
    attachment = record["attachments"][0]
    assert attachment["filename"] == "invoice.pdf"
    assert attachment["sha256"] == hashlib.sha256(b"%PDF-1.4 fake").hexdigest()
    # Metadata only. The bytes are not in the record anywhere.
    assert "content" not in attachment and "payload" not in attachment
    assert b"%PDF" not in repr(record).encode()


# ==========================================================================
# 2. Failed SPF / DKIM / DMARC
# ==========================================================================
def test_a_tampered_signature_fails(keypair):
    key, txt = keypair
    record = classify(_sign(message(), key, break_signature=True), resolver=resolver_for(txt))
    assert record["classification"] == "FAILED"
    assert record["dkim_result"] == "fail"
    assert "did not verify" in record["audit"]["evaluated_mechanisms"]["dkim"]["detail"]


def test_a_body_modified_after_signing_fails(keypair):
    """The body hash catches this without needing the public key at all --
    which is why it is checked first."""
    key, txt = keypair
    signed = _sign(message(), key)
    tampered = signed.replace(b"INV-9001 attached", b"INV-9001 attached, pay to new account")
    record = classify(tampered, resolver=resolver_for(txt))
    assert record["classification"] == "FAILED"
    assert "body hash" in record["audit"]["evaluated_mechanisms"]["dkim"]["detail"]


def test_a_revoked_signing_key_fails_rather_than_reading_as_unavailable(keypair):
    """An empty p= means revoked (RFC 6376 §3.6.1). That is a real negative,
    unlike a key we simply could not fetch."""
    key, _ = keypair
    record = classify(_sign(message(), key), resolver=resolver_for("v=DKIM1; k=rsa; p="))
    assert record["dkim_result"] == "fail"
    assert "revoked" in record["audit"]["evaluated_mechanisms"]["dkim"]["detail"]
    assert record["classification"] == "FAILED"


def test_a_trusted_boundary_reporting_spf_fail_fails():
    raw = message(extra_headers=[ar(f"spf=fail smtp.mailfrom={VENDOR_DOMAIN}")])
    record = classify(raw, authserv_ids=(BOUNDARY,))
    assert record["spf_result"] == "fail"
    assert record["classification"] == "FAILED"
    assert record["status"] == "QUARANTINED"


def test_a_trusted_boundary_reporting_dmarc_fail_fails():
    raw = message(extra_headers=[ar(f"spf=softfail smtp.mailfrom=elsewhere.example; "
                                    f"dmarc=fail header.from={VENDOR_DOMAIN}")])
    record = classify(raw, authserv_ids=(BOUNDARY,))
    assert record["classification"] == "FAILED"


def test_softfail_is_recorded_as_the_word_it_was_not_flattened_to_fail():
    """The normalised state drives the decision; the RFC 8601 word is kept
    beside it, because 'fail' and 'softfail' warrant different follow-up and
    an audit record that lost the distinction could not tell them apart."""
    raw = message(extra_headers=[ar("spf=softfail smtp.mailfrom=elsewhere.example")])
    record = classify(raw, authserv_ids=(BOUNDARY,))
    assert record["spf_result"] == "fail"        # the state
    assert record["spf_detail"] == "softfail"    # the actual word


def test_a_signature_past_its_own_expiry_fails(keypair):
    """x= is the signer's own bound on its own signature. Ignoring it would let
    a signature captured before a deliberate key rotation keep verifying
    forever, which is the replay x= exists to stop."""
    key, txt = keypair
    expired = _sign(message(), key, extra_tags="t=1600000000; x=1600003600; ")
    record = classify(expired, resolver=resolver_for(txt))
    assert record["dkim_result"] == "fail"
    assert "expiry" in record["audit"]["evaluated_mechanisms"]["dkim"]["detail"]
    assert record["classification"] == "FAILED"


def test_a_signature_inside_its_expiry_still_verifies(keypair):
    """The other half of the same claim: honouring x= must not break a valid,
    unexpired signature that happens to carry one."""
    key, txt = keypair
    import time as _time
    live = _sign(message(), key,
                 extra_tags=f"t={int(_time.time()) - 60}; x={int(_time.time()) + 3600}; ")
    record = classify(live, resolver=resolver_for(txt))
    assert record["dkim_result"] == "pass"
    assert record["classification"] == "VERIFIED"


def test_a_signature_expiring_no_later_than_it_was_made_is_malformed(keypair):
    key, txt = keypair
    bad = _sign(message(), key, extra_tags="t=1600003600; x=1600000000; ")
    record = classify(bad, resolver=resolver_for(txt))
    assert record["dkim_result"] == "fail"
    assert "no later than it was created" in \
        record["audit"]["evaluated_mechanisms"]["dkim"]["detail"]


def test_a_malformed_expiry_tag_is_unavailable_not_a_crash(keypair):
    key, txt = keypair
    bad = _sign(message(), key, extra_tags="x=not-a-timestamp; ")
    record = classify(bad, resolver=resolver_for(txt))
    assert record["dkim_result"] == "unavailable"
    assert record["classification"] != "VERIFIED"


def test_an_unsupported_weak_algorithm_is_not_accepted(keypair):
    """rsa-sha1 is forbidden by RFC 8301. It must not read as a pass."""
    key, txt = keypair
    signed = _sign(message(), key).replace(b"a=rsa-sha256", b"a=rsa-sha1", 1)
    record = classify(signed, resolver=resolver_for(txt))
    assert record["dkim_result"] != "pass"
    assert "unsupported algorithm" in record["audit"]["dkim_signatures"][0]["detail"]


# ==========================================================================
# 3. Missing authentication information
# ==========================================================================
def test_a_message_with_no_authentication_information_is_unverified_not_failed():
    """The central distinction of the whole phase: we could not check, which
    is NOT the same as it failed, and must never be recorded as if it were."""
    record = classify(message())
    assert record["classification"] == "UNVERIFIED"
    assert record["classification"] != "FAILED"
    assert record["status"] == "QUARANTINED"
    assert record["spf_result"] == "unavailable"
    assert record["dkim_result"] == "unavailable"
    assert record["dmarc_result"] == "unavailable"
    assert "not a failed check" in " ".join(record["reasons"])


def test_an_unverified_message_is_held_for_a_person_not_labelled_malicious():
    record = classify(message())
    text = " ".join(record["reasons"]).lower()
    for accusation in ("malicious", "hostile", "spoof", "attack", "fraud"):
        assert accusation not in text, \
            f"an unavailable check must not be described as {accusation!r}"


def test_spf_is_reported_unavailable_rather_than_guessed_from_received_headers():
    """SPF authorises a connecting IP. A stored message cannot establish one,
    and Received: headers are just more sender-chosen text."""
    raw = message(extra_headers=[
        "Received: from mail.acme-office.example (mail.acme-office.example [203.0.113.9]) "
        "by mx.buyer.example; Mon, 01 Sep 2025 10:00:01 +0000"])
    record = classify(raw)
    assert record["spf_result"] == "unavailable"
    assert "connecting IP" in record["audit"]["evaluated_mechanisms"]["spf"]["detail"]


def test_the_dmarc_policy_is_unavailable_not_absent_without_a_resolver():
    record = classify(message())
    assert record["audit"]["dmarc"]["policy"] is None
    assert record["audit"]["dmarc"]["policy_source"] == "unavailable"


def test_a_dmarc_policy_is_read_when_a_resolver_can_supply_one(keypair):
    key, txt = keypair
    resolver = resolver_for(txt, extra={f"_dmarc.{VENDOR_DOMAIN}": "v=DMARC1; p=reject; adkim=s"})
    record = classify(_sign(message(), key), resolver=resolver)
    assert record["audit"]["dmarc"]["policy"]["policy"] == "reject"
    assert record["audit"]["dmarc"]["alignment_mode"]["dkim"] == "strict"
    assert record["classification"] == "VERIFIED"      # exact domain match, so strict passes


def test_a_strict_policy_rejects_a_subdomain_that_relaxed_would_allow(keypair):
    """The published policy decides which alignment mode applies, and strict
    is an exact match with no public-suffix heuristic involved."""
    key, txt = keypair
    signed = _sign(message(from_header=f"Billing <billing@invoices.{VENDOR_DOMAIN}>"),
                   key, domain=VENDOR_DOMAIN)
    strict = resolver_for(txt, extra={
        f"_dmarc.invoices.{VENDOR_DOMAIN}": "v=DMARC1; p=reject; adkim=s"})
    relaxed = resolver_for(txt, extra={
        f"_dmarc.invoices.{VENDOR_DOMAIN}": "v=DMARC1; p=reject; adkim=r"})

    assert classify(signed, resolver=relaxed)["dmarc_result"] == "pass"
    assert classify(signed, resolver=strict)["dmarc_result"] == "fail"


# ==========================================================================
# 4. Spoofed From
# ==========================================================================
def test_a_valid_signature_for_another_domain_does_not_authenticate_this_from(keypair):
    """The case DMARC exists for. The signature is real and verifies -- it
    just belongs to somebody else."""
    key, txt = keypair
    signed = _sign(message(), key, domain="attacker.test")
    resolver = es.StaticDnsTxtResolver({"s1._domainkey.attacker.test": txt})
    record = classify(signed, resolver=resolver)

    assert record["dkim_result"] == "pass"          # the signature itself is fine
    assert record["dmarc_result"] == "fail"         # but it is not aligned
    assert record["classification"] == "FAILED"
    assert "not aligned" in " ".join(record["reasons"])


def test_a_lookalike_domain_does_not_align_with_the_real_one(keypair):
    key, txt = keypair
    lookalike = "acme-office.example.attacker.test"
    signed = _sign(message(from_header=f"Billing <billing@{lookalike}>"), key,
                   domain=lookalike)
    resolver = es.StaticDnsTxtResolver({f"s1._domainkey.{lookalike}": txt})
    record = classify(signed, resolver=resolver)
    # It authenticates as itself, and is not on the allowlist -- which is the
    # point: authentication is not authorisation.
    assert record["dkim_result"] == "pass"
    assert record["trusted_sender"] is False
    assert record["classification"] == "SUSPICIOUS"


def test_two_from_headers_are_refused():
    """Legal in no reading of RFC 5322, and clients disagree about which they
    display -- which is the entire trick."""
    raw = (b"From: Billing <billing@" + VENDOR_DOMAIN.encode() + b">\r\n"
           b"From: Billing <billing@attacker.test>\r\n"
           b"To: ap@buyer.example\r\nSubject: Invoice\r\n\r\nbody\r\n")
    record = classify(raw)
    assert record["classification"] == "FAILED"
    assert record["from_header_count"] == 2
    assert any("From headers" in r for r in record["reasons"])


def test_a_from_header_listing_two_addresses_is_refused():
    raw = message(from_header=f"billing@{VENDOR_DOMAIN}, billing@attacker.test")
    record = classify(raw)
    assert record["classification"] == "FAILED"
    assert any("more than one address" in r for r in record["reasons"])


def test_a_message_with_no_from_header_is_refused():
    raw = b"To: ap@buyer.example\r\nSubject: Invoice\r\n\r\nbody\r\n"
    record = classify(raw)
    assert record["classification"] == "FAILED"


def test_a_self_inserted_authentication_results_header_is_discarded():
    """THE core anti-spoofing test. A sender can put anything in this header;
    it is only worth reading when our own boundary stamped it."""
    raw = message(extra_headers=[ar(f"spf=pass smtp.mailfrom={VENDOR_DOMAIN}; "
                                    f"dmarc=pass header.from={VENDOR_DOMAIN}",
                                    authserv="attacker-controlled.test")])
    record = classify(raw, authserv_ids=(BOUNDARY,))

    assert record["classification"] == "UNVERIFIED", \
        "a forged Authentication-Results header must not produce a verified message"
    assert record["dmarc_result"] == "unavailable"
    # Kept as evidence: an auditor should be able to see that it was tried.
    discarded = record["audit"]["evidence"]["discarded_authentication_results"]
    assert len(discarded) == 1
    assert discarded[0]["authserv_id"] == "attacker-controlled.test"


def test_an_authentication_results_header_is_ignored_when_nothing_is_trusted():
    """The default configuration trusts no boundary, so even a header naming
    a real gateway counts for nothing."""
    raw = message(extra_headers=[ar(f"dmarc=pass header.from={VENDOR_DOMAIN}")])
    record = classify(raw, authserv_ids=())
    assert record["classification"] == "UNVERIFIED"
    assert record["audit"]["evidence"]["trusted_authserv_ids"] == []


def test_a_received_spf_header_is_never_believed():
    """It carries no authserv-id, so it cannot be attributed to our boundary
    rather than to the sender."""
    raw = message(extra_headers=[f"Received-SPF: pass (acme-office.example: sender is "
                                 f"authorized) client-ip=203.0.113.9"])
    record = classify(raw, authserv_ids=(BOUNDARY,))
    assert record["spf_result"] == "unavailable"
    assert record["classification"] == "UNVERIFIED"
    assert record["audit"]["evidence"]["received_spf_headers_ignored"]


def test_an_authenticated_sender_not_on_the_allowlist_is_suspicious(keypair):
    """Authentication is not authorisation. Proving who sent it does not make
    them someone this business buys from."""
    key, txt = keypair
    other = "unknown-vendor.example"
    signed = _sign(message(from_header=f"Billing <billing@{other}>"), key, domain=other)
    resolver = es.StaticDnsTxtResolver({f"s1._domainkey.{other}": txt})
    record = classify(signed, resolver=resolver)
    assert record["dmarc_result"] == "pass"
    assert record["classification"] == "SUSPICIOUS"
    assert record["status"] == "QUARANTINED"


def test_being_on_the_allowlist_does_not_by_itself_authenticate_anyone():
    """An allowlisted domain in From costs a spoofer nothing."""
    record = classify(message())        # From is the allowlisted vendor, unsigned
    assert record["trusted_sender"] is False or record["classification"] != "VERIFIED"
    assert record["classification"] == "UNVERIFIED"


# ==========================================================================
# 5. Conflicting authentication signals
# ==========================================================================
def test_a_boundary_claiming_dkim_pass_over_a_signature_that_does_not_verify(keypair):
    """Our own arithmetic outranks a relayed claim about it, and the
    disagreement is recorded rather than resolved by picking the friendlier
    answer."""
    key, txt = keypair
    signed = _sign(message(), key, break_signature=True)
    raw = signed.replace(b"To: ap@buyer.example",
                         ar(f"dkim=pass header.d={VENDOR_DOMAIN}").encode()
                         + b"\r\nTo: ap@buyer.example", 1)
    record = classify(raw, resolver=resolver_for(txt), authserv_ids=(BOUNDARY,))

    assert record["dkim_result"] == "fail"
    assert record["classification"] == "FAILED"
    assert record["audit"]["conflicts"], "the disagreement must be recorded"
    assert any("verif" in c for c in record["audit"]["conflicts"])


def test_two_trusted_boundaries_disagreeing_about_dmarc_is_suspicious():
    raw = message(extra_headers=[
        ar(f"dmarc=pass header.from={VENDOR_DOMAIN}"),
        ar(f"dmarc=fail header.from={VENDOR_DOMAIN}", authserv="mx2.buyer.example")])
    record = classify(raw, authserv_ids=(BOUNDARY, "mx2.buyer.example"))
    assert record["classification"] == "SUSPICIOUS"
    assert any("disagree" in c for c in record["audit"]["conflicts"])


def test_a_boundary_claiming_dmarc_pass_against_local_alignment_saying_otherwise(keypair):
    """A real signature from another domain, plus a trusted header asserting
    the message is aligned. The locally computed alignment wins."""
    key, txt = keypair
    signed = _sign(message(), key, domain="attacker.test")
    raw = signed.replace(b"To: ap@buyer.example",
                         ar(f"dmarc=pass header.from={VENDOR_DOMAIN}").encode()
                         + b"\r\nTo: ap@buyer.example", 1)
    resolver = es.StaticDnsTxtResolver({"s1._domainkey.attacker.test": txt})
    record = classify(raw, resolver=resolver, authserv_ids=(BOUNDARY,))

    assert record["classification"] == "FAILED"
    assert record["audit"]["conflicts"]


# ==========================================================================
# 6. Unavailable verification
# ==========================================================================
def test_a_signature_with_no_reachable_key_is_unavailable_never_failed(keypair):
    """An honest vendor whose key we cannot fetch -- because DNS is off, or
    down -- must not be filed as an authentication failure."""
    key, _ = keypair
    record = classify(_sign(message(), key), resolver=es.NullDnsTxtResolver())
    assert record["dkim_result"] == "unavailable"
    assert record["classification"] == "UNVERIFIED"
    assert record["classification"] != "FAILED"


def test_the_null_resolver_is_named_in_the_evidence():
    """Whoever reads a stored record later has to be able to tell what the
    verification was actually capable of at the time."""
    record = classify(message())
    assert record["audit"]["evidence"]["dns_resolver"] == "NullDnsTxtResolver"


def test_the_stored_record_states_its_own_limitations():
    record = classify(message())
    limitations = " ".join(record["audit"]["limitations"]).lower()
    assert "not that the invoice inside it is legitimate" in limitations
    assert "spf is never computed locally" in limitations


def test_the_dnspython_resolver_refuses_clearly_when_the_package_is_absent():
    """Same contract as S3DocumentStore with boto3 missing: a clear failure at
    construction, not an obscure one later."""
    try:
        import dns.resolver   # noqa: F401
        pytest.skip("dnspython is installed, so the missing-package path cannot be exercised")
    except ImportError:
        pass
    with pytest.raises(RuntimeError) as exc:
        es.DnspythonTxtResolver()
    assert "dnspython" in str(exc.value)


# ==========================================================================
# 7. Digital signatures -- present, and deliberately not verified
# ==========================================================================
SMIME = (b"From: Billing <billing@" + VENDOR_DOMAIN.encode() + b">\r\n"
         b"To: ap@buyer.example\r\nSubject: Invoice INV-9001\r\n"
         b"MIME-Version: 1.0\r\n"
         b'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; '
         b'micalg=sha-256; boundary="SIG"\r\n\r\n'
         b"--SIG\r\nContent-Type: text/plain\r\n\r\nInvoice attached.\r\n"
         b"--SIG\r\nContent-Type: application/pkcs7-signature; name=\"smime.p7s\"\r\n\r\n"
         b"MIIFAKESIGNATUREBYTES\r\n--SIG--\r\n")

PGP = (b"From: Billing <billing@" + VENDOR_DOMAIN.encode() + b">\r\n"
       b"To: ap@buyer.example\r\nSubject: Invoice INV-9001\r\n"
       b"MIME-Version: 1.0\r\n"
       b'Content-Type: multipart/signed; protocol="application/pgp-signature"; '
       b'micalg=pgp-sha256; boundary="SIG"\r\n\r\n'
       b"--SIG\r\nContent-Type: text/plain\r\n\r\nInvoice attached.\r\n"
       b"--SIG\r\nContent-Type: application/pgp-signature\r\n\r\n"
       b"-----BEGIN PGP SIGNATURE-----\r\n-----END PGP SIGNATURE-----\r\n--SIG--\r\n")


def test_an_smime_signature_is_detected_and_reported_unavailable():
    record = classify(SMIME)
    assert record["signature_kind"] == "smime"
    assert record["signature_result"] == "unavailable"
    assert record["audit"]["digital_signature"]["verified"] is False
    assert "trust anchor" in record["audit"]["digital_signature"]["detail"]


def test_a_pgp_signature_is_detected_and_reported_unavailable():
    record = classify(PGP)
    assert record["signature_kind"] == "pgp"
    assert record["signature_result"] == "unavailable"
    assert record["audit"]["digital_signature"]["verified"] is False


def test_an_unsigned_message_reports_not_present_not_unavailable():
    """'Nobody signed this' and 'somebody signed this and we could not check'
    are different facts, and a reviewer needs to be told which."""
    record = classify(message())
    assert record["signature_kind"] == "none"
    assert record["signature_result"] == "not_present"


def test_a_dkim_pass_never_becomes_a_user_level_signature_pass(keypair):
    """The distinction the whole email_signature module exists to protect: a
    domain asserting it relayed a message is not a person asserting they
    wrote one."""
    key, txt = keypair
    record = classify(_sign(message(), key), resolver=resolver_for(txt))
    assert record["dkim_result"] == "pass"
    assert record["signature_result"] == "not_present"
    assert record["signature_kind"] == "none"


def test_the_unavailable_verifier_has_no_path_that_returns_a_pass():
    """Asserted against every message shape in this file at once, because a
    stub that could be coaxed into reporting success would be worse than no
    stub at all."""
    verifier = esig.UnavailableSignatureVerifier()
    import email
    import email.policy
    for raw in (SMIME, PGP, message(), with_pdf_attachment(), b"", b"not an email at all"):
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            msg = None
        assert verifier.verify(msg)["state"] != esig.STATE_PASS
        assert verifier.verify(msg)["verified"] is False
    assert verifier.verify(None)["state"] != esig.STATE_PASS


def test_asking_for_an_unimplemented_verifier_raises_rather_than_downgrading(monkeypatch):
    """A deployment that asked for real verification and silently got
    detection instead would be exactly the false assurance to avoid."""
    monkeypatch.setenv("EMAIL_SIGNATURE_VERIFIER", "smime")
    with pytest.raises(RuntimeError) as exc:
        esig.get_verifier()
    assert "not implemented" in str(exc.value)


def test_signature_detection_survives_a_message_it_cannot_parse():
    assert esig.detect(None)["kind"] == "none"


# ==========================================================================
# 8. Malformed and malicious headers
# ==========================================================================
MALFORMED = {
    "empty": b"",
    "binary-garbage": b"\x00\x01\x02\x03 not an email",
    "no-blank-line": b"From: billing@acme-office.example",
    "empty-header-values": b"From: \r\nTo: \r\n\r\n",
    "header-line-without-a-colon":
        b"NoColonHeaderLine\r\nFrom: billing@acme-office.example\r\n\r\nbody",
    "broken-encoded-word":
        b"From: =?utf-8?q?=FF=FE broken encoded word?= <a@b.example>\r\n\r\nbody",
    "non-ascii-display-name":
        "From: \u00dcnicode <billing@acme-office.example>\r\n\r\nbody".encode("utf-8"),
    "enormous-header-value":
        b"From: billing@acme-office.example\r\nSubject: " + b"a" * 100000 + b"\r\n\r\nbody",
    "repeated-mime-preamble":
        b"Content-Type: multipart/mixed; boundary=X\r\n\r\n--X\r\n" * 50,
    "null-byte-in-the-from-address":
        b"From: billing@acme\x00-office.example\r\nSubject: Invoice\r\n\r\nbody",
    "lone-lf-line-endings":
        b"From: billing@acme-office.example\nSubject: Invoice\n\nbody\n",
}


@pytest.mark.parametrize("raw", list(MALFORMED.values()), ids=list(MALFORMED))
def test_a_malformed_message_produces_a_verdict_not_a_crash(raw):
    """Every one of these is something an attacker can send. None of them may
    raise: a crash is a worse outcome than a quarantine, and a 500 tells the
    sender they found something."""
    record = classify(raw)
    assert record["classification"] in config.EMAIL_CLASSIFICATIONS
    assert record["status"] in config.EMAIL_STATUSES
    assert record["classification"] != "VERIFIED", \
        "a message this malformed must never come out verified"


def test_a_subject_with_embedded_newlines_is_flattened_and_flagged():
    """Header injection: CRLF in a value is how an attacker forges additional
    headers downstream."""
    raw = b"From: billing@acme-office.example\r\nSubject: Invoice\r\n \r\n\r\nbody\r\n"
    record = classify(raw)
    assert record["classification"] in config.EMAIL_CLASSIFICATIONS
    if record["subject"]:
        assert "\n" not in record["subject"] and "\r" not in record["subject"]


def test_an_attachment_filename_cannot_carry_a_path():
    """The same treatment main.py already gives an uploaded filename -- this
    string reaches the database and the screen."""
    raw = with_pdf_attachment(filename="../../../../etc/passwd.pdf")
    record = classify(raw)
    name = record["attachments"][0]["filename"]
    assert "/" not in name and "\\" not in name and ".." not in name


def test_an_implausibly_large_header_block_is_not_evaluated_for_dkim():
    raw = (b"DKIM-Signature: v=1; a=rsa-sha256; d=x.example; s=s1; h=from; bh=x; b=x\r\n"
           b"X-Filler: " + b"a" * (600 * 1024) + b"\r\nFrom: a@b.example\r\n\r\nbody")
    record = classify(raw)
    assert record["classification"] in config.EMAIL_CLASSIFICATIONS
    assert record["dkim_result"] != "pass"


def test_a_message_with_many_mime_parts_is_bounded():
    parts = "".join(f"--X\r\nContent-Type: text/plain\r\n\r\npart {i}\r\n" for i in range(500))
    raw = (f"From: billing@{VENDOR_DOMAIN}\r\nMIME-Version: 1.0\r\n"
           f'Content-Type: multipart/mixed; boundary="X"\r\n\r\n{parts}--X--\r\n').encode()
    record = classify(raw)
    assert record["classification"] in config.EMAIL_CLASSIFICATIONS


def test_a_dkim_signature_header_that_is_pure_garbage_does_not_pass():
    raw = message(extra_headers=["DKIM-Signature: this is not a tag list at all"])
    record = classify(raw)
    assert record["dkim_result"] != "pass"
    assert record["classification"] != "VERIFIED"


# ==========================================================================
# Organizational-domain / alignment unit behaviour
# ==========================================================================
@pytest.mark.parametrize("domain,expected", [
    ("acme-office.example", "acme-office.example"),
    ("invoices.acme-office.example", "acme-office.example"),
    ("a.b.c.acme-office.example", "acme-office.example"),
    ("acme.co.uk", "acme.co.uk"),
    ("mail.acme.co.uk", "acme.co.uk"),
    ("acme.com.au", "acme.com.au"),
    ("example", "example"),
    ("", ""),
])
def test_organizational_domain(domain, expected):
    assert es.organizational_domain(domain) == expected


def test_alignment_distinguishes_strict_from_relaxed():
    result = es.alignment("invoices.acme.example", "acme.example")
    assert result["relaxed"] is True
    assert result["strict"] is False


# ==========================================================================
# 9. Persistence, the quarantine gate, and its concurrency
# ==========================================================================
def test_a_submitted_message_is_stored_with_its_evidence_but_not_its_body(db, client):
    body = "SENSITIVE INVOICE NARRATIVE THAT MUST NOT BE PERSISTED"
    raw = message(body=body + "\r\n")
    response = submit(client, raw)
    assert response.status_code == 200
    email_id = response.json()["message"]["id"]

    stored = storage.get_email_message(email_id)
    assert stored["classification"] == "UNVERIFIED"
    assert stored["status"] == "QUARANTINED"
    assert stored["sha256"] == hashlib.sha256(es.normalise_eol(raw)).hexdigest()
    # The evidence is there; the message is not.
    assert stored["audit"]["evidence"]["trusted_authserv_ids"] == []
    assert body not in str(stored)


def test_arrival_and_evaluation_are_separate_events_in_the_history(db, client):
    email_id = submit(client, message()).json()["message"]["id"]
    events = [e["event_type"] for e in storage.list_email_activity(email_id)]
    assert events == ["MESSAGE_RECEIVED", "AUTHENTICATION_EVALUATED", "QUARANTINED"]


def test_a_byte_identical_resubmission_returns_the_existing_record(db, client):
    raw = message()
    first = submit(client, raw).json()
    second = submit(client, raw).json()
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["message"]["id"] == first["message"]["id"]
    assert len(storage.list_email_messages()) == 1
    # The replay is visible in the history rather than hidden.
    events = [e["event_type"] for e in storage.list_email_activity(first["message"]["id"])]
    assert "MESSAGE_RESUBMITTED" in events


def test_a_quarantined_message_can_be_released_by_a_reviewer(db, client):
    email_id = submit(client, message()).json()["message"]["id"]
    response = client.post(f"/api/email/messages/{email_id}/release",
                           json={"note": "confirmed with the vendor by phone"},
                           headers=auth_headers("reviewer"))
    assert response.status_code == 200
    stored = storage.get_email_message(email_id)
    assert stored["status"] == "RELEASED"
    assert stored["release_note"] == "confirmed with the vendor by phone"
    # The classification is a finding and never moves.
    assert stored["classification"] == "UNVERIFIED"


def test_a_message_may_be_ruled_on_only_once(db, client):
    email_id = submit(client, message()).json()["message"]["id"]
    assert client.post(f"/api/email/messages/{email_id}/release",
                       headers=auth_headers("reviewer")).status_code == 200
    second = client.post(f"/api/email/messages/{email_id}/discard",
                         headers=auth_headers("reviewer"))
    assert second.status_code == 409
    assert "already been ruled on" in str(second.json()["detail"])


def test_releasing_an_admitted_message_is_refused(db, client, keypair, monkeypatch):
    key, txt = keypair
    monkeypatch.setattr(es, "resolver_from_config", lambda: resolver_for(txt))
    email_id = submit(client, _sign(message(), key)).json()["message"]["id"]
    assert storage.get_email_message(email_id)["status"] == "ADMITTED"
    response = client.post(f"/api/email/messages/{email_id}/release",
                           headers=auth_headers("reviewer"))
    assert response.status_code == 409
    assert "not quarantined" in str(response.json()["detail"])


def test_release_and_discard_on_an_unknown_message_are_404(db, client):
    for action in ("release", "discard"):
        response = client.post(f"/api/email/messages/999999/{action}",
                               headers=auth_headers("reviewer"))
        assert response.status_code == 404


def test_concurrent_rulings_on_one_message_produce_exactly_one_winner(db, client):
    """Real threads, same barrier pattern as the Phase D/E races -- a
    double-clicked Release, a retry, or one reviewer releasing while another
    discards must not both apply."""
    email_id = submit(client, message()).json()["message"]["id"]
    n = 10
    results, lock, barrier = [], threading.Lock(), threading.Barrier(n)

    def worker(i):
        decision = "RELEASED" if i % 2 == 0 else "DISCARDED"
        barrier.wait()
        r = storage.set_email_status(email_id, decision, actor=f"employee-{i}")
        with lock:
            results.append((decision, r))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [(d, r) for d, r in results if r["ok"]]
    assert len(winners) == 1, f"expected exactly one ruling to land, got {len(winners)}"
    stored = storage.get_email_message(email_id)
    assert stored["status"] == winners[0][0]
    # And the history carries exactly that one ruling, not several.
    rulings = [e for e in storage.list_email_activity(email_id)
               if e["event_type"] in ("RELEASED", "DISCARDED")]
    assert len(rulings) == 1


def test_a_quarantined_message_cannot_acquire_a_run(db, client):
    """The gate this phase exists to impose: a held message must not be able
    to reach the pipeline by the back door."""
    email_id = submit(client, message()).json()["message"]["id"]
    result = storage.link_email_to_run(email_id, 1)
    assert result["ok"] is False
    assert "may not be processed" in result["error"]


def test_a_released_message_can_be_linked_to_the_run_it_produces(db, client):
    """Phase F never calls this -- creating the run is Phase G -- but the join
    it enables is why the column exists now."""
    email_id = submit(client, message()).json()["message"]["id"]
    storage.set_email_status(email_id, "RELEASED", actor="reviewer")
    run_id = make_run(status="APPROVED")
    result = storage.link_email_to_run(email_id, run_id)
    assert result["ok"] is True
    assert storage.get_email_message(email_id)["run_id"] == run_id
    events = [e["event_type"] for e in storage.list_email_activity(email_id)]
    assert "LINKED_TO_RUN" in events


def test_linking_to_an_unknown_run_is_refused(db, client):
    email_id = submit(client, message()).json()["message"]["id"]
    storage.set_email_status(email_id, "RELEASED", actor="reviewer")
    assert storage.link_email_to_run(email_id, 999999)["error"] == "unknown run"


def test_messages_can_be_filtered_by_status(db, client):
    submit(client, message(subject="one"))
    second = submit(client, message(subject="two")).json()["message"]["id"]
    storage.set_email_status(second, "RELEASED", actor="reviewer")

    quarantined = client.get("/api/email/messages?status_filter=QUARANTINED",
                             headers=auth_headers("viewer")).json()
    released = client.get("/api/email/messages?status_filter=RELEASED",
                          headers=auth_headers("viewer")).json()
    assert [m["id"] for m in quarantined] != []
    assert [m["id"] for m in released] == [second]


def test_an_unknown_status_filter_is_refused(db, client):
    response = client.get("/api/email/messages?status_filter=NONSENSE",
                          headers=auth_headers("viewer"))
    assert response.status_code == 400


def test_the_list_view_omits_the_full_evidence_but_the_detail_view_carries_it(db, client):
    email_id = submit(client, message()).json()["message"]["id"]
    listed = client.get("/api/email/messages", headers=auth_headers("viewer")).json()[0]
    assert "audit" not in listed
    detail = client.get(f"/api/email/messages/{email_id}",
                        headers=auth_headers("viewer")).json()
    assert detail["audit"]["evaluated_mechanisms"]["dkim"]["state"] == "unavailable"
    assert detail["activity"]


def test_an_unknown_message_id_is_404(db, client):
    assert client.get("/api/email/messages/999999",
                      headers=auth_headers("viewer")).status_code == 404


def test_the_trusted_sender_list_and_verification_setup_are_readable(db, client):
    body = client.get("/api/email/trusted-senders", headers=auth_headers("viewer")).json()
    assert any(s["sender"] == VENDOR_DOMAIN for s in body["senders"])
    assert body["verification"]["dns_resolver"] in ("none", "dnspython")
    assert set(body["verification"]["classifications"]) == set(config.EMAIL_CLASSIFICATIONS)


def test_the_trusted_sender_list_is_reloaded_from_json_and_has_no_writer(db, client):
    """Reference data, like purchase orders and vendors -- editable by a file
    under review, not by anyone holding a token."""
    assert storage.list_trusted_senders()
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/api/email/trusted-senders",
                                           headers=auth_headers("admin"))
        assert response.status_code in (404, 405)


# ==========================================================================
# 10. Authorization and security boundaries
# ==========================================================================
@pytest.mark.parametrize("method,path", [
    ("post", "/api/email/messages"),
    ("get", "/api/email/messages"),
    ("get", "/api/email/messages/1"),
    ("post", "/api/email/messages/1/release"),
    ("post", "/api/email/messages/1/discard"),
    ("get", "/api/email/trusted-senders"),
])
def test_every_email_endpoint_refuses_an_unauthenticated_caller(db, client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code in (401, 403), f"{method.upper()} {path} was not guarded"


def test_a_viewer_cannot_submit_a_message(db, client):
    """Submitting is ingestion, and carries the same scope as processing an
    invoice."""
    assert submit(client, message(), role="viewer").status_code == 403


def test_an_analyst_cannot_release_a_quarantined_message(db, client):
    """Releasing is a review ruling -- the same authority that accepts a
    NEEDS_REVIEW invoice, not the one that uploads."""
    email_id = submit(client, message()).json()["message"]["id"]
    assert client.post(f"/api/email/messages/{email_id}/release",
                       headers=auth_headers("analyst")).status_code == 403
    assert client.post(f"/api/email/messages/{email_id}/discard",
                       headers=auth_headers("analyst")).status_code == 403
    assert storage.get_email_message(email_id)["status"] == "QUARANTINED"


def test_a_viewer_may_read_a_message_record(db, client):
    """A security record is invoice data, permissioned like the rest of it --
    the same call documents.py's endpoints already make."""
    email_id = submit(client, message()).json()["message"]["id"]
    assert client.get(f"/api/email/messages/{email_id}",
                      headers=auth_headers("viewer")).status_code == 200


def test_a_forged_token_cannot_reach_a_message(db, client, monkeypatch):
    import auth
    forged = auth.jwt.encode({"sub": "attacker", "scope": "invoice:read invoice:review"},
                             "not-the-signing-key", algorithm="HS256")
    response = client.get("/api/email/messages", headers={"Authorization": "Bearer " + forged})
    assert response.status_code == 401


def test_the_submitting_user_is_recorded_as_the_actor_not_a_client_supplied_name(db, client):
    email_id = submit(client, message(), role="analyst").json()["message"]["id"]
    stored = storage.get_email_message(email_id)
    assert stored["submitted_by"] == "test-analyst"


def test_an_oversized_message_is_refused(db, client, monkeypatch):
    monkeypatch.setenv("EMAIL_MAX_MESSAGE_BYTES", "2048")
    response = submit(client, message(body="x" * 5000))
    assert response.status_code == 413


def test_an_empty_submission_is_refused(db, client):
    assert submit(client, b"").status_code == 400


def test_submitting_is_rate_limited(db, client, monkeypatch):
    """The ingestion door carries the same per-user limit invoice processing
    does, so it cannot be used to bypass it."""
    monkeypatch.setattr(config, "RATE_LIMIT_PROCESS_PER_MINUTE", 3)
    ratelimit.limiter.reset()
    codes = [submit(client, message(subject=f"invoice {i}")).status_code for i in range(6)]
    assert 429 in codes


def test_no_email_endpoint_response_leaks_a_secret(db, client, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "super-secret-signing-key-value")
    email_id = submit(client, message()).json()["message"]["id"]
    for path in ("/api/email/messages", f"/api/email/messages/{email_id}",
                 "/api/email/trusted-senders"):
        body = client.get(path, headers=auth_headers("admin")).text
        assert "super-secret-signing-key-value" not in body


# ==========================================================================
# 11. Backwards compatibility -- nothing that worked before may have changed
# ==========================================================================
def test_a_manual_pdf_upload_still_processes_end_to_end(db, client):
    """The guarantee that matters most: Phase F added a second, independent
    door. The original one is untouched, and an invoice uploaded through it
    never acquires an email verdict or an email requirement."""
    with open(os.path.join(SAMPLES, "01_happy_path_acme.pdf"), "rb") as f:
        pdf = f.read()
    response = client.post("/api/runs/stream",
                           files={"file": ("01_happy_path_acme.pdf", io.BytesIO(pdf),
                                           "application/pdf")},
                           headers=auth_headers("analyst"))
    assert response.status_code == 200
    assert "final" in response.text

    runs = storage.list_runs()
    assert len(runs) == 1
    assert runs[0]["status"] in ("APPROVED", "NEEDS_REVIEW", "REJECTED")
    # No email record was invented for a manual upload.
    assert storage.list_email_messages() == []


def test_an_existing_run_is_unaffected_by_the_new_column(db, client):
    run_id = make_run(status="NEEDS_REVIEW")
    run = storage.get_run(run_id)
    assert run["status"] == "NEEDS_REVIEW"
    # The runs table gained nothing in this phase; the link lives on the
    # email side, so a run has no idea email exists.
    assert "email_id" not in run and "email_message_id" not in run


def test_the_email_endpoints_did_not_displace_any_existing_route(db, client):
    for path in ("/api/runs", "/api/reference", "/api/sample-invoices"):
        assert client.get(path, headers=auth_headers("viewer")).status_code == 200


def test_reset_demo_clears_runs_and_keeps_the_security_record(db, client):
    """A security finding about a sender stays true whether or not the
    invoice it carried is still on file."""
    email_id = submit(client, message()).json()["message"]["id"]
    storage.set_email_status(email_id, "RELEASED", actor="reviewer")
    run_id = make_run(status="APPROVED")
    storage.link_email_to_run(email_id, run_id)

    response = client.post("/api/admin/reset-demo", headers=auth_headers("admin"))
    assert response.status_code == 200
    assert storage.list_runs() == []

    survivor = storage.get_email_message(email_id)
    assert survivor is not None, "the security record must survive a run-history reset"
    assert survivor["run_id"] is None, "but its link to the deleted run must be dropped"


def test_classification_is_deterministic(keypair):
    """The same bytes and the same configuration always give the same answer.
    This is what lets a resubmission return the stored record instead of
    re-deciding, and what lets an auditor re-derive a verdict later."""
    key, txt = keypair
    raw = _sign(message(), key)
    first = classify(raw, resolver=resolver_for(txt))
    second = classify(raw, resolver=resolver_for(txt))
    assert first["classification"] == second["classification"]
    assert first["reasons"] == second["reasons"]
    assert first["audit"]["evaluated_mechanisms"] == second["audit"]["evaluated_mechanisms"]
