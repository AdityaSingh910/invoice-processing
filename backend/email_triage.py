"""Cheap deterministic triage of an incoming message (Phase G).

WHAT THIS IS FOR

Reading an invoice costs real money and real time: an LLM call, or page
rasterisation and a vision call. Most of what lands in a shared mailbox is not
an invoice at all. This module decides -- from headers and MIME structure
alone, before a single expensive operation runs -- whether a message is worth
spending that on.

**Nothing here calls a model, opens an attachment, rasterises a page, reads a
PDF, or performs cryptography.** It reads addresses, a subject line, and the
content types of the parts. That is the whole point: it is the stage that runs
first precisely because it is nearly free.

THE TWO AXES, AND WHY THEY ARE NOT ONE

    sender_type   CORPORATE / PERSONAL / UNKNOWN
    trust_status  TRUSTED / UNTRUSTED / UNKNOWN

These answer different questions and are never collapsed into a single score:

    invoice@acme-office.example   CORPORATE, and TRUSTED if procurement put
                                  that domain on the allowlist
    supplier@gmail.com            PERSONAL, and UNKNOWN trust -- which is not
                                  the same as untrusted
    billing@never-heard-of.test   CORPORATE (it is a company domain), trust
                                  UNKNOWN (nobody has vouched for it)

A corporate sender is **not** automatically trusted -- a company domain costs
an attacker nothing to register, and a spoofed `From` costs nothing at all.
An unknown sender is **not** automatically hostile -- every genuine vendor was
unknown once.

WHAT THIS MODULE MUST NOT DO

It must not delete anything, and it must not treat "cheap signals are weak" as
"this is not an invoice". A small supplier really does sometimes invoice from a
Gmail address with the subject "hi". So the low-relevance outcome is
*recorded and kept*, readable afterwards, and re-runnable by a human -- never
a silent drop. The claim this module makes is only ever "not worth an LLM call
without a person asking", never "not an invoice".

Trust itself is NOT decided here. `trust_status` is read from Phase F's
existing `trusted_email_senders` allowlist -- this module does not keep a
second copy of it -- and whether the sender is who they *claim* to be is
Phase F's cryptography, which runs after this stage on everything that
survives it.
"""
import json
import os
import re

import config

# Domains loaded from data/email_domain_policy.json, cached after first read.
# A module-level cache rather than a database table because this is read on
# every message and never written: it is policy, like config.FX_RATES, not
# state. `reload_domain_policy()` exists so a test (or an operator editing the
# file) is not stuck with a stale copy for the life of the process.
_POLICY = None


def reload_domain_policy():
    global _POLICY
    _POLICY = None
    return domain_policy()


def domain_policy() -> dict:
    """{'corporate': frozenset, 'personal': frozenset}, file plus environment.

    A missing or malformed file is not fatal: triage degrades to "every domain
    is UNKNOWN", which routes more messages into the full pipeline rather than
    fewer. That is the correct direction to fail -- the cost of a bad policy
    file should be a larger bill, never a lost invoice.
    """
    global _POLICY
    if _POLICY is not None:
        return _POLICY
    corporate, personal = set(), set()
    try:
        if os.path.isfile(config.EMAIL_DOMAIN_POLICY_SEED):
            with open(config.EMAIL_DOMAIN_POLICY_SEED, encoding="utf-8") as f:
                raw = json.load(f)
            corporate = {d.strip().lower().lstrip("@")
                         for d in (raw.get("corporate_domains") or []) if str(d).strip()}
            personal = {d.strip().lower().lstrip("@")
                        for d in (raw.get("personal_domains") or []) if str(d).strip()}
    except Exception:
        corporate, personal = set(), set()
    corporate |= set(config.email_corporate_domains())
    personal |= set(config.email_personal_domains())
    _POLICY = {"corporate": frozenset(corporate), "personal": frozenset(personal)}
    return _POLICY


def _domain_matches(domain: str, configured) -> bool:
    """True for the domain itself or any subdomain of it.

    `invoices.acme.example` matching a configured `acme.example` is what a
    vendor's own mail infrastructure normally looks like. Matching is on label
    boundaries, so `notacme.example` cannot match `acme.example` by being a
    suffix of the string.
    """
    domain = (domain or "").lower().strip().rstrip(".")
    if not domain:
        return False
    for entry in configured:
        if domain == entry or domain.endswith("." + entry):
            return True
    return False


def classify_sender(from_address: str, from_domain: str = None,
                    trusted_senders=None, reply_to: str = None) -> dict:
    """The two axes, decided from lookups only.

    `trusted_senders` is Phase F's allowlist rows, passed in rather than
    fetched here so this module has no database dependency and stays trivially
    testable -- and so there is exactly one allowlist in the system.
    """
    from email_security import domain_of, organizational_domain

    address = (from_address or "").strip().lower()
    domain = (from_domain or domain_of(address) or "").lower()
    policy = domain_policy()

    # ---- axis 1: what KIND of address is this
    if not domain:
        sender_type, type_reason = "UNKNOWN", "the message has no usable sender domain"
    elif _domain_matches(domain, policy["personal"]):
        sender_type, type_reason = "PERSONAL", (
            f"{domain} is a consumer/free-mail provider. That is an optimisation "
            f"signal only -- a small supplier may legitimately invoice from one.")
    elif _domain_matches(domain, policy["corporate"]):
        sender_type, type_reason = "CORPORATE", f"{domain} is a configured corporate domain"
    elif "." in domain:
        # An organisational domain that is neither free-mail nor one of ours is
        # still a company domain -- we simply do not know WHICH company. That
        # is CORPORATE with unknown trust, not UNKNOWN type.
        sender_type, type_reason = "CORPORATE", (
            f"{domain} is an organisational domain not on any configured list")
    else:
        sender_type, type_reason = "UNKNOWN", f"{domain!r} is not a usable domain"

    # ---- axis 2: have we decided to do business with them
    #
    # Read from Phase F's allowlist. Being on it is an AUTHORISATION statement
    # ("we buy from these people"), not an authentication one -- it says
    # nothing about whether this particular message really came from them,
    # which is what Phase F's signature checking is for.
    trust_status, trust_reason, vendor_name, matched_on = "UNKNOWN", None, None, None
    for entry in trusted_senders or []:
        sender = (entry.get("sender") or "").strip().lower()
        if not sender:
            continue
        status = (entry.get("status") or "trusted").lower()
        kind = (entry.get("kind") or ("address" if "@" in sender else "domain")).lower()
        hit = False
        if kind == "address" and address and address == sender:
            hit, matched_on = True, "address"
        elif kind == "domain" and domain:
            if _domain_matches(domain, {sender}):
                hit, matched_on = True, "domain"
            elif organizational_domain(domain) == organizational_domain(sender):
                hit, matched_on = True, "organizational_domain"
        if hit:
            vendor_name = entry.get("vendor_name")
            if status == "trusted":
                trust_status = "TRUSTED"
                trust_reason = (f"{sender} is on the trusted-sender list"
                                + (f" ({vendor_name})" if vendor_name else ""))
            else:
                # An entry explicitly marked something other than trusted is a
                # deliberate negative -- procurement said no. That is different
                # from never having been listed.
                trust_status = "UNTRUSTED"
                trust_reason = f"{sender} is listed with status {status!r}"
            break
    if trust_reason is None:
        trust_reason = "the sender is not on the trusted-sender list, which means unknown, not refused"

    # A Reply-To pointing somewhere other than the From domain is a normal
    # thing for mailing lists and ticketing systems, and also how a reply gets
    # redirected to an attacker. Recorded as a signal, never as a rejection.
    reply_to_domain = domain_of((reply_to or "").strip().lower())
    reply_to_mismatch = bool(reply_to_domain and domain and
                             organizational_domain(reply_to_domain) != organizational_domain(domain))

    return {
        "sender_type": sender_type,
        "sender_type_reason": type_reason,
        "trust_status": trust_status,
        "trust_reason": trust_reason,
        "vendor_name": vendor_name,
        "trust_matched_on": matched_on,
        "from_address": address or None,
        "from_domain": domain or None,
        "reply_to": (reply_to or "").strip().lower() or None,
        "reply_to_domain": reply_to_domain or None,
        "reply_to_mismatch": reply_to_mismatch,
    }


_PDF_NAME_RE = re.compile(r"\.pdf$", re.I)


def looks_like_invoice_subject(subject: str) -> bool:
    s = (subject or "").lower()
    return any(hint in s for hint in config.EMAIL_INVOICE_SUBJECT_HINTS)


def looks_like_invoice_attachment(attachment: dict) -> bool:
    """Filename or declared content type suggests a PDF.

    Both are attacker-controlled, and neither is trusted as proof -- the real
    check is the `%PDF-` magic-byte test in email_ingest.py, which runs later
    on the actual bytes. Here they are only used to decide whether opening the
    attachment at all is worth it.
    """
    name = (attachment.get("filename") or "")
    ctype = (attachment.get("content_type") or "").lower()
    if _PDF_NAME_RE.search(name):
        return True
    return ctype in ("application/pdf", "application/x-pdf")


def assess_relevance(sender: dict, subject: str, attachments) -> dict:
    """How likely is this to be a vendor invoice, from cheap signals only.

    Returns one of config.EMAIL_RELEVANCE plus the reasons behind it. HIGH and
    POSSIBLE proceed to Phase F verification and, if admitted, to extraction.
    LOW and IRRELEVANT stop here -- recorded, kept, and re-runnable by a human,
    never deleted.

    The asymmetry is deliberate. A false "irrelevant" costs a missed invoice
    that someone has to chase; a false "possible" costs one LLM call. So every
    doubt resolves upward, and the only messages that stop are the ones with
    nothing invoice-shaped about them at all.
    """
    attachments = list(attachments or [])
    pdf_like = [a for a in attachments if looks_like_invoice_attachment(a)]
    subject_hit = looks_like_invoice_subject(subject)
    trusted = sender.get("trust_status") == "TRUSTED"
    personal = sender.get("sender_type") == "PERSONAL"
    reasons = []

    if pdf_like:
        reasons.append(f"{len(pdf_like)} PDF attachment(s)")
    elif attachments:
        reasons.append(f"{len(attachments)} attachment(s), none of them a PDF")
    else:
        reasons.append("no attachments")
    if subject_hit:
        reasons.append("the subject reads like an invoice")
    if trusted:
        reasons.append("the sender is on the trusted-sender list")
    if personal:
        reasons.append("the sender is a consumer/free-mail address")

    # A PDF is the thing the extraction pipeline can actually read, so its
    # presence dominates. Without one there is nothing for the pipeline to do
    # even if the message IS about an invoice.
    if pdf_like:
        if trusted or subject_hit:
            relevance = "HIGH"
        elif personal:
            # Free-mail plus a PDF and no invoice-shaped subject: weak, but a
            # PDF is present, so this still goes through. POSSIBLE, not LOW.
            relevance = "POSSIBLE"
        else:
            relevance = "POSSIBLE"
    else:
        if subject_hit and trusted:
            # A trusted vendor writing about an invoice with nothing attached
            # is usually a query or a chase, not a new invoice -- but it is
            # worth a person seeing, so it is LOW rather than IRRELEVANT.
            relevance = "LOW"
            reasons.append("nothing to extract without an attachment")
        elif subject_hit:
            relevance = "LOW"
            reasons.append("nothing to extract without an attachment")
        else:
            relevance = "IRRELEVANT"
            reasons.append("no PDF and nothing invoice-shaped in the subject")

    return {
        "relevance": relevance,
        "reasons": reasons,
        "pdf_attachment_count": len(pdf_like),
        "attachment_count": len(attachments),
        "subject_matched": subject_hit,
        # The one line the rest of the system acts on. Everything above is
        # explanation for a human reading the record later.
        "proceed": relevance in ("HIGH", "POSSIBLE"),
    }


def triage(parsed: dict, trusted_senders=None) -> dict:
    """Both stages together, over a Phase F `parse_message()` result.

    Takes the parsed structure rather than raw bytes so the message is parsed
    ONCE per ingestion and the two stages cannot disagree about what it said.
    """
    sender = classify_sender(
        parsed.get("from_address"), parsed.get("from_domain"),
        trusted_senders=trusted_senders, reply_to=parsed.get("reply_to"))
    relevance = assess_relevance(sender, parsed.get("subject"),
                                 parsed.get("attachments"))
    return {"sender": sender, "relevance": relevance,
            "proceed": relevance["proceed"],
            "summary": (f"{sender['sender_type']}/{sender['trust_status']} sender, "
                        f"relevance {relevance['relevance']}")}
