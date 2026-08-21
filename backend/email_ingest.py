"""Email invoice ingestion (Phase G) -- the orchestrator.

WHAT THIS MODULE IS

The wiring between four things that already existed and one that did not:

    email_provider.py   fetches raw messages          (new, Phase G)
    email_triage.py     decides if it is worth it     (new, Phase G)
    email_security.py   decides what can be proven    (Phase F, unchanged)
    main.run_pipeline() reads and judges the invoice  (Phases 0-2, unchanged)
    documents.py        keeps the PDF                 (Phase C, unchanged)

It holds no business logic of its own. It does not decide whether an invoice is
approved, does not re-implement PO matching, and does not know what a tolerance
is -- it hands a PDF to the same pipeline a browser upload goes through, and
records what came back. **There is exactly one invoice pipeline in this
application**, and this module is a second door into it, not a second copy of
it.

THE ORDER OF THE STAGES IS THE POINT

    1. fetch                  cheap
    2. idempotency check      one indexed lookup; a message seen before stops here
    3. parse headers + MIME   cheap, stdlib, no attachment is opened
    4. triage                 cheap, deterministic, NO MODEL      <- stops most mail
    5. Phase F verification   cryptography, microseconds
    6. attachment validation  opens attachments, checks magic bytes
    7. run_pipeline()         EXPENSIVE: OCR, LLM, PO matching

Stage 4 exists so that stage 7 is never reached by a message that obviously
has nothing to do with invoices. A newsletter with no PDF costs one header
parse and two dictionary lookups; it never touches an LLM, and never spends a
cent of the daily extraction budget. That ordering is enforced structurally --
`ingest_message()` returns before `_process_attachments()` is called -- and is
asserted directly by the test suite, which spies on `extraction.extract_invoice`
and fails if an irrelevant message ever reaches it.

WHAT IS NEVER DONE HERE

* **Nothing is deleted.** A message that fails triage is recorded, kept, and
  readable afterwards, with the reasons that stopped it. "Not worth an LLM
  call" is not "not an invoice", and a human can look.
* **Phase F is never bypassed.** Every message that gets past triage is
  classified, and only a message the database says is ADMITTED or RELEASED can
  reach `_process_attachments()`. That check reads the stored row, not an
  in-memory variable, so it holds across restarts and across processes.
* **Nothing is silently dropped.** Every failure path writes a status and an
  activity row before returning.
"""
import asyncio
import hashlib
import json
import re
import sys
import traceback

import config
import documents          # noqa: F401  (kept: the pipeline's persistence path)
import email_provider
import email_security
import email_triage
import storage

_PDF_MAGIC = b"%PDF-"

# Attachment names arrive attacker-controlled and end up in the database, in
# the UI, and as the download filename. Reduced to a bare, printable basename
# exactly as main.py's `_safe_filename` does for a browser upload -- one rule
# for both doors.
_UNSAFE_NAME_CHARS = r'\/:*?"<>|'


def safe_attachment_filename(name: str, fallback: str = "attachment.pdf") -> str:
    name = (name or "").replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in _UNSAFE_NAME_CHARS)
    name = name.strip(". ")
    # ".." survives basename-stripping on its own, and a name that reduces to
    # nothing must still produce something storable.
    if not name or set(name) <= {"."}:
        return fallback
    return name[:180]


def _walk_attachment_parts(msg, limit=200):
    """Every leaf part that presents as an attachment, with its bytes.

    This is the first point in the whole flow where attachment CONTENT is
    touched at all -- deliberately after triage and after Phase F, so a message
    that was never going to be processed never has its attachments decoded.
    """
    seen = 0
    for part in msg.walk():
        if seen >= limit:
            break
        if part.is_multipart():
            continue
        seen += 1
        disposition = (part.get_content_disposition() or "")
        filename = part.get_filename()
        if disposition != "attachment" and not filename:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            # A part with a broken transfer encoding is reported as an empty,
            # unusable attachment rather than taking the message down.
            payload = b""
        yield part, filename, (part.get_content_type() or "").lower(), payload


def collect_attachments(raw: bytes) -> list:
    """Attachment metadata AND bytes, validated, deduplicated.

    Every attachment is described whether or not it is usable, because "we
    ignored the 4 MB image in your email" has to be answerable later. Only the
    ones that pass validation carry `data`.
    """
    import email as _email
    import email.policy as _policy
    try:
        msg = _email.message_from_bytes(raw, policy=_policy.default)
    except Exception:
        try:
            msg = _email.message_from_bytes(raw)
        except Exception:
            return []

    out, seen_hashes = [], set()
    max_bytes = config.MAX_UPLOAD_BYTES
    for _, filename, content_type, payload in _walk_attachment_parts(msg):
        safe_name = safe_attachment_filename(filename) if filename else None
        digest = hashlib.sha256(payload).hexdigest() if payload else None
        item = {
            "filename": safe_name,
            "original_filename_was_unsafe": bool(
                filename and safe_name and safe_name != filename),
            "content_type": content_type,
            "size_bytes": len(payload),
            "sha256": digest,
            "is_invoice_candidate": False,
            "status": "SKIPPED",
            "skip_reason": None,
            "data": None,
        }

        if not payload:
            item["skip_reason"] = "the attachment is empty or could not be decoded"
        elif digest in seen_hashes:
            # The identical file attached twice to one email. Recorded so it is
            # visible, processed once.
            item["skip_reason"] = "a byte-identical attachment already appeared in this message"
        elif len(payload) > max_bytes:
            item["skip_reason"] = (f"the attachment is {len(payload) // 1024} KB, over the "
                                   f"{max_bytes // (1024 * 1024)} MB limit")
        elif not payload.startswith(_PDF_MAGIC):
            # CONTENT decides, not the filename and not the declared type --
            # both are chosen by the sender. This is the same magic-byte test
            # `main._validate_pdf` applies to a browser upload, and it is what
            # stops "invoice.pdf" that is actually a script from ever being
            # handed to a PDF parser.
            declared = content_type or "unknown type"
            item["skip_reason"] = (
                f"not a PDF: the content does not begin with %PDF- "
                f"(declared {declared}). The invoice pipeline reads PDFs only.")
        else:
            item.update({"is_invoice_candidate": True, "status": "PENDING",
                         "data": payload})
            seen_hashes.add(digest)

        if digest:
            seen_hashes.add(digest)
        out.append(item)
    return out


def _metadata_only(attachments):
    """The same list with the bytes removed, for anything that persists it."""
    return [{k: v for k, v in a.items() if k != "data"} for a in attachments]


# --------------------------------------------------------------------------
# Ingesting one message
# --------------------------------------------------------------------------
def ingest_message(incoming, submitted_by: str = None, trusted_senders=None,
                   process: bool = True) -> dict:
    """Take one raw message all the way to a verdict. The whole Phase G flow.

    Returns a summary describing where the message got to and why. Never
    raises for anything the message itself did -- a malformed message is a
    result, not an exception -- so one bad message can never stop a poll and
    strand every message behind it.
    """
    provider = getattr(incoming, "provider", "unknown")
    message_id = getattr(incoming, "provider_message_id", None)
    raw = getattr(incoming, "raw", b"") or b""

    if not message_id:
        # Without a stable id there is no idempotency, and reprocessing on the
        # next poll is guaranteed. Refused rather than processed once and
        # duplicated forever after.
        return {"ok": False, "status": "FAILED", "duplicate": False,
                "error": "the provider supplied no message id"}

    # ---- 2. have we seen it before? One indexed lookup, before any parsing.
    existing = storage.email_for_provider_message(provider, message_id)
    if existing:
        storage.log_email_activity(
            existing["id"], "DUPLICATE_DELIVERY", submitted_by,
            note="the provider delivered this message again; it was not reprocessed",
            metadata={"provider": provider, "provider_message_id": message_id})
        return {"ok": True, "duplicate": True, "email_id": existing["id"],
                "status": existing.get("ingest_status"),
                "detail": "already ingested; not reprocessed"}

    if not raw:
        # An oversized or unreadable fetch. Recorded as a real, visible failure
        # -- an operator has to know a message arrived that we could not take.
        return _record_failure(provider, message_id, submitted_by,
                               "the message was empty or too large to retrieve",
                               incoming)

    if len(raw) > config.email_max_message_bytes():
        return _record_failure(provider, message_id, submitted_by,
                               f"the message is {len(raw) // 1024} KB, over the configured "
                               f"limit of {config.email_max_message_bytes() // 1024} KB",
                               incoming)

    trusted_senders = (trusted_senders if trusted_senders is not None
                       else storage.list_trusted_senders())

    # ---- 3. parse (cheap: headers and MIME structure, no attachment opened)
    try:
        parsed = email_security.parse_message(raw)
        parsed.pop("_fields", None)
        parsed.pop("_message", None)
    except Exception as exc:
        return _record_failure(provider, message_id, submitted_by,
                               f"the message could not be parsed: {exc.__class__.__name__}",
                               incoming)

    # ---- 4. TRIAGE. Cheap, deterministic, no model. The gate that keeps
    #        obviously-irrelevant mail away from everything expensive.
    triage = email_triage.triage(parsed, trusted_senders=trusted_senders)

    if not triage["proceed"]:
        # Stops here. No cryptography, no attachment decoding, no extraction,
        # no quota spent. The message is KEPT, with its reasons, and a human
        # can still look at it and re-run it.
        record = dict(parsed)
        record.update({
            "classification": None, "status": None, "reasons": [],
            "spf_result": None, "dkim_result": None, "dmarc_result": None,
            "dmarc_aligned": False, "signature_kind": None, "signature_result": None,
            "trusted_sender": triage["sender"]["trust_status"] == "TRUSTED",
            "audit": {"triage_only": True,
                      "note": ("this message did not pass the relevance filter, so no "
                               "authentication verification was performed on it"),
                      "triage": triage},
        })
        record["classification"] = "UNVERIFIED"
        record["status"] = "QUARANTINED"
        record["reasons"] = [
            f"filtered out before processing: {triage['relevance']['relevance']}",
            *triage["relevance"]["reasons"],
        ]
        claim = storage.claim_incoming_message(
            provider, message_id, record, triage, "FILTERED_OUT",
            provider_received_at=getattr(incoming, "received_at", None),
            submitted_by=submitted_by)
        if not claim["created"]:
            return {"ok": True, "duplicate": True, "email_id": claim["id"],
                    "status": "FILTERED_OUT"}
        storage.record_attachments(claim["id"], [
            {**a, "status": "SKIPPED",
             "skip_reason": "the message did not pass the relevance filter",
             "is_invoice_candidate": False}
            for a in (parsed.get("attachments") or [])])
        return {"ok": True, "duplicate": False, "email_id": claim["id"],
                "status": "FILTERED_OUT", "relevance": triage["relevance"]["relevance"],
                "reasons": triage["relevance"]["reasons"],
                "processed_attachments": 0, "runs": []}

    # ---- 5. PHASE F. Unmodified, and unavoidable: everything past triage is
    #        verified, and the verdict decides whether anything may proceed.
    try:
        record = email_security.classify(raw, trusted_senders=trusted_senders)
    except Exception as exc:
        return _record_failure(provider, message_id, submitted_by,
                               f"security verification failed: {exc.__class__.__name__}",
                               incoming, triage=triage)

    ingest_status = "RECEIVED" if record["status"] == "ADMITTED" else "QUARANTINED"
    claim = storage.claim_incoming_message(
        provider, message_id, record, triage, ingest_status,
        provider_received_at=getattr(incoming, "received_at", None),
        submitted_by=submitted_by)
    if not claim["created"]:
        return {"ok": True, "duplicate": True, "email_id": claim["id"],
                "status": ingest_status, "detail": "already ingested; not reprocessed"}
    email_id = claim["id"]

    # ---- 6. attachment validation (first time any attachment is opened)
    attachments = collect_attachments(raw)
    storage.record_attachments(email_id, _metadata_only(attachments))

    if record["status"] != "ADMITTED":
        # QUARANTINED. Phase F's own hold, reused exactly as it is: nothing is
        # processed, and a reviewer with invoice:review releases or discards
        # through the Phase F endpoints.
        #
        # The candidate PDFs ARE preserved, in the existing DocumentStore, so
        # that releasing a message later has something to act on -- a
        # quarantine that threw the invoice away would force the vendor to
        # resend it. The message BODY is still never stored; only the
        # attachment, which is the invoice itself.
        held = _hold_attachments(email_id, attachments)
        storage.set_ingest_status(
            email_id, "QUARANTINED",
            note=(f"held by email security verification; {held} attachment(s) preserved "
                  f"for review. Release it to process, or discard it."),
            event="QUARANTINED")
        return {"ok": True, "duplicate": False, "email_id": email_id,
                "status": "QUARANTINED", "classification": record["classification"],
                "reasons": record["reasons"], "held_attachments": held,
                "processed_attachments": 0, "runs": []}

    if not process:
        return {"ok": True, "duplicate": False, "email_id": email_id,
                "status": "RECEIVED", "classification": record["classification"],
                "processed_attachments": 0, "runs": []}

    # ---- 7. the existing pipeline
    outcome = process_message_attachments(email_id, raw, actor=submitted_by,
                                          attachments=attachments, triage=triage,
                                          classification=record["classification"])
    # Every return from ingest_message carries `duplicate`, so a caller never
    # has to know which branch produced the answer.
    return {**outcome, "duplicate": False}


def _record_failure(provider, message_id, submitted_by, error, incoming, triage=None):
    """Persist a message we could not handle. Never a silent drop.

    Written with whatever is known -- often only the provider's id -- because a
    message that arrived and could not be processed is exactly the thing an
    operator must be able to find later.
    """
    triage = triage or {"sender": {}, "relevance": {}, "proceed": False}
    record = {
        "classification": "UNVERIFIED", "status": "QUARANTINED",
        "reasons": [f"ingestion failed: {error}"],
        "audit": {"ingestion_error": error},
        "sha256": hashlib.sha256(getattr(incoming, "raw", b"") or b"").hexdigest(),
        "size_bytes": len(getattr(incoming, "raw", b"") or b""),
    }
    try:
        claim = storage.claim_incoming_message(
            provider, message_id, record, triage, "FAILED",
            provider_received_at=getattr(incoming, "received_at", None),
            submitted_by=submitted_by)
        storage.set_ingest_status(claim["id"], "FAILED", error=error, event="INGEST_FAILED")
        return {"ok": False, "duplicate": not claim["created"], "email_id": claim["id"],
                "status": "FAILED", "error": error}
    except Exception as exc:
        # The database itself is unavailable. Nothing can be recorded, so this
        # is reported upward rather than swallowed -- and because the message
        # was never marked handled at the provider, the next poll will see it
        # again.
        print(f"[error] could not record ingestion failure for {provider}:{message_id}: "
              f"{exc.__class__.__name__}", file=sys.stderr)
        return {"ok": False, "duplicate": False, "email_id": None,
                "status": "FAILED", "error": error, "unrecorded": True}


def _hold_attachments(email_id: int, attachments) -> int:
    """Keep a quarantined message's candidate PDFs, via Phase C's DocumentStore.

    No second storage system: the same `DocumentStore` the invoice documents
    use, and the same server-generated key shape, so the path-safety argument
    Phase C made (a key is never derived from a sender-supplied filename)
    applies here unchanged.
    """
    rows = {r["sha256"]: r for r in storage.list_email_attachments(email_id) if r.get("sha256")}
    held = 0
    for att in attachments or []:
        row = rows.get(att.get("sha256"))
        if row is None or not att.get("data") or not att.get("is_invoice_candidate"):
            continue
        if row.get("storage_key"):
            held += 1
            continue
        try:
            key = documents.new_storage_key()
            documents.get_store().save(key, att["data"])
            storage.set_attachment_storage(row["id"], config.document_store_backend(), key)
            held += 1
        except Exception as exc:
            # Failing to hold the bytes must not lose the security record --
            # the message stays quarantined either way. It is recorded so the
            # gap is visible rather than discovered on release.
            print(f"[error] could not preserve attachment {row['id']} for email "
                  f"{email_id}: {exc.__class__.__name__}", file=sys.stderr)
            storage.log_email_activity(
                email_id, "ATTACHMENT_HOLD_FAILED", None,
                note=f"the attachment bytes could not be preserved: {exc.__class__.__name__}",
                metadata={"attachment_id": row["id"]})
    return held


def _load_held_attachment(row) -> bytes:
    """Read a preserved attachment back, or b'' if it is not retrievable."""
    if not row.get("storage_key"):
        return b""
    try:
        return documents.get_store().read(row["storage_key"]) or b""
    except Exception:
        return b""


def _release_held_bytes(row):
    """Drop the holding copy once the run owns one. Best-effort, never fatal."""
    if not row.get("storage_key"):
        return
    try:
        documents.get_store().delete(row["storage_key"])
    except Exception:
        pass
    try:
        storage.set_attachment_storage(row["id"], None, None)
    except Exception:
        pass


def process_message_attachments(email_id: int, raw: bytes = b"", actor: str = None,
                                attachments=None, triage=None, classification=None) -> dict:
    """Feed a message's PDF attachments through the existing invoice pipeline.

    THE PHASE F GATE. The eligibility check reads the STORED row, not a value
    passed in by the caller, so a message can only be processed if the database
    says it is ADMITTED or RELEASED. That holds across restarts, across
    processes, and regardless of how this function was reached -- there is no
    argument a caller can pass to skip it.
    """
    message = storage.get_email_message(email_id)
    if not message:
        return {"ok": False, "error": "unknown message"}
    if message.get("status") not in ("ADMITTED", "RELEASED"):
        return {"ok": False, "error":
                f"a message with security status {str(message.get('status')).lower()} may not "
                f"be processed; release it first"}

    if attachments is None and raw:
        attachments = collect_attachments(raw)
        storage.record_attachments(email_id, _metadata_only(attachments))

    rows = {r["sha256"]: r for r in storage.list_email_attachments(email_id) if r.get("sha256")}
    if attachments is None:
        # The release path: no raw message (Phase F never kept it), so the work
        # list is the stored attachment rows, and the bytes come from the
        # holding copy written when the message was quarantined.
        attachments = [{"sha256": r["sha256"], "filename": r["filename"],
                        "content_type": r["content_type"],
                        "is_invoice_candidate": bool(r["is_invoice_candidate"]),
                        "skip_reason": r["skip_reason"], "data": None}
                       for r in rows.values()]

    runs, processed, failed, skipped = [], 0, 0, 0

    for att in attachments:
        row = rows.get(att.get("sha256"))
        if row is None:
            continue

        # IDEMPOTENCY FIRST, before anything is read or decoded. An attachment
        # that already produced a run is reported as that run and left alone --
        # its held copy has deliberately been deleted by then, so trying to
        # load its bytes would look like a retrieval failure and overwrite a
        # perfectly good PROCESSED row with FAILED.
        if row["status"] == "PROCESSED":
            if row.get("run_id"):
                runs.append(row["run_id"])
            processed += 1
            continue

        if not att.get("is_invoice_candidate"):
            if row["status"] == "PENDING":
                storage.complete_attachment(row["id"], "SKIPPED",
                                            skip_reason=att.get("skip_reason"))
            skipped += 1
            continue

        if att.get("data") is None:
            att = {**att, "data": _load_held_attachment(row)}
        if not att.get("data"):
            # It WAS a usable PDF and the preserved copy cannot be read back.
            # Recorded as a failure rather than a skip, because that is our
            # problem to fix, not something the sender did.
            storage.complete_attachment(
                row["id"], "FAILED",
                error="the preserved copy of this attachment could not be read back")
            failed += 1
            continue

        # The same check again under a row lock, so two concurrent passes over
        # one message cannot both start work on the same attachment.
        claim = storage.claim_attachment_for_processing(row["id"])
        if not claim.get("ok"):
            if claim.get("already"):
                if claim.get("run_id"):
                    runs.append(claim["run_id"])
                processed += 1
            continue

        try:
            run = _run_invoice_pipeline(att["filename"] or "invoice.pdf", att["data"],
                                        uploaded_by=actor)
        except Exception as exc:
            # An extraction or pipeline failure is recorded against THIS
            # attachment and the loop continues: one corrupt PDF must not stop
            # the other two invoices in the same email from being processed.
            detail = f"{exc.__class__.__name__}: {exc}"[:500]
            print(f"[error] pipeline failed for email {email_id} attachment "
                  f"{row['id']}: {exc.__class__.__name__}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            storage.complete_attachment(row["id"], "FAILED", error=detail)
            failed += 1
            continue

        storage.complete_attachment(row["id"], "PROCESSED", run_id=run["run_id"],
                                    run_status=run["status"])
        # The run now owns its own copy through the documents table, so the
        # quarantine holding copy is redundant and is dropped.
        _release_held_bytes(row)
        runs.append(run["run_id"])
        processed += 1

    if failed and processed:
        status = "PARTIAL"
    elif failed:
        status = "FAILED"
    elif processed:
        status = "PROCESSED"
    else:
        status = "NO_ATTACHMENTS"

    storage.set_ingest_status(
        email_id, status,
        error=(f"{failed} attachment(s) failed" if failed else None),
        actor=actor,
        note=(f"{processed} invoice run(s) created, {skipped} attachment(s) skipped"
              if status != "NO_ATTACHMENTS"
              else "no usable PDF attachment was found in this message"),
        event="INGEST_COMPLETED")

    return {"ok": True, "email_id": email_id, "status": status,
            "classification": classification or message.get("classification"),
            "processed_attachments": processed, "skipped_attachments": skipped,
            "failed_attachments": failed, "runs": runs}


def _run_invoice_pipeline(filename: str, pdf_bytes: bytes, uploaded_by: str = None) -> dict:
    """Drive the EXISTING pipeline and return its final result.

    `main.run_pipeline` is an async generator that streams SSE frames to a
    browser. Rather than reimplement any of it for the email path -- which
    would be a second invoice pipeline, quietly drifting from the first -- this
    consumes exactly the same generator and reads the `final` frame it already
    emits. Every stage, the audit trail, the confidence gate, PO matching, the
    allocation ledger, the document persistence and the review routing are the
    ones a browser upload gets, because they ARE the ones a browser upload
    gets.

    `source="EMAIL"` is the value `config.DOCUMENT_SOURCES` has recognised
    since Phase C and that nothing has written until now.
    """
    import main   # deferred: main imports this module

    async def drive():
        final = None
        async for frame in main.run_pipeline(filename, pdf_bytes,
                                             uploaded_by=uploaded_by, source="EMAIL"):
            if not frame.startswith("data: "):
                continue
            try:
                payload = json.loads(frame[len("data: "):].strip())
            except (ValueError, TypeError):
                continue
            if payload.get("type") == "final":
                final = payload.get("result")
            elif payload.get("type") == "error":
                raise RuntimeError(payload.get("error") or "pipeline error")
        return final

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        result = asyncio.run(drive())
    else:
        # Called from inside a running loop (a FastAPI request handler). The
        # pipeline is driven on its own loop in a worker thread so this cannot
        # deadlock on the caller's.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(lambda: asyncio.run(drive())).result()

    if not result:
        raise RuntimeError("the pipeline produced no final result")
    return result


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------
def poll_once(provider=None, limit: int = None, actor: str = None) -> dict:
    """One pass over the mailbox. Safe to run concurrently with itself.

    Concurrency safety is not a lock -- it is the unique constraint on
    (provider, provider_message_id). Two workers polling the same folder will
    both fetch the same message and both try to claim it; exactly one wins the
    INSERT and processes it, and the other is told it is a duplicate. That is
    why this can run in every uvicorn worker without coordination.
    """
    own = provider is None
    if provider is None:
        try:
            provider = email_provider.get_provider()
        except Exception as exc:
            return {"ok": False, "error": f"{exc}", "fetched": 0, "results": []}

    limit = limit or config.email_poll_batch()
    try:
        messages = provider.fetch(limit)
    except email_provider.EmailProviderError as exc:
        # A provider that is down, or rejecting our credentials, is a visible
        # failure -- never an empty poll, which would look like "no new mail".
        _close_if_owned(provider, own)
        return {"ok": False, "error": str(exc), "fetched": 0, "results": []}
    except Exception as exc:
        _close_if_owned(provider, own)
        return {"ok": False, "error": f"provider error: {exc.__class__.__name__}",
                "fetched": 0, "results": []}

    results = []
    trusted = storage.list_trusted_senders()
    for message in messages:
        try:
            outcome = ingest_message(message, submitted_by=actor, trusted_senders=trusted)
        except Exception as exc:
            print(f"[error] ingestion crashed on {message!r}: {exc.__class__.__name__}",
                  file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            outcome = {"ok": False, "status": "FAILED",
                       "error": f"{exc.__class__.__name__}"}
        results.append({"provider_message_id": message.provider_message_id, **outcome})
        # Marked only after the outcome is committed. A crash before this point
        # leaves the message unflagged, so the next poll sees it again -- and
        # the unique constraint stops that becoming a duplicate.
        if outcome.get("ok") or outcome.get("duplicate"):
            try:
                provider.mark_handled(message)
            except Exception:
                pass

    _close_if_owned(provider, own)
    return {"ok": True, "fetched": len(messages), "results": results,
            "provider": getattr(provider, "name", "unknown")}


def _close_if_owned(provider, own: bool):
    """Close only a provider this call constructed. A caller that passed one in
    (a test, or a future scheduler holding a long-lived connection) keeps
    ownership of it."""
    if not own:
        return
    try:
        provider.close()
    except Exception:
        pass


# --------------------------------------------------------------------------
# The background poller
# --------------------------------------------------------------------------
_poller_task = None

# The event loop the application is actually running on.
#
# WHY THIS HAS TO BE REMEMBERED (Phase G2). `start_poller()` was previously
# only ever called from the FastAPI startup handler, which Starlette invokes
# from inside the running loop -- so reaching for the current loop there just
# worked. It is now ALSO called from the Gmail OAuth callback, and a sync
# FastAPI path operation runs in a worker thread with NO running loop at all.
# Asking for one there raises, which would have made "connecting a mailbox
# starts reading it" quietly false until the next restart.
_main_loop = None


async def _poll_forever():
    interval = config.email_poll_seconds()
    print(f"[email] ingestion poller started (every {interval}s, "
          f"provider={config.email_provider()})", file=sys.stderr)
    while True:
        try:
            # The provider does blocking socket I/O and the pipeline does
            # blocking CPU work, so a poll runs in a worker thread. Doing it on
            # the event loop would stall every HTTP request for the duration of
            # an LLM call.
            result = await asyncio.to_thread(poll_once, None, None, None)
            if not result.get("ok"):
                print(f"[email] poll failed: {result.get('error')}", file=sys.stderr)
            elif result.get("fetched"):
                print(f"[email] polled {result['fetched']} message(s)", file=sys.stderr)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The loop must survive anything. A poller that dies on one bad
            # message is a mailbox nobody is reading.
            print(f"[email] poll cycle error: {exc.__class__.__name__}", file=sys.stderr)
        await asyncio.sleep(max(15, config.email_poll_seconds()))


def ingestion_configured() -> bool:
    """Whether there is actually a mailbox for the poller to read.

    IMAP IS UNCHANGED: it polls when `EMAIL_INGEST_ENABLED` is set, exactly as
    it always has, and nothing about a Gmail connection turns it on or off.

    What is new is that an unset `EMAIL_PROVIDER` now defers to a stored Gmail
    connection (Phase G2). An administrator who has just walked through
    Google's consent screen has expressed the intention more concretely than an
    environment variable could, and requiring them to ALSO edit `.env` and
    restart the process would make the "Connected" badge in the UI a statement
    about nothing. Disconnecting removes the connection and stops the poller by
    the same rule.

    A provider named explicitly always wins over stored state -- `gmail` still
    needs a live connection to be worth polling, and `imap` is never overridden
    by one.
    """
    provider = config.email_provider()
    if provider == "imap":
        return config.email_ingest_enabled()
    if provider == "gmail":
        return email_provider.gmail_connection_is_live()
    return email_provider.gmail_connection_is_live()


def remember_event_loop():
    """Record the loop the app runs on, so a worker thread can reach it later.

    Called once from the FastAPI startup handler, which Starlette runs from
    inside the loop. See `_main_loop` above for why this is needed at all.
    """
    global _main_loop
    try:
        _main_loop = asyncio.get_running_loop()
    except RuntimeError:
        _main_loop = None
    return _main_loop is not None


def start_poller() -> bool:
    """Start the background poller if ingestion is configured. Idempotent.

    Safe to call from a request handler as well as from startup. A sync
    FastAPI endpoint runs in a worker thread with no running loop, so the task
    is handed to the remembered loop with `call_soon_threadsafe` -- creating a
    task directly from another thread is not safe, and asking that thread for
    "the" event loop raises.
    """
    global _poller_task
    if not ingestion_configured():
        return False
    if _poller_task is not None and not _poller_task.done():
        return True

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    loop = running or _main_loop
    if loop is None or loop.is_closed():
        # No loop to run on: a management command, a test calling this
        # directly, or a process that never started the app. Reported as not
        # started rather than raising -- ingestion is not what that caller was
        # doing.
        return False

    def _spawn():
        # Re-checked inside the loop: between scheduling and running, another
        # call may have started it, or a disconnect may have removed the
        # mailbox this was started for.
        global _poller_task
        if _poller_task is not None and not _poller_task.done():
            return
        if not ingestion_configured():
            return
        _poller_task = loop.create_task(_poll_forever())

    if running is loop:
        _spawn()
    else:
        loop.call_soon_threadsafe(_spawn)
    return True


def stop_poller():
    global _poller_task
    task, _poller_task = _poller_task, None
    if task is not None and not task.done():
        task.cancel()


def poller_running() -> bool:
    return _poller_task is not None and not _poller_task.done()


def ingestion_status() -> dict:
    """Non-secret configuration and current state, for the admin endpoint."""
    try:
        provider = email_provider.get_provider()
        described, error = provider.describe(), None
        try:
            provider.close()
        except Exception:
            pass
    except Exception as exc:
        described, error = {"provider": config.email_provider()}, str(exc)
    # The Gmail connection, through the projection that does not select the
    # token columns at all (storage.public_oauth_connection). This endpoint is
    # admin-scoped, but "only an administrator can see it" is not a reason to
    # put a refresh token in a JSON body.
    try:
        connection = storage.public_oauth_connection("gmail")
    except Exception:
        connection = None

    return {
        "enabled": config.email_ingest_enabled(),
        "active": ingestion_configured(),
        "poller_running": poller_running(),
        "poll_seconds": config.email_poll_seconds(),
        "poll_batch": config.email_poll_batch(),
        "provider": described,
        "configuration_error": error,
        "gmail": {
            # Whether a Google OAuth CLIENT is configured, which is a different
            # question from whether a mailbox is connected -- and the two have
            # different remedies (edit the environment vs. click Connect), so
            # the UI needs to be able to tell them apart.
            "oauth_configured": config.google_oauth_configured(),
            "connection": connection,
            "scopes_requested": requested_scopes(),
        },
        "counts": storage.ingestion_summary(),
    }


def requested_scopes():
    """What the consent screen will ask for, or the reason it cannot be built.

    A misconfigured GMAIL_OAUTH_SCOPES raises from config (deliberately -- see
    `config.gmail_scopes`), and the status endpoint is precisely where an
    administrator should find out about it rather than at the moment they click
    Connect.
    """
    try:
        return config.gmail_scopes()
    except ValueError as exc:
        return {"error": str(exc)}
