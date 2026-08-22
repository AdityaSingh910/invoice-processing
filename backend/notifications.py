"""Rejection-notification service: compose, preview, send, and account for an
email telling a vendor why their invoice was rejected.

WHERE THIS SITS

    invoice rejection -> notifications.py -> email_outbound.py -> Gmail

This module holds every decision specific to a REJECTION NOTICE: which run is
eligible, who the default recipient is, what the email says, whether one was
already sent, and what the audit trail records about the attempt. It knows
nothing about how a message is actually delivered -- that is
`email_outbound.py` -- and nothing about mailbox credentials -- that is
`oauth_google.py`. Each layer is testable without the ones below it.

REUSED, NOT REINVENTED

The rejection reasons a vendor reads are `portal.client_state()`'s
`detail_lines` -- the exact sentences the supplier portal already shows a
client about their own declined invoice (§7g.6). That table is frozen, keyed
by rule name, hand-written for an external reader, and already excludes every
internal detail (run ids, reviewer names, PO balances, extraction routes) a
vendor has no business seeing. Building a second rejection-reason vocabulary
for this feature would risk exactly the leak Phase J's design spent its effort
preventing; reusing the same one means a rejection reason reads identically
whether a vendor sees it in the portal or in their inbox.

NO NEW STORED "WAS IT SENT" COLUMN

Whether a rejection email was already sent, to whom, and when, is DERIVED from
`invoice_activity` -- the same table (and the same append-only discipline)
every other action on a run is already recorded in (§6.1) -- never a new
column on `runs`. This is the same call this project has made repeatedly
(review claims, PO balances, KPIs, the log, §6.2/§7c.1/§7d.1/etc.): a fact
already implied by an event log is not a fact worth a second place to store
it. `REJECTION_EMAIL_SENT` / `REJECTION_EMAIL_FAILED` are two more event types
in that same log, nothing else.
"""
import re
from datetime import datetime, timezone

import config
import email_outbound
import oauth_google
import portal
import storage

EVENT_SENT = "REJECTION_EMAIL_SENT"
EVENT_FAILED = "REJECTION_EMAIL_FAILED"
EVENT_EXPORTED = "AUDIT_REPORT_EXPORTED"

# A practical sanity check, not full RFC 5322: reject anything that is not
# obviously "text@text.text", and -- separately and explicitly -- anything
# carrying a control character, which is how a header-injection attempt would
# arrive (a second To/Cc/Bcc line smuggled in behind a newline). The stdlib
# `email` package would very likely refuse an embedded newline on its own; this
# refuses it before the value is anywhere near a message object.
_EMAIL_RE = re.compile(r"\A[^\s@]+@[^\s@]+\.[^\s@]+\Z")


class RecipientError(ValueError):
    """The resolved or supplied recipient cannot be used. Message is safe to
    show a reviewer directly -- it never repeats attacker-supplied text."""


def valid_recipient(address: str) -> bool:
    if not address or len(address) > 320:
        return False
    if any(ch in address for ch in ("\r", "\n", "\t")):
        return False
    return bool(_EMAIL_RE.match(address))


def resolve_default_recipient(run_id: int) -> str:
    """The vendor address this run's invoice actually arrived from, or None.

    Sourced from `email_messages.from_address` via the row `run_id` links to
    (Phase F/G) -- the address that was AUTHENTICATED to some degree, not one
    typed into the extracted invoice text, which is document content and
    exactly as trustworthy as anything else a sender chose to print on a PDF
    (§7a.1's whole point). A manually uploaded or portal-submitted invoice has
    no such row and so has no default: this function returns None rather than
    guessing at the extracted vendor name plus a domain, which would be
    inventing an address nobody supplied.
    """
    row = storage.email_for_run(run_id)
    return (row or {}).get("from_address") or None


def sender_availability() -> dict:
    """Whether THIS deployment can currently send at all, and why not if it
    can't -- for the preview endpoint to tell the UI before a reviewer writes
    a message that cannot go anywhere. Never raises."""
    connection = storage.get_oauth_connection(email_outbound.PROVIDER)
    if not connection or connection.get("status") != storage.OAUTH_CONNECTED:
        return {"available": False,
                "reason": "No Gmail mailbox is connected. Connect one from Settings."}
    granted = (connection.get("scopes") or "").split()
    if not oauth_google.can_send(granted):
        return {"available": False,
                "reason": "The connected Gmail mailbox was not granted permission to "
                          "send. Reconnect Gmail with sending enabled."}
    return {"available": True, "reason": None}


def _invoice_identity(run: dict) -> dict:
    audit = run.get("audit") or {}
    invoice = audit.get("invoice") or {}
    extracted = run.get("extracted") or {}
    return {
        "invoice_number": invoice.get("invoice_number") or extracted.get("invoice_number"),
        "vendor_name": invoice.get("vendor") or extracted.get("vendor_name"),
        "currency": invoice.get("currency") or extracted.get("currency"),
        "total": invoice.get("total") if invoice.get("total") is not None
                 else extracted.get("total"),
    }


def _friendly_date(iso: str) -> str:
    if not iso:
        return "the date on file"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)[:10]
    return dt.strftime("%d %B %Y")


def rejection_reasons(run: dict) -> list:
    """The vendor-safe sentences this run's rejection is explained by.

    `portal.client_state()`, unchanged -- see the module docstring for why
    this is reused rather than reimplemented. Works for any REJECTED run
    regardless of source (email, manual upload, portal submission): the rules
    that produced `audit["rules_failed"]` don't know or care how the invoice
    arrived, and neither does this.
    """
    state, _headline, lines = portal.client_state(run.get("status"), run.get("audit"))
    return lines


def compose_rejection_email(run: dict) -> dict:
    """{"recipient", "subject", "body", "reasons"} -- a ready-to-review draft.

    Nothing here is sent. This is the PREVIEW a reviewer reads, edits if they
    want to, and then explicitly confirms (or does not) -- see main.py's two
    separate endpoints, mirroring the release-then-process split Phase G's
    email quarantine already established (§7b.10): composing is not sending.
    """
    identity = _invoice_identity(run)
    invoice_number = identity["invoice_number"] or f"run #{run.get('id')}"
    reasons = rejection_reasons(run)
    recipient = resolve_default_recipient(run["id"])

    lines = [
        "Hello,",
        "",
        f"We reviewed the invoice submitted on {_friendly_date(run.get('created_at'))}.",
        "",
        f"Invoice: {invoice_number}",
        "",
        "Unfortunately, the invoice could not be approved for the following reasons:",
        "",
        *[f"- {reason}" for reason in reasons],
        "",
        "Please correct the above issues and resubmit the invoice.",
        "",
        "Regards,",
        "Invoice Processing Team",
    ]
    return {
        "recipient": recipient,
        "subject": f"Invoice Rejected – {invoice_number}",
        "body": "\n".join(lines),
        "reasons": reasons,
        "invoice_number": invoice_number,
        "vendor_name": identity["vendor_name"],
    }


def rejection_email_history(run_id: int) -> list:
    """Every send attempt for this run, oldest first -- straight off
    `invoice_activity`, the one history this project keeps (§6.1). No second
    table, no cache: this is `storage.list_activity()` filtered to the two
    event types this module writes."""
    return [a for a in storage.list_activity(run_id)
            if a["event_type"] in (EVENT_SENT, EVENT_FAILED)]


def last_successful_send(run_id: int) -> dict:
    """The most recent SUCCESSFUL send, or None. This -- not a stored flag --
    is what "already sent" means, and what the duplicate-send guard checks."""
    sent = [a for a in rejection_email_history(run_id) if a["event_type"] == EVENT_SENT]
    return sent[-1] if sent else None


def send_rejection_email(run_id: int, actor: str, recipient: str, subject: str,
                         body: str, reasons=None, force: bool = False) -> dict:
    """Validate, send, and record -- the one path every send goes through.

    Order matters and each step can stop the attempt before anything is sent:
    the run must exist and be REJECTED; the recipient must look like an email
    address and carry no control characters; a prior successful send blocks a
    repeat unless the caller explicitly forces a resend. Only after all three
    hold does this reach `email_outbound.get_sender().send()`.

    BOTH OUTCOMES ARE RECORDED. A failed send is audited exactly as
    thoroughly as a successful one (event, recipient, subject, error,
    error category) -- "did this get attempted, and what happened" must be
    answerable even when the answer is no, per this feature's whole point.
    Nothing about the failure is ever mistaken for success: the run's
    REJECTED status is never touched by this function, on either outcome.
    """
    run = storage.get_run(run_id)
    if not run:
        return {"ok": False, "error": "unknown run"}
    if run.get("status") != "REJECTED":
        return {"ok": False,
                "error": "a rejection notice may only be sent for a REJECTED invoice"}

    if not valid_recipient(recipient):
        return {"ok": False, "error": "recipient does not look like a usable email address"}

    if not force:
        previous = last_successful_send(run_id)
        if previous:
            return {"ok": False, "error": "duplicate",
                    "previous": {"recipient": (previous.get("metadata") or {}).get("recipient"),
                                "sent_at": previous["created_at"],
                                "by": previous["actor"]}}

    subject = (subject or "").strip()[:300] or f"Invoice Rejected – run #{run_id}"
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "the email body cannot be empty"}

    identity = _invoice_identity(run)
    metadata = {
        "recipient": recipient,
        "subject": subject,
        "invoice_number": identity["invoice_number"],
        "vendor_name": identity["vendor_name"],
        "reasons": reasons if reasons is not None else rejection_reasons(run),
        "resend": bool(force and last_successful_send(run_id)),
    }

    try:
        result = email_outbound.get_sender().send(recipient, subject, body)
    except email_outbound.EmailSendError as exc:
        storage.log_activity(
            run_id, EVENT_FAILED, actor=actor,
            note=f"rejection email to {recipient} failed: {exc}",
            metadata={**metadata, "error": str(exc), "error_category": exc.code or "unknown"})
        return {"ok": False, "error": str(exc), "error_category": exc.code or "unknown"}

    storage.log_activity(
        run_id, EVENT_SENT, actor=actor,
        note=f"rejection email sent to {recipient}",
        metadata={**metadata, "message_id": result.get("message_id"),
                 "provider": result.get("provider")})
    return {"ok": True, "recipient": recipient, "subject": subject,
            "message_id": result.get("message_id"),
            "sent_at": datetime.now(timezone.utc).isoformat()}


def log_export(run_id: int, actor: str, fmt: str):
    """One activity row per audit-report download -- the same DOCUMENT_VIEWED/
    DOCUMENT_DOWNLOADED precedent (§5) applied to the export this feature
    adds: an export is an action taken on invoice data and belongs in the same
    history as opening or downloading the source document."""
    storage.log_activity(run_id, EVENT_EXPORTED, actor=actor, metadata={"format": fmt})
