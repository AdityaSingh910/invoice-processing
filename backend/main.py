"""FastAPI app: invoice processing pipeline with a live (SSE) run view + dashboard."""
import asyncio
import hashlib
import json
import time
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import (Body, Depends, FastAPI, File, HTTPException, Request,
                     Security, UploadFile, status)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles

import auth
import config
import documents
import email_security
import extraction
import matching
import ratelimit
import rules
import storage
from schemas import ExtractedInvoice

app = FastAPI(title="Invoice Processing")

# CORS is configured, never relied on. It is enforced by browsers and ignored
# entirely by curl or a script, so it is not a security boundary -- the bearer
# token is. Default is same-origin (no middleware at all), which is how the app
# is actually served; CORS_ORIGINS opts specific origins in deliberately.
if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

# Which UI to serve.
#
# The Next.js app is a STATIC EXPORT: `npm run build` in frontend-next/ emits
# plain HTML/JS into out/, with no Node process at runtime. Serving it from here
# keeps the UI same-origin with the API, so the browser's relative /api/... calls
# resolve without CORS, a base URL, or a second port to get wrong.
#
# The original vanilla frontend stays as the fallback, so a clone that has never
# run npm still boots a working UI and the test suite is unaffected.
_NEXT_EXPORT = os.path.join(os.path.dirname(__file__), "..", "frontend-next", "out")
_VANILLA = os.path.join(os.path.dirname(__file__), "..", "frontend")

FRONTEND_DIR = _NEXT_EXPORT if os.path.isfile(
    os.path.join(_NEXT_EXPORT, "index.html")) else _VANILLA


# --------------------------------------------------------------------------
# error handling
#
# Two rules: say what the caller needs to act on, and never say anything about
# how this process is built. A stack trace, a provider message or a file path in
# an error body is reconnaissance, and provider errors in particular can echo
# request content back to whoever sent it.
# --------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    # `error` alongside `detail` because the existing frontend reads `error`.
    body = {"error": exc.detail, "detail": exc.detail}
    if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        body["ok"] = False
    return JSONResponse(status_code=exc.status_code, content=body,
                        headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    # Logged in full server-side, described to the client in six words.
    print(f"[error] unhandled {exc.__class__.__name__} on {request.url.path}",
          file=sys.stderr)
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        content={"error": "Internal server error",
                                 "detail": "Internal server error"})


@app.on_event("startup")
def _startup():
    config.load_dotenv()
    # Refuse to come up with a production-unsafe configuration. Deliberately
    # after load_dotenv (so .env is honoured) and before init_db, so a
    # misconfigured deployment fails immediately rather than serving traffic.
    # A no-op unless APP_ENV says this is production.
    auth.enforce_production_config()
    storage.init_db()
    if config.is_production():
        print(f"[startup] {config.APP_ENV_VAR}={config.app_env()} — production "
              f"configuration checks passed.", file=sys.stderr)


def sse(event_type, payload):
    return f"data: {json.dumps({'type': event_type, **payload})}\n\n"


# Stages after EXTRACT_TEXT, in order. Used to close out a run that cannot proceed.
_REMAINING_AFTER_TEXT = ["EXTRACT_FIELDS", "VALIDATE", "VENDOR_CHECK", "PO_MATCH",
                         "DUPLICATE_CHECK", "TOLERANCE_CHECK"]


def _persist_document(run_id, filename, pdf_bytes, uploaded_by, source):
    """Store the uploaded PDF and record its metadata against the run.

    Deliberately never allowed to fail the run it belongs to: by the time
    this runs the automated decision is already made and, in most call
    sites, already committed to `runs`. A storage-layer problem (a full disk,
    an unreachable S3 endpoint) is real and worth knowing about, but it must
    not turn a completed, correctly-decided invoice run into a pipeline
    error the operator has to re-run -- the same fail-safe posture as the
    daily quota breaker (config.py), just applied to a different resource.
    """
    try:
        key = documents.new_storage_key()
        documents.get_store().save(key, pdf_bytes)
        storage.save_document(
            run_id=run_id,
            original_filename=filename,
            mime_type="application/pdf",
            size_bytes=len(pdf_bytes),
            sha256_hex=hashlib.sha256(pdf_bytes).hexdigest(),
            uploaded_by=uploaded_by,
            source=source,
            storage_backend=config.document_store_backend(),
            storage_key=key,
        )
    except Exception as exc:
        print(f"[error] failed to persist document for run {run_id}: "
              f"{exc.__class__.__name__}", file=sys.stderr)


async def _abort_unreadable(filename, message, stages, stage, pdf_bytes=b"",
                            uploaded_by=None, source="MANUAL_UPLOAD"):
    """Close out a run whose file could not be opened at all.

    The remaining checks are reported as skipped rather than silently dropped, so
    the run view stays complete, and the run is still persisted -- an unreadable
    file is an AP event worth seeing on the dashboard, not a 500.
    """
    for name in _REMAINING_AFTER_TEXT:
        yield sse("stage", {"stage": stage(name, "warn", "Skipped — nothing could be read from the file.")})
        await asyncio.sleep(0.05)

    status = "NEEDS_REVIEW"
    reasons = [{
        "text": f"{message} Nothing was extracted, so no check could run. "
                f"Route for manual handling or request a valid PDF from the vendor.",
        "level": "fail",
    }]
    yield sse("stage", {"stage": stage("DECISION", "ok", f"Final status: {status}.")})

    extracted = ExtractedInvoice(raw_text="", extraction_method="none").to_dict()
    po_match = matching.empty_match(None)
    run_id = storage.save_run(filename, status, extracted, po_match, stages, reasons,
                              uploaded_by=uploaded_by)
    # An unreadable file is still a real upload worth keeping -- a reviewer
    # routing it for manual handling needs the original document, not just
    # the fact that nothing could be read from it.
    if pdf_bytes:
        _persist_document(run_id, filename, pdf_bytes, uploaded_by, source)
    yield sse("final", {"result": {
        "run_id": run_id, "filename": filename, "status": status, "reasons": reasons,
        "extracted": extracted, "po_match": po_match, "stages": stages,
    }})


async def run_pipeline(filename: str, pdf_bytes: bytes, uploaded_by: str = None,
                       source: str = "MANUAL_UPLOAD"):
    stages = []
    # Timing measures real work only. The small asyncio.sleep pauses between stages
    # exist so a human can watch the run unfold; they are deliberately excluded so
    # the reported per-stage ms reflect actual processing cost.
    clock = {"t": time.perf_counter()}

    def stage(name, status, detail):
        now = time.perf_counter()
        s = {"name": name, "status": status, "detail": detail,
             "ms": int((now - clock["t"]) * 1000)}
        clock["t"] = now
        stages.append(s)
        return s

    def mark():
        """Reset the stage clock after a pacing sleep so it isn't counted."""
        clock["t"] = time.perf_counter()

    # 1. INGEST
    size_kb = round(len(pdf_bytes) / 1024, 1)
    yield sse("stage", {"stage": stage("INGEST", "ok", f"Received \"{filename}\" ({size_kb} KB).")})
    await asyncio.sleep(0.25)
    mark()

    # 2. EXTRACT_TEXT -- read the embedded text layer only. Which extraction route
    # that implies is decided in the next stage.
    try:
        text, page_count, has_text_layer = extraction.extract_text(pdf_bytes)
    except extraction.PdfUnreadable as exc:
        yield sse("stage", {"stage": stage("EXTRACT_TEXT", "fail", str(exc))})
        async for evt in _abort_unreadable(filename, str(exc), stages, stage,
                                           pdf_bytes=pdf_bytes, uploaded_by=uploaded_by,
                                           source=source):
            yield evt
        return

    pages_note = f"{page_count} page{'s' if page_count != 1 else ''}"
    if has_text_layer:
        detail = f"{pages_note}; extracted {len(text)} characters of embedded text."
        st = "ok"
    else:
        detail = f"{pages_note}; no embedded text layer (document looks scanned)."
        st = "warn"
    yield sse("stage", {"stage": stage("EXTRACT_TEXT", st, detail)})
    await asyncio.sleep(0.25)
    mark()

    # 3. EXTRACT_FIELDS -- LLM over text, LLM over page images, or regex.
    extracted_obj, extract_info = extraction.extract_invoice(
        pdf_bytes, pre=(text, page_count, has_text_layer))
    extracted = extracted_obj.to_dict()
    found = [k for k in ["vendor_name", "invoice_number", "invoice_date", "total"] if extracted.get(k)]
    if extract_info["route"] == "none":
        ef_status = "fail"
    elif found and extract_info["route"] != "regex":
        ef_status = "ok"
    else:
        ef_status = "ok" if found else "warn"
    yield sse("stage", {"stage": stage(
        "EXTRACT_FIELDS", ef_status,
        f"Route: {extracted['extraction_method']}. "
        f"Found: {', '.join(found) if found else 'nothing usable'}."
    )})
    await asyncio.sleep(0.3)
    mark()

    # 4. VALIDATE -- required fields, plus the security screen on what was read.
    # Both are reported here so an operator sees "this document tried something"
    # in the run view, not only in the final reasoning trail.
    missing = rules.validate_required_fields(extracted)
    arithmetic = rules.validate_arithmetic(extracted)
    amount = rules.validate_amount(extracted)
    low_confidence = rules.validate_confidence(extracted)
    security_flags = extract_info.get("security_flags") or []
    if security_flags:
        val_status = "fail"
        val_detail = (
            f"SECURITY: {len(security_flags)} field(s) contain instruction-like text "
            f"— {security_flags[0]}. Routed for human review."
        )
        if missing:
            val_detail += f" Also missing: {', '.join(missing)}."
        if amount:
            val_detail += f" Total is also invalid (${amount['total']:.2f})."
        if arithmetic:
            val_detail += f" Arithmetic also off by ${arithmetic['diff']:.2f}."
    elif missing:
        val_status = "fail"
        val_detail = f"Missing required field(s): {', '.join(missing)}."
    elif amount:
        val_status = "fail"
        val_detail = (f"Invalid invoice amount: total is ${amount['total']:.2f}; "
                      f"it must be greater than zero.")
    elif arithmetic:
        val_status = "fail"
        val_detail = (
            f"Arithmetic mismatch: subtotal ${arithmetic['subtotal']:.2f} + tax "
            f"${arithmetic['tax']:.2f} = ${arithmetic['expected']:.2f}, but the invoice "
            f"states ${arithmetic['total']:.2f} (off by ${arithmetic['diff']:.2f})."
        )
    elif low_confidence:
        val_status = "warn"
        val_detail = "Low extraction confidence: " + ", ".join(
            f"{f['field']} ({f['confidence'] * 100:.0f}%)" for f in low_confidence
        ) + ". Held for review rather than trusted outright."
    else:
        val_status = "ok"
        val_detail = "All required fields present; arithmetic consistent; no injection patterns detected."
    yield sse("stage", {"stage": stage("VALIDATE", val_status, val_detail)})
    await asyncio.sleep(0.25)
    mark()

    # 5. VENDOR_CHECK
    vendor_ok, vendor_row, vendor_detail = rules.vendor_check(extracted)
    vendor_stage_status = "ok" if vendor_ok else ("warn" if vendor_ok is None else "fail")
    yield sse("stage", {"stage": stage("VENDOR_CHECK", vendor_stage_status, vendor_detail)})
    await asyncio.sleep(0.25)
    mark()

    # 6. PO_MATCH
    po_match = matching.match_po(extracted)
    if po_match["po_number"] is None:
        pm_detail = "No matching purchase order found."
        pm_status = "fail"
    elif po_match.get("is_multi"):
        # Name every PO and the balance each brings, so the live view shows the
        # invoice spanning them rather than appearing to bind just the first.
        pm_detail = (
            f"Matched {len(po_match['po_numbers'])} POs "
            f"({', '.join(po_match['po_numbers'])}); combined remaining balance before "
            f"this invoice: ${po_match['remaining_before']:.2f}."
        )
        pm_status = "warn"
    else:
        pm_detail = (
            f"Matched {po_match['po_number']} ({po_match['matched_via']}); "
            f"remaining balance before this invoice: ${po_match['remaining_before']:.2f}."
        )
        pm_status = "ok" if po_match["po_status"] == "open" else "warn"
    yield sse("stage", {"stage": stage("PO_MATCH", pm_status, pm_detail)})
    await asyncio.sleep(0.3)
    mark()

    # 7. DUPLICATE_CHECK
    dup_row, dup_detail = rules.duplicate_check(extracted)
    yield sse("stage", {"stage": stage("DUPLICATE_CHECK", "fail" if dup_row else "ok", dup_detail)})
    await asyncio.sleep(0.25)
    mark()

    # 8. TOLERANCE_CHECK
    if po_match["po_number"] is not None:
        if po_match.get("is_multi"):
            # The comparison is against the COMBINED balance, so say so and show
            # the proposed split -- "within tolerance" against a sum of POs would
            # otherwise read as though one PO had covered the invoice.
            tol_detail = (
                "Split across "
                + ", ".join(f"{a['po_number']} ${a['amount']:.2f}"
                            for a in po_match["allocations"])
                + f" against ${po_match['remaining_before']:.2f} combined remaining. "
                  f"Calculated, not stated on the invoice — held for confirmation."
            )
            tol_status = "warn"
        elif po_match["within_tolerance"] and po_match["is_partial"]:
            tol_detail = f"Diff ${po_match['diff']:.2f} — partial invoice, within remaining PO balance."
            tol_status = "ok"
        else:
            tol_detail = (
                f"Diff ${po_match['diff']:.2f} vs tolerance ${po_match['tolerance']:.2f} "
                f"({'within' if po_match['within_tolerance'] else 'OUTSIDE'} tolerance)."
            )
            tol_status = "ok" if po_match["within_tolerance"] else "fail"
    else:
        tol_detail = "Skipped — no PO to compare against."
        tol_status = "warn"
    yield sse("stage", {"stage": stage("TOLERANCE_CHECK", tol_status, tol_detail)})
    await asyncio.sleep(0.25)
    mark()

    # 9. DECISION
    #
    # `audit` is filled in by the same evaluation that produces the status --
    # not by a second pass over the result -- so the structured trail and the
    # verdict cannot disagree.
    audit = {}
    status, reasons = rules.decide(
        extract_info, missing, vendor_ok, vendor_detail, dup_row, dup_detail, po_match,
        arithmetic=arithmetic, amount=amount, audit=audit, extracted=extracted,
        low_confidence=low_confidence,
    )
    yield sse("stage", {"stage": stage("DECISION", "ok", f"Final status: {status}.")})
    await asyncio.sleep(0.15)
    mark()

    # Commit under the ledger write lock, which re-verifies the PO balance and
    # downgrades a stale APPROVED rather than overspending the PO.
    run_id, status, extra = storage.save_run_checked(
        filename, status, extracted, po_match, stages, reasons,
        tolerance_for=matching.tolerance_for, audit=audit, uploaded_by=uploaded_by)
    if extra:
        reasons = list(reasons) + [extra]
        # save_run_checked re-checked the balance under the write lock and
        # downgraded this run; re-read the stored trail so the response carries
        # the version that was actually committed.
        stored = storage.get_run(run_id)
        if stored and stored.get("audit"):
            audit = stored["audit"]

    _persist_document(run_id, filename, pdf_bytes, uploaded_by, source)

    result = {
        "run_id": run_id,
        "filename": filename,
        "status": status,
        "reasons": reasons,
        "extracted": extracted,
        "po_match": po_match,
        "stages": stages,
        "audit": audit,
    }
    yield sse("final", {"result": result})


# --------------------------------------------------------------------------
# authentication endpoints
# --------------------------------------------------------------------------

@app.get("/api/health")
def health():
    """Public liveness probe. Says nothing about configuration, versions, which
    providers are reachable or whether keys are present -- all of which would be
    useful to someone deciding whether this host is worth attacking."""
    return {"status": "ok"}


@app.post("/api/auth/token", dependencies=[Depends(ratelimit.rate_limit_login)])
def issue_token(form: OAuth2PasswordRequestForm = Depends()):
    """OAuth 2.0 password grant. Returns a signed bearer token and its scopes.

    Rate limited per IP: this is the only endpoint that accepts a password, so
    it is the only one where an unlimited caller is guessing rather than
    scraping. One message for bad user and bad password alike.
    """
    user = auth.authenticate_user(form.username, form.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Incorrect username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    return auth.create_access_token(user)


@app.get("/api/auth/me")
def whoami(principal: auth.Principal = Security(auth.current_principal)):
    """Who the token says you are, and what it permits. The UI uses this to
    decide which controls to render -- a convenience, never a control: every
    endpoint re-checks the scope itself."""
    return {"username": principal.username, "roles": principal.roles,
            "scopes": principal.scopes}


# --------------------------------------------------------------------------
# upload validation
# --------------------------------------------------------------------------
_PDF_MAGIC = b"%PDF-"


def _safe_filename(name: str) -> str:
    """A filename safe to store and display.

    The uploaded name is attacker-controlled and ends up in the database and on
    screen, so any directory component is stripped (a client can send
    "../../x.pdf"), control characters are removed, and the length is bounded.
    """
    name = os.path.basename((name or "").replace("\\", "/"))
    name = "".join(ch for ch in name if ch.isprintable() and ch not in r'\/:*?"<>|')
    name = name.strip(". ") or "upload.pdf"
    return name[:180]


async def _read_capped(file: UploadFile) -> bytes:
    """Read the upload, refusing anything past the configured cap.

    Read in chunks and stop at the limit rather than `await file.read()` -- the
    latter buys the whole body into memory before anyone checks how big it is,
    which turns the size limit into a formality. Content-Length is not trusted
    for this; it is a client-supplied header.
    """
    limit = config.MAX_UPLOAD_BYTES
    chunks, total = [], 0
    while True:
        chunk = await file.read(1024 * 256)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds the {limit // (1024 * 1024)} MB upload limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_pdf(data: bytes):
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Empty file")
    # Checked by content, not by extension or by the client's Content-Type --
    # both are trivially set to whatever the caller likes.
    if not data.startswith(_PDF_MAGIC):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF files are accepted")


@app.post("/api/runs/stream")
async def create_run_stream(
    file: UploadFile = File(...),
    principal: auth.Principal = Depends(ratelimit.rate_limit_processing),
):
    """Process an invoice. Requires 'invoice:process' and is rate limited.

    The dependency does authentication, authorization and rate limiting in that
    order, before a single byte is read -- so an unauthorised caller cannot make
    this endpoint do any work, let alone spend extraction quota.
    """
    pdf_bytes = await _read_capped(file)
    _validate_pdf(pdf_bytes)
    return StreamingResponse(
        _guarded_pipeline(_safe_filename(file.filename), pdf_bytes, principal.username),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _guarded_pipeline(filename: str, pdf_bytes: bytes, uploaded_by: str):
    """Run the pipeline, converting any unexpected failure into a safe event.

    The exception handlers above cannot help here: by the time the generator
    runs, the 200 and the headers are already on the wire, so a crash mid-stream
    cannot become a 500 -- it just severs the connection, and on some servers
    spills a traceback into the response body. Catching it here means the client
    gets one clean event and the detail stays in the server log.
    """
    try:
        # Every upload through this endpoint is, definitionally, a manual
        # upload -- it is a human at a browser posting a file. EMAIL is a
        # source this schema already recognises (config.DOCUMENT_SOURCES) for
        # when Phase J's ingestion path exists, but nothing writes it yet.
        async for event in run_pipeline(filename, pdf_bytes, uploaded_by=uploaded_by,
                                        source="MANUAL_UPLOAD"):
            yield event
    except Exception as exc:
        print(f"[error] pipeline failed on {filename!r}: {exc.__class__.__name__}",
              file=sys.stderr)
        yield sse("error", {"error": "Processing failed. The run was not completed."})


@app.get("/api/runs")
def get_runs(principal: auth.Principal = Security(auth.current_principal,
                                                  scopes=["invoice:read"])):
    return storage.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: int,
            principal: auth.Principal = Security(auth.current_principal,
                                                 scopes=["invoice:read"])):
    """A single run, including its audit trail. Reading a decision trail means
    reading vendor names, amounts and PO balances, so it needs the same
    permission as the rest of the invoice data."""
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


def _get_run_document_or_404(run_id: int):
    """The document row for a run, or raise. Shared by the metadata and
    download endpoints so a run that does not exist and a run with no stored
    document are told apart identically in both places."""
    if not storage.get_run(run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    doc = storage.get_document_for_run(run_id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No document is stored for this run")
    return doc


@app.get("/api/runs/{run_id}/document")
def get_run_document(run_id: int,
                     principal: auth.Principal = Security(auth.current_principal,
                                                          scopes=["invoice:read"])):
    """Metadata for the PDF a run was processed from.

    Same permission as reading the run itself -- a document is invoice data,
    not a separate resource with its own authorization story. Deliberately
    never includes `storage_backend` or `storage_key`: those name where the
    file physically lives, which is nobody's business outside this process.
    """
    doc = _get_run_document_or_404(run_id)
    storage.log_activity(run_id, "DOCUMENT_VIEWED", actor=principal.username)
    return {
        "id": doc["id"],
        "run_id": doc["run_id"],
        "original_filename": doc["original_filename"],
        "mime_type": doc["mime_type"],
        "size_bytes": doc["size_bytes"],
        "sha256": doc["sha256"],
        "uploaded_by": doc["uploaded_by"],
        "uploaded_at": doc["uploaded_at"],
        "source": doc["source"],
    }


@app.get("/api/runs/{run_id}/document/download")
def download_run_document(run_id: int, inline: bool = False,
                          principal: auth.Principal = Security(auth.current_principal,
                                                               scopes=["invoice:read"])):
    """The invoice PDF itself, for viewing or downloading.

    Authorization is checked (the Security dependency above) before the
    handler body runs at all -- before the document row is even looked up,
    let alone the file read off disk or a bucket -- so an unauthorised
    caller learns nothing about whether run_id even has a document.

    `inline=1` asks for `Content-Disposition: inline`, which is what an
    embedded viewer (an <object>/<iframe> in the browser) needs to render the
    PDF instead of prompting a download; the default is `attachment`, for an
    explicit "download" action. Either way the bytes served are the same
    real file that was uploaded -- there is no placeholder path here.
    """
    doc = _get_run_document_or_404(run_id)
    try:
        data = documents.get_store().read(doc["storage_key"])
    except (FileNotFoundError, OSError, ValueError):
        # The metadata survived but the content did not (disk cleared by
        # hand, bucket object expired, backend switched mid-project). Say so
        # without naming a path or a bucket.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Document content is no longer available")

    storage.log_activity(run_id, "DOCUMENT_DOWNLOADED", actor=principal.username,
                         metadata={"inline": inline})

    # original_filename was already reduced to a safe, control-character-free
    # basename at upload time (main.py's _safe_filename) before it was ever
    # stored, so it is safe to place directly in this header.
    filename = doc["original_filename"] or f"invoice-{run_id}.pdf"
    disposition = "inline" if inline else "attachment"
    return Response(
        content=data,
        media_type=doc["mime_type"] or "application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@app.post("/api/runs/{run_id}/status")
def change_run_status(run_id: int, payload: dict = Body(...),
                      principal: auth.Principal = Security(auth.current_principal,
                                                           scopes=["invoice:admin"])):
    """Change a run's status, then re-evaluate anything queued on the same PO.

    This is the reversal path. There is no balance to refund: consumption is
    derived from APPROVED runs, so moving a run out of APPROVED frees its budget
    the moment the row is updated. Freed budget then cascades to invoices that
    were held only because the PO was exhausted.
    """
    new_status = (payload or {}).get("status")
    note = (payload or {}).get("note")
    # Attribute the override to the authenticated caller, not to a name they
    # supplied. This is the broad override path, so who used it matters most.
    if note:
        note = f"{note} (by {principal.username})"
    ok, old_status, po_number = storage.set_run_status(run_id, new_status, note)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Unknown run, or invalid status")
    if old_status != new_status:
        storage.log_activity(run_id, "STATUS_OVERRIDDEN", actor=principal.username,
                             note=note, metadata={"from": old_status, "to": new_status})

    cascaded = []
    if po_number and old_status != new_status:
        cascaded = rules.reevaluate_po_queue(po_number, triggered_by=run_id)

    return {
        "run_id": run_id,
        "from": old_status,
        "to": new_status,
        "po_number": po_number,
        "remaining_after": storage.remaining_for_po(po_number) if po_number else None,
        "cascaded": cascaded,
    }


@app.post("/api/admin/reset-demo")
def reset_demo_data(principal: auth.Principal = Security(auth.current_principal,
                                                         scopes=["invoice:admin"])):
    """Clear processed-run history so the sample invoices are repeatable.

    The samples are history-dependent by design, so a second pass through them
    turns the happy path into a duplicate of itself and leaves PO-1001 with no
    budget. The verdicts stay correct; the demonstration stops working. This is
    the supported way to get back to a clean slate without shell access.

    Scoped to invoice:admin, and deliberately narrow: it deletes runs only.
    Purchase orders, vendors and users are seed data owned by data/*.json, so
    nothing here destroys anything that re-running an invoice cannot rebuild.
    """
    deleted = storage.clear_run_history()
    return {"ok": True, "deleted": deleted, "by": principal.username}


@app.post("/api/runs/{run_id}/review")
def review_run(run_id: int, payload: dict = Body(...),
               principal: auth.Principal = Security(auth.current_principal,
                                                    scopes=["invoice:review"])):
    """Record a human ruling on a run the process held for review.

    Body: {"decision": "ACCEPTED"|"REJECTED", "reviewer": str, "note": str}

    This is the human-in-the-loop path, and it is deliberately not the same
    endpoint as /status. /status is an operator changing a run's state (a
    reversal, a correction). This one asserts something stronger: that a person
    looked at the audit trail for an invoice the rules would not clear, and took
    responsibility for the outcome. It records who and when, and it leaves the
    automated decision exactly where it was.

    Only runs whose AUTOMATED decision was NEEDS_REVIEW are eligible; the
    storage layer enforces that rather than trusting the caller.
    """
    # The reviewer is the authenticated principal, FULL STOP. Any "reviewer"
    # field in the body is ignored: an audit record that says whatever the
    # client typed is not evidence of anything, and this is the one action in
    # the system that moves money against the process's own judgement.
    result = storage.record_human_review(
        run_id,
        (payload or {}).get("decision"),
        reviewer=principal.username,
        note=(payload or {}).get("note"),
    )
    if not result.get("ok"):
        err = result["error"]
        if err == "claimed":
            # Someone else is actively reviewing this run. Told as a rich 409,
            # not a bare string, so the caller can render "currently being
            # reviewed by X" without a second lookup.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": f"This invoice is currently being reviewed by "
                                 f"{result['claimed_by']}.",
                       "claimed_by": result["claimed_by"], "expires_at": result["expires_at"]})
        # 404 for a run that does not exist, 409 for one that exists but is not
        # in a reviewable state -- a caller can act on the difference.
        code = (status.HTTP_404_NOT_FOUND if "unknown run" in err
                else status.HTTP_409_CONFLICT
                if ("NEEDS_REVIEW" in err or "already been reviewed" in err)
                else status.HTTP_400_BAD_REQUEST)
        raise HTTPException(status_code=code, detail=result["error"])

    # Accepting an invoice consumes PO budget; rejecting one releases it. Either
    # way the queue behind that PO may now resolve differently, so re-evaluate it
    # exactly as the reversal path does.
    po_number = result.get("po_number")
    result["cascaded"] = (
        rules.reevaluate_po_queue(po_number, triggered_by=run_id) if po_number else []
    )
    result["remaining_after"] = storage.remaining_for_po(po_number) if po_number else None
    result["run"] = storage.get_run(run_id)
    return result


@app.post("/api/runs/{run_id}/review/claim")
def claim_run_review(run_id: int,
                     principal: auth.Principal = Security(auth.current_principal,
                                                          scopes=["invoice:review"])):
    """Claim exclusive ownership of a NEEDS_REVIEW run for human review.

    One employee at a time: enforced by a row lock in the database
    (storage.claim_review), not a frontend timer or an in-memory flag, so two
    people racing this endpoint for the same run cannot both win. A claim
    carries a lease (config.review_claim_lease_minutes()) so a closed tab or a
    lost connection does not block the invoice forever -- the next claim
    attempt after it expires simply takes over. Claiming again with the same
    identity while already holding the claim renews the lease rather than
    conflicting with itself, so a retried or double-submitted request is safe.
    """
    result = storage.claim_review(run_id, principal.username)
    if not result.get("ok"):
        err = result.get("error", "")
        if err == "claimed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": f"This invoice is currently being reviewed by "
                                 f"{result['claimed_by']}.",
                       "claimed_by": result["claimed_by"], "expires_at": result["expires_at"]})
        code = status.HTTP_404_NOT_FOUND if "unknown run" in err else status.HTTP_409_CONFLICT
        raise HTTPException(status_code=code, detail=err)
    return result


@app.post("/api/runs/{run_id}/review/release")
def release_run_review(run_id: int,
                       principal: auth.Principal = Security(auth.current_principal,
                                                            scopes=["invoice:review"])):
    """Release a review claim. Only the claim's own holder may release it,
    unless the caller also has 'invoice:admin' -- the same override authority
    that scope already carries for /status, here used to free a claim left by
    someone who is no longer available to release it themselves."""
    result = storage.release_review_claim(
        run_id, principal.username, is_admin=principal.has("invoice:admin"))
    if not result.get("ok"):
        err = result.get("error", "")
        code = (status.HTTP_404_NOT_FOUND if "unknown run" in err
               else status.HTTP_409_CONFLICT)
        raise HTTPException(status_code=code, detail=err)
    return result


@app.post("/api/runs/{run_id}/comment")
def add_run_comment(run_id: int, payload: dict = Body(...),
                    principal: auth.Principal = Security(auth.current_principal,
                                                         scopes=["invoice:review"])):
    """Add a note to a run's activity history without ruling on it -- for a
    reviewer flagging something mid-review, before an accept or reject.
    Requires the same 'invoice:review' scope as claiming and deciding: a
    comment is part of the review workflow, not general-purpose annotation."""
    note = (payload or {}).get("note")
    result = storage.add_comment(run_id, principal.username, note)
    if not result.get("ok"):
        err = result.get("error", "")
        code = status.HTTP_404_NOT_FOUND if "unknown run" in err else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=err)
    return result


@app.get("/api/runs/{run_id}/activity")
def get_run_activity(run_id: int,
                     principal: auth.Principal = Security(auth.current_principal,
                                                          scopes=["invoice:read"])):
    """Chronological activity for a run -- who did what, and when -- plus who,
    if anyone, currently holds its review claim.

    Deliberately distinct from the run's own audit trail (`GET /api/runs/{id}`):
    that explains why the DETERMINISTIC RULES reached the verdict they did.
    This explains what PEOPLE (and the system, acting on their behalf) did
    about it afterwards -- claimed it, released it, commented, decided,
    viewed the source document. Same permission as reading the run itself:
    an activity history is invoice data, not a separately-permissioned
    resource, matching how the document endpoints already treat 'invoice:read'.
    """
    if not storage.get_run(run_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return {
        "run_id": run_id,
        "activity": storage.list_activity(run_id),
        "current_claim": storage.get_active_claim(run_id),
    }


# --------------------------------------------------------------------------
# Email security & trusted-source verification (Phase F)
#
# WHAT THESE ENDPOINTS ARE, AND WHAT THEY ARE NOT
#
# They verify a message that is HANDED to this process. They do not connect
# to a mailbox, poll IMAP, or fetch anything -- that is Phase G, and this is
# the seam it plugs into: whatever transport eventually retrieves a message
# hands the raw bytes to the same `email_security.classify()` these endpoints
# call, and gets the same verdict, because the verdict depends only on the
# bytes and on configuration.
#
# Nothing here creates a run or processes an attachment. An ADMITTED message
# is one that is *allowed* to be processed; actually processing it is the
# next phase.
#
# AUTHORIZATION reuses the scopes that already exist rather than inventing an
# email-specific one: submitting a message is ingestion (invoice:process),
# reading its record is reading invoice data (invoice:read), and releasing a
# quarantined message is a hold/release ruling (invoice:review) -- the same
# authority that accepts or rejects a NEEDS_REVIEW invoice.
# --------------------------------------------------------------------------
async def _read_capped_message(file: UploadFile) -> bytes:
    """Read a submitted message, refusing anything past the configured cap.

    Same chunked approach as `_read_capped` and for the same reason -- the
    cap has to be enforced while reading, not after -- but against
    `config.email_max_message_bytes()`, since a message carrying a PDF is
    necessarily larger than the PDF alone.
    """
    limit = config.email_max_message_bytes()
    chunks, total = [], 0
    while True:
        chunk = await file.read(1024 * 256)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Message exceeds the {limit // (1024 * 1024)} MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/email/messages")
async def submit_email_message(
    file: UploadFile = File(...),
    principal: auth.Principal = Depends(ratelimit.rate_limit_processing),
):
    """Verify an incoming RFC 5322 message and record what could be proven.

    Rate limited and scoped exactly like invoice processing: this is the
    ingestion door, and an unauthorised caller must not be able to make it do
    work. The verdict is deterministic -- the same bytes and the same
    configuration always produce the same classification -- which is why a
    byte-identical resubmission returns the existing record rather than
    writing a second one. A retried or double-clicked submission is therefore
    safe, and a genuine replay is visible in the message's history rather
    than hidden behind a duplicate row.
    """
    raw = await _read_capped_message(file)
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty message")

    try:
        record = email_security.classify(raw, trusted_senders=storage.list_trusted_senders())
    except Exception as exc:
        # A malformed or hostile message must produce a verdict or a clean
        # 400 -- never a 500 that tells the sender they found a crash.
        print(f"[error] email verification failed: {exc.__class__.__name__}", file=sys.stderr)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="The message could not be evaluated")

    existing = storage.find_email_by_sha256(record["sha256"])
    if existing:
        storage.log_email_activity(existing["id"], "MESSAGE_RESUBMITTED",
                                   principal.username,
                                   metadata={"sha256": record["sha256"]})
        return {"duplicate": True, "message": storage.get_email_message(existing["id"])}

    email_id = storage.save_email_message(record, submitted_by=principal.username,
                                          source="SUBMITTED")
    return {"duplicate": False, "message": storage.get_email_message(email_id)}


@app.get("/api/email/messages")
def list_email_messages(status_filter: str = None,
                        principal: auth.Principal = Security(auth.current_principal,
                                                             scopes=["invoice:read"])):
    """Every message evaluated, newest first. Summary rows only -- the full
    authentication evidence is on the individual record."""
    if status_filter and status_filter.upper() not in config.EMAIL_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Unknown status {status_filter!r}")
    return storage.list_email_messages(
        status=status_filter.upper() if status_filter else None)


@app.get("/api/email/messages/{email_id}")
def get_email_message(email_id: int,
                      principal: auth.Principal = Security(auth.current_principal,
                                                           scopes=["invoice:read"])):
    """One message: the verdict, the full authentication evidence behind it,
    and what people have done about it.

    The evidence is the point. It carries the authentication headers that
    were believed, the ones that were DISCARDED as coming from an untrusted
    boundary, the DKIM signature parameters, and the alignment arithmetic --
    everything an auditor needs to re-derive the verdict without the original
    message, which this system deliberately did not keep.
    """
    message = storage.get_email_message(email_id)
    if not message:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    message["activity"] = storage.list_email_activity(email_id)
    return message


@app.post("/api/email/messages/{email_id}/release")
def release_email_message(email_id: int, payload: dict = Body(default=None),
                          principal: auth.Principal = Security(auth.current_principal,
                                                               scopes=["invoice:review"])):
    """Release a quarantined message: a person accepts responsibility for it.

    Requires 'invoice:review' -- the same authority that accepts a
    NEEDS_REVIEW invoice, because it is the same kind of act. Deliberately
    NOT automatic for any classification: an UNVERIFIED message is held
    because nothing could be checked, and the resolution to "nothing could be
    checked" is a human looking, not a rule that eventually gives up and lets
    it through.
    """
    note = ((payload or {}).get("note") or "").strip() or None
    result = storage.set_email_status(email_id, "RELEASED", principal.username, note)
    if not result.get("ok"):
        err = result.get("error", "")
        code = (status.HTTP_404_NOT_FOUND if "unknown message" in err
               else status.HTTP_409_CONFLICT)
        raise HTTPException(status_code=code, detail=err)
    return result


@app.post("/api/email/messages/{email_id}/discard")
def discard_email_message(email_id: int, payload: dict = Body(default=None),
                          principal: auth.Principal = Security(auth.current_principal,
                                                               scopes=["invoice:review"])):
    """Discard a quarantined message. Terminal: it can never become a run."""
    note = ((payload or {}).get("note") or "").strip() or None
    result = storage.set_email_status(email_id, "DISCARDED", principal.username, note)
    if not result.get("ok"):
        err = result.get("error", "")
        code = (status.HTTP_404_NOT_FOUND if "unknown message" in err
               else status.HTTP_409_CONFLICT)
        raise HTTPException(status_code=code, detail=err)
    return result


@app.get("/api/email/trusted-senders")
def get_trusted_senders(principal: auth.Principal = Security(auth.current_principal,
                                                             scopes=["invoice:read"])):
    """The configured trusted-sender allowlist, and how message verification
    is currently set up.

    `trusted_authserv_ids` being empty is worth surfacing rather than hiding:
    it is the difference between "this deployment can recognise an
    authenticated sender" and "every message will read UNVERIFIED", and an
    operator should be able to see which one they have without reading the
    source. No secret is exposed -- an authserv-id is a hostname, and the
    resolver name is a class name.
    """
    return {
        "senders": storage.list_trusted_senders(),
        "verification": {
            "trusted_authserv_ids": list(config.email_trusted_authserv_ids()),
            "dns_resolver": config.email_dns_resolver(),
            "signature_verifier": config.email_signature_verifier(),
            "classifications": list(config.EMAIL_CLASSIFICATIONS),
            "statuses": list(config.EMAIL_STATUSES),
        },
    }


@app.get("/api/reference")
def get_reference(principal: auth.Principal = Security(auth.current_principal,
                                                       scopes=["invoice:read"])):
    return {"purchase_orders": storage.list_purchase_orders(), "vendors": storage.list_vendors()}


@app.get("/api/sample-invoices")
def list_sample_invoices(principal: auth.Principal = Security(auth.current_principal,
                                                              scopes=["invoice:read"])):
    """Sample PDFs plus, where available, the scenario metadata from manifest.json
    so the UI can show what each file is meant to demonstrate."""
    d = os.path.join(os.path.dirname(__file__), "..", "sample_invoices")
    if not os.path.isdir(d):
        return []
    manifest = {}
    mpath = os.path.join(d, "manifest.json")
    if os.path.isfile(mpath):
        try:
            with open(mpath, encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            manifest = {}

    # One sample's expected verdict depends on which extraction route is live:
    # an image-only PDF goes to review when there is no key to read it with, and
    # is approved when the vision route can. Show what will actually happen, so
    # the badge in the UI never contradicts the run sitting next to it.
    vision = config.has_api_key()

    items = []
    for name in os.listdir(d):
        if not name.lower().endswith(".pdf"):
            continue
        meta = manifest.get(name, {})
        expect = meta.get("expect")
        if vision and meta.get("expect_with_vision"):
            expect = meta["expect_with_vision"]
        items.append({
            "filename": name,
            "label": meta.get("label"),
            "note": meta.get("note"),
            "expect": expect,
            "order": meta.get("order", 999),
        })
    items.sort(key=lambda i: (i["order"], i["filename"]))
    return items


@app.get("/api/sample-invoices/{name}")
def get_sample_invoice(name: str,
                       principal: auth.Principal = Security(auth.current_principal,
                                                            scopes=["invoice:read"])):
    """Serve one sample PDF by name.

    `name` is caller-controlled and was being joined straight onto a directory,
    which on Windows let a backslash-separated parent reference walk out of the
    samples folder and read any PDF on the host. Verified before the fix: a name
    of the form "..<sep>data<sep>x.pdf" resolved outside the directory entirely.
    Now the name is reduced to its basename and the
    resolved path is required to sit inside the samples directory -- belt and
    braces, because basename alone is easy to reintroduce a hole around.
    """
    from fastapi.responses import FileResponse
    d = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sample_invoices"))
    safe = os.path.basename(name.replace("\\", "/"))
    path = os.path.abspath(os.path.join(d, safe))
    if (os.path.commonpath([d, path]) != d
            or not safe.lower().endswith(".pdf")
            or not os.path.isfile(path)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sample not found")
    return FileResponse(path, media_type="application/pdf")


class _AppShell(StaticFiles):
    """Static files, except the HTML shell is never cached.

    The hashed /_next/* assets are immutable and SHOULD be cached hard. The
    entry document must not be: it is the file that names which hashed bundle
    to load, so a cached copy pins the browser to a build that no longer exists
    on disk. That produced a genuinely confusing failure twice -- the server
    serving a new UI while the browser kept rendering the old one, with no
    error anywhere to explain the disagreement.

    Scoped to the static mount on purpose. A blanket HTTP middleware would sit
    in front of the SSE endpoint too, and nothing is worth risking the stream
    the whole demo runs on.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        if str(resp.media_type or "").startswith("text/html"):
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp


app.mount("/", _AppShell(directory=FRONTEND_DIR, html=True), name="frontend")
