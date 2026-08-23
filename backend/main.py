"""FastAPI app: invoice processing pipeline with a live (SSE) run view + dashboard."""
import asyncio
import hashlib
import json
import time
import os
import sys
import uuid
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import (Body, Depends, FastAPI, File, HTTPException, Query, Request,
                     Security, UploadFile, status)
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders
from fastapi.responses import (JSONResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles

import analytics
import audit_export
import auth
import chat
import config
import doclang
import documents
import i18n
import email_ingest
import jobs
import email_outbound
import email_security
import extraction
import logs
import matching
import notifications
import oauth_google
import portal
import quota
import ratelimit
import rules
import storage
from schemas import ExtractedInvoice

app = FastAPI(title="Invoice Processing")


# --------------------------------------------------------------------------
# HTTP security headers (Phase K)
#
# This process serves its own UI (the static export is mounted at "/" at the
# bottom of this file), so the browser-side protections for that UI are this
# application's job. Before Phase K, no response carried any of them: the app
# could be framed by any site (clickjacking the accept/reject controls, which
# are one click and move money), responses could be MIME-sniffed, and full
# URLs -- including run ids -- were sent as the Referer to anywhere a user
# navigated next.
#
# WRITTEN AS RAW ASGI, NOT AS @app.middleware("http"), AND THAT IS DELIBERATE.
# The existing comment on the static mount records why a blanket HTTP
# middleware was avoided in the first place: Starlette's BaseHTTPMiddleware
# wraps the response body in its own stream, which is exactly the machinery
# the SSE run view depends on and the last thing worth risking. This class
# never touches the body. It intercepts one message -- http.response.start --
# sets headers that are not already present, and passes everything through
# untouched, so a streaming response streams exactly as it did before.
#
# Existing headers are never overwritten: the app shell sets its own
# Cache-Control, and a proxy in front may set its own policy.
# --------------------------------------------------------------------------

_STATIC_SECURITY_HEADERS = {
    # Stop the browser second-guessing a Content-Type. Without it, a response
    # this app labels application/json can be sniffed into something
    # executable if an attacker controls enough of the body.
    "X-Content-Type-Options": "nosniff",
    # Never send this app's URLs to another origin. Run ids, filter strings
    # and search terms all live in query strings here.
    "Referrer-Policy": "no-referrer",
    # Anti-clickjacking, twice over: X-Frame-Options for older browsers, and
    # frame-ancestors inside the CSP for current ones. Approving an invoice is
    # a single click, which is precisely what a framed UI monetises.
    "X-Frame-Options": "DENY",
    # Sever the window.opener relationship with anything that opens this app.
    "Cross-Origin-Opener-Policy": "same-origin",
    # This application asks for no device permissions at all, so it says so
    # rather than leaving the defaults to whatever the browser decides.
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


class SecurityHeaders:
    """Adds the headers above to every HTTP response, without touching bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not config.SECURITY_HEADERS_ENABLED:
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _STATIC_SECURITY_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
                if config.CONTENT_SECURITY_POLICY and \
                        "Content-Security-Policy" not in headers:
                    headers["Content-Security-Policy"] = config.CONTENT_SECURITY_POLICY
                # HSTS is production-only and is the one header here that is
                # hard to take back: a browser told to pin https:// for a year
                # will refuse plain http to that host for a year. On a
                # developer's laptop, served over http, that would break the
                # machine rather than protect it.
                if config.is_production() and config.HSTS_MAX_AGE_SECONDS > 0 \
                        and "Strict-Transport-Security" not in headers:
                    headers["Strict-Transport-Security"] = (
                        f"max-age={config.HSTS_MAX_AGE_SECONDS}; includeSubDomains")
            await send(message)

        await self.app(scope, receive, _send)


app.add_middleware(SecurityHeaders)

# CORS is configured, never relied on. It is enforced by browsers and ignored
# entirely by curl or a script, so it is not a security boundary -- the bearer
# token is. Default is same-origin (no allowed origins at all), which is how
# the app is actually served; CORS_ORIGINS opts specific origins in
# deliberately.
#
# THE ORIGINS ARE READ AT REQUEST TIME, NOT AT IMPORT (Phase K).
#
# `app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS)` binds
# that list when this module is imported -- which is BEFORE `load_dotenv()` has
# ever run, so `CORS_ORIGINS` set in .env configured precisely nothing, and the
# production start-up check that refuses a wildcard origin was inspecting a
# value .env could not influence.
#
# The obvious fix -- load .env at import -- was tried and reverted, because it
# also front-loads the provider API KEYS: importing this module would then mean
# a live provider is available, which changed the behaviour of test modules
# that had never asked for one. Configuration and secrets should not have to
# share a load order, so the middleware learns to re-read instead.

# Response headers a cross-origin browser is allowed to READ.
#
# Only relevant once the UI is on a different origin from the API. A browser
# hands JavaScript just seven safelisted response headers by default, and
# `Content-Disposition` is not one of them -- so `downloadFile()` in lib/api.ts,
# which reads the server-chosen filename out of it, would silently fall back to
# its generic name for every audit report and every document download. Same for
# `X-Export-Max-Rows`, which is how a scripted client tells a truncated log
# export from a complete one (§7d.8) without parsing the CSV.
#
# Exposing a header only lets the page READ what this server already sent it.
# Neither of these says anything the response body does not.
CORS_EXPOSE_HEADERS = ["Content-Disposition", "X-Export-Max-Rows"]


class ConfiguredCORS:
    """CORSMiddleware over the CURRENT `config.CORS_ORIGINS`.

    Delegates to a real CORSMiddleware, rebuilt only when the configured
    origins actually change -- so this costs one tuple comparison per request
    and none of Starlette's behaviour is reimplemented here. With nothing
    configured it steps out of the way entirely, which is exactly what the
    previous `if config.CORS_ORIGINS:` did.

    `config.CORS_ORIGIN_REGEX` is the second, optional half, and it exists for
    exactly one real problem: a hosted frontend mints a NEW origin per preview
    deployment, so those origins cannot be enumerated ahead of time. It is
    empty by default, it is never a substitute for naming the production
    origin, and `auth.validate_production_config()` refuses a production start
    with a pattern that would match any origin at all -- because a regex is a
    far quieter way to arrive at `allow_origins=["*"]` than typing it.
    """

    def __init__(self, app):
        self.app = app
        self._key = None
        self._impl = None

    def _delegate(self):
        origins = tuple(config.CORS_ORIGINS or ())
        pattern = config.CORS_ORIGIN_REGEX or ""
        key = (origins, pattern)
        if key != self._key:
            self._key = key
            self._impl = CORSMiddleware(
                self.app,
                allow_origins=list(origins),
                allow_origin_regex=pattern or None,
                allow_methods=["GET", "POST"],
                allow_headers=["Authorization", "Content-Type"],
                expose_headers=CORS_EXPOSE_HEADERS,
            ) if (origins or pattern) else None
        return self._impl or self.app

    async def __call__(self, scope, receive, send):
        await self._delegate()(scope, receive, send)


app.add_middleware(ConfiguredCORS)

# Which UI to serve.
#
# The Next.js app is a STATIC EXPORT: `npm run build` in frontend-next/ emits
# plain HTML/JS into out/, with no Node process at runtime. Serving it from here
# keeps the UI same-origin with the API, so the browser's relative /api/... calls
# resolve without CORS, a base URL, or a second port to get wrong.
#
# The original vanilla frontend has been removed -- frontend-next/ is the only
# UI now. Run `npm run build` inside frontend-next/ before starting the server.
#
# THE MOUNT IS CONDITIONAL, AND THAT IS A DEPLOYMENT FACT RATHER THAN A
# PREFERENCE. `out/` is a build artifact and is gitignored, so a checkout that
# has not been built -- which is exactly what a backend-only container image is
# -- does not have it, and `StaticFiles(directory=...)` raises at import time
# for a missing directory. That would take the API down for want of a UI it was
# never meant to serve.
#
# So: when out/ is present this process serves the UI exactly as it always did
# (one origin, one port, relative /api/... calls, no CORS) -- the local demo and
# the whole test suite are unchanged. When it is absent this is an API-only
# deployment, the UI is served by someone else (a CDN, a static host), and the
# browser reaches this origin cross-origin with CORS_ORIGINS naming it.
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend-next", "out")
SERVE_FRONTEND = os.path.isdir(FRONTEND_DIR)


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
    # A job still marked queued or running belongs to a process that no longer
    # exists -- the worker pool lives in THIS one, so nothing is working on it.
    # Failing it with a message beats reporting "processing" to a reader for
    # ever, and it is done before the first request is served so nobody can
    # read the stale state in between.
    try:
        abandoned = storage.abandon_stale_jobs(
            "Processing was interrupted because the server restarted. "
            "The invoice was not recorded; upload it again.")
        if abandoned:
            print("[startup] " + str(abandoned) + " interrupted processing job(s) "
                  "were closed out as failed", file=sys.stderr)
    except Exception as exc:
        # Never fatal. A database that cannot be written here is a problem the
        # very next request will report far better than a failed startup does.
        print("[startup] stale processing jobs could not be reconciled: "
              + exc.__class__.__name__, file=sys.stderr)
    if config.is_production():
        print(f"[startup] {config.APP_ENV_VAR}={config.app_env()} — production "
              f"configuration checks passed.", file=sys.stderr)
    # Phase G. Starts only when EMAIL_INGEST_ENABLED is set AND a provider is
    # configured, so an install that does not want email ingestion opens no
    # outbound connection. Safe in every uvicorn worker: duplicate suppression
    # is the database's UNIQUE (provider, provider_message_id), not
    # coordination between pollers.
    # Remembered here because this handler runs ON the event loop, and the
    # Gmail OAuth callback -- a sync endpoint, therefore a worker thread --
    # needs a way back to it to start polling a mailbox that was just
    # connected (§7h.8).
    email_ingest.remember_event_loop()
    try:
        if email_ingest.start_poller():
            print(f"[startup] email ingestion polling every "
                  f"{config.email_poll_seconds()}s via {config.email_provider()}",
                  file=sys.stderr)
    except Exception as exc:
        # A misconfigured mailbox must not stop the API serving invoices that
        # arrive by upload. It is reported and left off.
        print(f"[startup] email ingestion not started: {exc.__class__.__name__}: {exc}",
              file=sys.stderr)


@app.on_event("shutdown")
def _shutdown():
    email_ingest.stop_poller()
    # Stop taking new invoices and let the ones being read finish. An invoice
    # abandoned half-way through would leave a job stuck in `processing` until
    # the next startup reconciled it, and finishing takes seconds.
    jobs.shutdown(wait=True)


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
                            uploaded_by=None, source="MANUAL_UPLOAD",
                            client_id=None):
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
                              uploaded_by=uploaded_by, client_id=client_id)
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
                       source: str = "MANUAL_UPLOAD", portal_client=None):
    """The nine-stage pipeline, streamed as SSE frames.

    `portal_client` is a `portal.ClientContext` when an external client
    submitted this invoice through the client portal (Phase J), and None on
    every other path. It changes NOTHING about how the invoice is read or
    judged -- the same stages run, the same rules decide, the same ledger is
    charged -- which is the whole point of there being one pipeline behind
    three doors rather than a separate one for outside parties.

    What it does change is two facts recorded at commit time: which client the
    run is attributed to, and whether the vendor named on the document is one
    that client actually represents. `storage.save_run_checked` acts on the
    second (see its docstring), holding an otherwise-approvable invoice for a
    person rather than charging a purchase order belonging to a company that
    did not send it.
    """
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

    # Phase J. Resolved once here rather than at each use, so the value the
    # run is committed with is the one this request authenticated as -- not a
    # second lookup that could see a different account state part-way through.
    client_id = portal_client.client_id if portal_client else None

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
                                           source=source, client_id=client_id):
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
    # What language the DOCUMENT was written in (Phase L). Reported here rather
    # than in EXTRACT_TEXT because this is the stage that acted on it, and
    # reported at all because "we read this as German" is exactly the kind of
    # thing a reviewer needs to be able to disagree with. It selected extra
    # patterns for the local extractor and nothing else -- no rule is passed it.
    lang_info = extract_info.get("language") or {}
    if lang_info.get("script") not in (None, "Latin"):
        lang_note = f" Script: {lang_info['script']} (no field vocabulary)."
    elif lang_info.get("supported"):
        lang_note = (f" Language: {doclang.name_of(lang_info['language'])} "
                     f"({lang_info.get('confidence', 0):.0%} confidence).")
    else:
        lang_note = " Language: not determined from the text."
    yield sse("stage", {"stage": stage(
        "EXTRACT_FIELDS", ef_status,
        f"Route: {extracted['extraction_method']}. "
        f"Found: {', '.join(found) if found else 'nothing usable'}."
        + lang_note
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
    # The vendor-identity question is asked HERE, against the extracted vendor
    # name, and answered by the client context rather than by anything in the
    # request -- so a submitter cannot assert whose invoice this is. False for
    # every non-portal run, because there is no client to disagree with.
    client_vendor_mismatch = bool(
        portal_client and not portal.represents_vendor(portal_client,
                                                       extracted.get("vendor_name")))
    run_id, status, extra = storage.save_run_checked(
        filename, status, extracted, po_match, stages, reasons,
        tolerance_for=matching.tolerance_for, audit=audit, uploaded_by=uploaded_by,
        client_id=client_id, client_vendor_mismatch=client_vendor_mismatch)
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

# --------------------------------------------------------------------------
# Which language to answer in (Phase L)
#
# One dependency, used by every localised endpoint, so that "what does
# ?lang=pt mean here" has exactly one answer across the whole API -- the same
# reason `analytics_window` and `log_filters` are single dependencies.
#
# THREE THINGS IT IS NOT:
#
#   * It is not authentication. It runs alongside the security dependency and
#     never in front of it; a locale is resolved for a caller who has already
#     been identified, and resolving one grants nothing.
#   * It is not a filter. No query in this application reads it. Two callers
#     with different locales and the same token see the same rows, the same
#     amounts and the same decision -- only the sentences differ.
#   * It is not a precondition. An unsupported or malformed value is never a
#     400: `i18n.resolve` bounds, shape-checks and matches it, and falls back
#     to English. A preference that could not be honoured is reported in the
#     response body (`locale`), not raised.
# --------------------------------------------------------------------------

def request_locale(request: Request,
                   lang: str = Query(None, max_length=35,
                                     description="BCP 47 language tag, e.g. pt-BR")
                   ) -> str:
    """The language for THIS response: ?lang= first, then Accept-Language."""
    return i18n.resolve(explicit=lang,
                        accept_language=request.headers.get("accept-language"))


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
def whoami(principal: auth.Principal = Security(auth.current_principal),
           locale: str = Depends(request_locale)):
    """Who the token says you are, and what it permits. The UI uses this to
    decide which controls to render -- a convenience, never a control: every
    endpoint re-checks the scope itself.

    It also carries the language this deployment resolved for the caller and
    the full list it can answer in (Phase L). That rides here rather than on an
    endpoint of its own deliberately: the languages available are a property of
    the session, this is the call every client already makes to open one, and
    adding a route would mean adding one more thing for the client-portal
    route sweep to have to make an exception for.
    """
    return {"username": principal.username, "roles": principal.roles,
            "scopes": principal.scopes,
            "languages": i18n.language_options(),
            **i18n.describe(locale)}


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
        # upload -- it is an employee at a browser posting a file. The other
        # two sources config.DOCUMENT_SOURCES recognises are written
        # elsewhere: EMAIL by Phase G's ingestion poller, and CLIENT_PORTAL by
        # Phase J's submission endpoint. (The comment that stood here
        # predicted email ingestion as "Phase J"; it predates the lettered
        # tracks, and both paths now exist.)
        async for event in run_pipeline(filename, pdf_bytes, uploaded_by=uploaded_by,
                                        source="MANUAL_UPLOAD"):
            yield event
    except Exception as exc:
        print(f"[error] pipeline failed on {filename!r}: {exc.__class__.__name__}",
              file=sys.stderr)
        yield sse("error", {"error": "Processing failed. The run was not completed."})


# --------------------------------------------------------------------------
# Background processing: upload, then ask
#
# WHY THIS ENDPOINT EXISTS BESIDE /api/runs/stream
#
# The streaming endpoint above drives the pipeline from inside its own
# response body. That is fine while somebody is reading it and fatal the
# moment they are not: Starlette's StreamingResponse runs the body generator
# and a disconnect listener in one task group and cancels the group when
# `http.disconnect` arrives (starlette/responses.py, StreamingResponse.__call__).
# A browser refresh aborts the fetch, the group is cancelled, and the pipeline
# is cancelled with it -- part-way through, before the DECISION stage that is
# the only place a run is ever written. Nothing was persisted, so there was
# nothing for the reloaded page to find.
#
# This endpoint separates the two halves. The request does the part that must
# happen while the caller is still here -- authorize, read the bytes, check
# they are a PDF, write a durable job row -- and returns a job id. The reading
# and judging happen on `jobs`'s worker pool, which holds no reference to the
# request and does not care what the browser does next.
#
# THE STREAMING ENDPOINT IS LEFT EXACTLY AS IT WAS. It is a working API for a
# caller that does hold the connection open, and it is what the test suite
# drives; removing it would be a breaking change this fix does not need to
# make.
# --------------------------------------------------------------------------
@app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run_job(
    file: UploadFile = File(...),
    principal: auth.Principal = Depends(ratelimit.rate_limit_processing),
):
    """Accept an invoice for processing and return the job that will read it.

    202, not 200: the invoice has been accepted and nothing has been decided
    about it yet. The caller polls `GET /api/jobs/{job_id}` for the outcome.
    """
    pdf_bytes = await _read_capped(file)
    _validate_pdf(pdf_bytes)
    filename = _safe_filename(file.filename)

    # THE DEDUPE KEY IS THE CONTENT, NOT A TOKEN THE CLIENT CHOOSES, so a
    # client that forgets to send one is still protected -- which is the whole
    # population of accidental resubmissions this is for (a double-clicked
    # button, a re-render firing submit twice, a fetch retried after a network
    # blip). It is scoped to the submitter and the door as well as the bytes,
    # because two different people uploading the same PDF are two uploads.
    #
    # It is only unique while a job is LIVE (see the partial index in
    # storage.init_db), which is what keeps this from quietly replacing the
    # duplicate RULE: uploading the same invoice again once the first has
    # settled creates a real second run and is rejected by
    # rules.duplicate_check exactly as it always was.
    key = hashlib.sha256(
        pdf_bytes + b"|" + (principal.username or "").encode("utf-8")
        + b"|MANUAL_UPLOAD").hexdigest()

    job_id = uuid.uuid4().hex
    job, duplicate = storage.create_processing_job(
        job_id=job_id, filename=filename, size_bytes=len(pdf_bytes),
        idempotency_key=key, submitted_by=principal.username,
        source="MANUAL_UPLOAD")

    # Only the job that was actually created is submitted. A duplicate returns
    # the job already doing the work, so the second caller watches the first
    # one's progress instead of starting a second read of the same PDF.
    if not duplicate:
        jobs.submit(job["job_id"], filename, pdf_bytes,
                    uploaded_by=principal.username, source="MANUAL_UPLOAD")

    return {**job, "duplicate": duplicate}


@app.get("/api/jobs")
def list_jobs(
    active: bool = Query(False, description="Only jobs that are queued or running"),
    mine: bool = Query(False, description="Only jobs this caller submitted"),
    principal: auth.Principal = Security(auth.current_principal,
                                         scopes=["invoice:read"]),
):
    """Recent processing jobs, newest first.

    `invoice:read`, and that is not a widening: a job carries a filename, a
    status and -- once it finishes -- the run it produced, all of which that
    scope already reads through `/api/runs`. It is not narrowed per user for
    the same reason nothing else here is (there is no per-user invoice
    ownership; this is a shared AP queue), but `?mine=1` is offered because
    "did the upload I started survive my reload?" is a question about your own
    work.
    """
    return storage.list_processing_jobs(
        submitted_by=principal.username if mine else None,
        active_only=active)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str,
            principal: auth.Principal = Security(auth.current_principal,
                                                 scopes=["invoice:read"])):
    """One job: its status, the stages recorded so far, and its result.

    THIS IS WHAT MAKES A REFRESH SURVIVABLE. The browser holds nothing; the
    row does. A page that comes back asks this and is told the truth --
    still running, finished (with the run it produced), or failed (with why).
    """
    job = storage.get_processing_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Job not found")
    return job


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
# Rejection notification -- email the vendor why their invoice was rejected
#
# Two endpoints, deliberately not one, mirroring the release-then-process
# split Phase G's email quarantine already established (§7b.10): the first
# COMPOSES a draft and sends nothing; only the second, explicitly confirmed
# by the reviewer, sends. `invoice:read` reads the draft (the same permission
# that already reads the run and its audit trail); `invoice:review` sends,
# the same authority that accepts or rejects a held invoice.
# --------------------------------------------------------------------------
@app.get("/api/runs/{run_id}/rejection-email")
def preview_rejection_email(run_id: int,
                            principal: auth.Principal = Security(auth.current_principal,
                                                                 scopes=["invoice:read"])):
    """A ready-to-review draft: recipient, subject, body, and the reasons it
    was built from -- plus whether one was already sent, and whether this
    deployment can currently send at all. Sends nothing."""
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if run.get("status") != "REJECTED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This invoice is not rejected; there is no rejection "
                                   "notice to compose.")
    draft = notifications.compose_rejection_email(run)
    return {
        "run_id": run_id,
        "draft": draft,
        "sender": notifications.sender_availability(),
        "history": notifications.rejection_email_history(run_id),
        "already_sent": notifications.last_successful_send(run_id) is not None,
    }


@app.post("/api/runs/{run_id}/rejection-email/send")
def send_rejection_email(run_id: int, payload: dict = Body(...),
                         principal: auth.Principal = Depends(ratelimit.rate_limit_notify)):
    """Send the rejection notice a reviewer has reviewed and confirmed.

    Body: {"recipient", "subject", "body", "force": bool}. `force` resends
    even though a successful send is already on record -- refused by default
    (see notifications.send_rejection_email), so an accidental duplicate
    click needs a deliberate second confirmation from the caller, not just a
    retried request.
    """
    result = notifications.send_rejection_email(
        run_id, actor=principal.username,
        recipient=(payload or {}).get("recipient"),
        subject=(payload or {}).get("subject"),
        body=(payload or {}).get("body"),
        force=bool((payload or {}).get("force")))
    if not result.get("ok"):
        err = result.get("error", "")
        if err == "unknown run":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        if err == "duplicate":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail={"error": "a rejection email has already been sent "
                                                 "for this invoice; pass force=true to resend",
                                       "previous": result.get("previous")})
        if "REJECTED" in err:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err)
        if "email address" in err or "empty" in err:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)
        # Everything else is the outbound send itself failing (no connection,
        # no send scope, Gmail refused the request). The failed attempt is
        # already recorded by notifications.send_rejection_email; this is a
        # 502 because the request to THIS API was fine and the upstream mail
        # provider was not.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err)
    return result


# --------------------------------------------------------------------------
# Audit report export -- a downloadable PDF or CSV about one run
#
# `invoice:read`, and that is not a widening: everything in the report is a
# field the run/activity/email endpoints this scope already guards would
# return one call at a time. This just assembles them into one document.
# --------------------------------------------------------------------------
@app.get("/api/runs/{run_id}/audit-report.pdf")
def export_run_pdf(run_id: int,
                   principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    pdf_bytes = audit_export.build_pdf(run_id)
    notifications.log_export(run_id, actor=principal.username, fmt="pdf")
    filename = audit_export.safe_filename_stub(run) + ".pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/runs/{run_id}/audit-report.csv")
def export_run_csv(run_id: int,
                   principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    csv_text = audit_export.build_csv(run_id)
    notifications.log_export(run_id, actor=principal.username, fmt="csv")
    filename = audit_export.safe_filename_stub(run) + ".csv"
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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


# --------------------------------------------------------------------------
# Email invoice ingestion (Phase G)
#
# Phase F verifies a message handed to the application. These endpoints are
# about messages the application went and FETCHED, and about what happened to
# them afterwards. The security model is Phase F's, unchanged: a quarantined
# message is released or discarded through the Phase F endpoints above, and
# only then can it be processed.
#
# Scopes are the existing ones again -- no Phase G scope was created. Reading
# ingestion state is reading invoice data (invoice:read); triggering a poll or
# processing a message is ingestion (invoice:process); operational
# configuration is admin (invoice:admin).
# --------------------------------------------------------------------------
@app.get("/api/email/ingestion")
def get_ingestion_status(principal: auth.Principal = Security(auth.current_principal,
                                                              scopes=["invoice:admin"])):
    """Whether ingestion is running, how it is configured, and what it has done.

    Scoped to admin because it describes the mailbox connection. It reports
    only whether a credential is PRESENT -- never the password, never the OAuth
    token, never anything derived from either.
    """
    return email_ingest.ingestion_status()


@app.post("/api/email/ingestion/poll")
def trigger_ingestion_poll(principal: auth.Principal = Depends(ratelimit.rate_limit_processing)):
    """Run one polling pass now, instead of waiting for the timer.

    Rate limited and scoped exactly like invoice processing, because that is
    what it can cause. Safe to call while the background poller is also
    running: both go through the same unique constraint, so the worst case is
    one of them being told a message was already ingested.
    """
    # Asks whether there is a mailbox to read, not merely whether an
    # environment variable is set: since Phase G2 a Gmail mailbox connected
    # through the admin UI is itself a configured mailbox, and refusing to poll
    # one an administrator just connected would be indefensible.
    if not email_ingest.ingestion_configured():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email ingestion is disabled. Connect Gmail from Settings, or set "
                   f"{config.EMAIL_INGEST_ENABLED_ENV}=1 and "
                   f"{config.EMAIL_PROVIDER_ENV}=imap.")
    result = email_ingest.poll_once(actor=principal.username)
    if not result.get("ok"):
        # A provider that is unreachable or refusing credentials is a 502: the
        # request was fine, the upstream mailbox was not.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=result.get("error", "the mail provider could not be reached"))
    return result


@app.post("/api/email/messages/{email_id}/process")
def process_email_message(email_id: int,
                          principal: auth.Principal = Depends(ratelimit.rate_limit_processing)):
    """Put an admitted or released message's PDF attachments through the pipeline.

    Deliberately a separate, explicit step rather than something `/release`
    does implicitly: releasing a message is a security ruling, and Phase F's
    endpoint means exactly what it meant before this phase. This is the
    follow-up action, and it is the ONLY way a quarantined message can ever
    reach the pipeline -- `process_message_attachments` re-reads the stored
    security status and refuses anything not ADMITTED or RELEASED, so the gate
    cannot be argued around by a caller.

    The attachments are read back from the holding copy written when the
    message was quarantined -- the invoice PDF is preserved through a
    quarantine (in the existing DocumentStore), even though the message body
    never is. An attachment already processed is skipped rather than run
    twice, so calling this repeatedly is safe.
    """
    message = storage.get_email_message(email_id)
    if not message:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    if message.get("status") not in ("ADMITTED", "RELEASED"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A message with security status {str(message.get('status')).lower()} may "
                   f"not be processed; release it first.")
    result = email_ingest.process_message_attachments(email_id, actor=principal.username)
    if not result.get("ok"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=result.get("error", "the message could not be processed"))
    return result


# --------------------------------------------------------------------------
# Gmail OAuth (Phase G2)
#
# THE AUTHORIZATION MODEL, STATED ONCE FOR ALL FOUR ENDPOINTS.
#
# Three of them require `invoice:admin` -- the scope that already guards
# /api/email/ingestion, because that endpoint describes the mailbox connection
# and these ones CHANGE it. No new scope was invented, for the reason Phases H,
# I and K2 each recorded: a scope needs a role to carry it, which means editing
# every deployment's user store. Phase J's exception does not apply here,
# because this adds no new kind of caller -- it is the existing administrator
# doing an administrative thing.
#
# The fourth, the callback, cannot be scoped at all: Google redirects the
# administrator's BROWSER to it and a top-level navigation carries no
# Authorization header. Its security is the single-use state value, and
# `ratelimit.rate_limit_oauth_callback` bounds guessing it. Nothing it does is
# reachable without a state this server itself minted for a named
# administrator minutes earlier.
# --------------------------------------------------------------------------

# The fixed vocabulary of outcomes the callback may put in a redirect URL.
# A CLOSED SET ON PURPOSE: it means nothing Google said -- no error body, no
# description, no code -- can ever be reflected into the browser's address bar,
# where it would end up in history and in every proxy log on the way.
_GMAIL_CALLBACK_RESULTS = (
    "connected", "denied", "invalid_state", "exchange_failed",
    "insufficient_scope", "no_refresh_token", "not_configured",
)


def _gmail_redirect(result: str) -> RedirectResponse:
    """Send the browser back to the app with a one-word outcome.

    The target is RELATIVE whenever this process also serves the UI, so it
    resolves to this application's own origin -- which is what it has always
    done and what the local demo still does.

    A SPLIT DEPLOYMENT IS THE ONE CASE THAT CANNOT BE RELATIVE. Google redirects
    the administrator's browser to this API host, and a relative "/" would land
    them on the API rather than back on the screen they left from -- including
    for the failure results, which are the ones that most need to be read.
    `FRONTEND_ORIGIN` names where to send them instead.

    STILL NO OPEN REDIRECT, AND THAT PROPERTY IS UNCHANGED. The destination is
    read from server configuration, never from the request: no query parameter,
    no header, no state field feeds it. `config.frontend_origin()` additionally
    admits only scheme://host[:port] and drops anything else, and `result` is
    still checked against the closed `_GMAIL_CALLBACK_RESULTS` set, so nothing
    Google said can reach the address bar either.
    """
    if result not in _GMAIL_CALLBACK_RESULTS:
        result = "exchange_failed"
    base = config.frontend_origin()
    # 303: the callback arrives as a GET, and the browser must follow with a
    # GET to a page rather than re-issuing anything.
    return RedirectResponse(url=f"{base}/?gmail={result}", status_code=303)


@app.get("/api/email/oauth/gmail/status")
def gmail_oauth_status(principal: auth.Principal = Security(auth.current_principal,
                                                            scopes=["invoice:admin"])):
    """Whether Gmail is connected, and what an administrator needs to know.

    Reports the mailbox address, the granted scopes, when it was last polled
    and the last error -- and NO token. The projection behind it
    (`storage.public_oauth_connection`) does not select the token columns at
    all, rather than selecting and then removing them, so a column added later
    is absent by default instead of exposed by default.
    """
    return {
        "provider": "gmail",
        # Two different questions with two different remedies: an unconfigured
        # OAuth client needs the environment edited and the process restarted,
        # while a configured-but-unconnected one just needs somebody to click
        # Connect. Collapsing them would send an administrator to the wrong fix.
        "oauth_configured": config.google_oauth_configured(),
        "redirect_uri": config.google_oauth_redirect_uri() or None,
        "scopes_requested": email_ingest.requested_scopes(),
        "connection": storage.public_oauth_connection("gmail"),
        "ingestion_active": email_ingest.ingestion_configured(),
        "poller_running": email_ingest.poller_running(),
    }


@app.post("/api/email/oauth/gmail/authorize")
def gmail_oauth_authorize(principal: auth.Principal = Security(auth.current_principal,
                                                               scopes=["invoice:admin"])):
    """Begin the authorization-code flow. Returns the URL to send the browser to.

    Returns a URL rather than issuing a redirect, because the caller is an XHR
    from the admin screen and a 302 to accounts.google.com would be followed by
    `fetch` rather than by the browser -- landing Google's HTML in a JSON
    parser instead of in front of the administrator.

    The state and the PKCE verifier are generated here and stored SERVER-SIDE,
    bound to this administrator. The verifier never leaves the server at all;
    only its SHA-256 challenge goes to Google, which is the whole point of
    PKCE -- an authorization code intercepted from the redirect cannot be
    exchanged without a secret that was never transmitted.
    """
    try:
        scopes = config.gmail_scopes()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    if not config.google_oauth_configured():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"Google OAuth is not configured. Set "
                    f"{config.GOOGLE_OAUTH_CLIENT_ID_ENV}, "
                    f"{config.GOOGLE_OAUTH_CLIENT_SECRET_ENV} and "
                    f"{config.GOOGLE_OAUTH_REDIRECT_URI_ENV}."))

    state = oauth_google.new_state()
    verifier = oauth_google.new_code_verifier()
    redirect_uri = config.google_oauth_redirect_uri()

    storage.create_pending_authorization(
        state=state, provider="gmail", code_verifier=verifier,
        redirect_uri=redirect_uri, requested_by=principal.username,
        expires_at=oauth_google.state_expiry())

    url = oauth_google.build_authorization_url(state, verifier)
    print(f"[oauth] gmail authorization started by {principal.username}", file=sys.stderr)
    # The state is NOT returned. The browser does not need it -- it travels to
    # Google inside the URL and comes back in the callback's query string -- and
    # a CSRF token handed to client-side JavaScript is one an XSS can read.
    return {"authorization_url": url, "scopes": scopes, "expires_in":
            config.OAUTH_STATE_TTL_SECONDS}


@app.get("/api/email/oauth/gmail/callback")
def gmail_oauth_callback(request: Request, code: str = None, state: str = None,
                         error: str = None,
                         _limit: None = Depends(ratelimit.rate_limit_oauth_callback)):
    """Where Google sends the administrator's browser back.

    EVERY EXIT FROM THIS FUNCTION IS A REDIRECT CARRYING ONE WORD FROM A FIXED
    LIST. No Google error text, no exception message and no token can reach the
    address bar, and the administrator lands back on the settings screen either
    way rather than on a JSON error page.

    The state is consumed under a row lock before the code is exchanged, so a
    replayed redirect -- a refreshed tab, a link out of browser history, a
    stolen code -- finds it already used and is refused.
    """
    if error:
        # The user pressed Cancel, or Google refused. `error` is Google's, so
        # it is logged as a short code and never echoed onward.
        print(f"[oauth] gmail authorization returned an error: "
              f"{oauth_google._scrub(error)}", file=sys.stderr)
        return _gmail_redirect("denied" if error == "access_denied" else "exchange_failed")

    pending = storage.consume_pending_authorization(state, provider="gmail")
    if not pending or not code:
        # Unknown, expired, already used, or for another provider. The four are
        # deliberately indistinguishable to the caller: each means "do not
        # exchange this code", and telling them apart would confirm to somebody
        # probing that a given state value once existed.
        print("[oauth] gmail callback refused: state was not valid", file=sys.stderr)
        return _gmail_redirect("invalid_state")

    try:
        payload = oauth_google.exchange_code(
            code, pending["code_verifier"], pending["redirect_uri"])
    except oauth_google.OAuthNotConfigured:
        return _gmail_redirect("not_configured")
    except oauth_google.OAuthError as exc:
        print(f"[oauth] gmail token exchange failed: {exc.code or 'error'}", file=sys.stderr)
        return _gmail_redirect("exchange_failed")

    scopes = oauth_google.granted_scopes(payload)
    if not oauth_google.scopes_are_sufficient(scopes):
        # Google's consent screen lets a user untick individual permissions. A
        # connection granted nothing usable would look successful here and fail
        # later inside a background poll nobody is watching, so it is refused
        # now, while there is a person to tell.
        print("[oauth] gmail authorization granted insufficient scope", file=sys.stderr)
        oauth_google.revoke(payload.get("refresh_token") or payload.get("access_token"))
        return _gmail_redirect("insufficient_scope")

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        # Without one, ingestion stops at the first access-token expiry -- an
        # hour later, silently. Refused rather than stored, because a
        # connection that works for an hour is worse than one that plainly did
        # not connect.
        print("[oauth] gmail authorization returned no refresh token", file=sys.stderr)
        oauth_google.revoke(payload.get("access_token"))
        return _gmail_redirect("no_refresh_token")

    access_token = payload.get("access_token")
    address = oauth_google.mailbox_address(access_token)

    # Where this mailbox's first poll starts reading from. Set at CONNECT time
    # rather than left NULL so that connecting cannot ingest and rule on years
    # of invoices somebody has already dealt with by hand; `GMAIL_BACKFILL_DAYS`
    # moves it back for a deployment that does want history.
    started = int((time.time() - config.gmail_backfill_days() * 86400) * 1000)

    try:
        storage.save_oauth_connection(
            provider="gmail",
            email_address=address,
            scopes=" ".join(scopes),
            refresh_token_encrypted=oauth_google.encrypt_token(refresh_token),
            access_token_encrypted=oauth_google.encrypt_token(access_token),
            access_token_expires_at=oauth_google.expiry_from(payload),
            connected_by=pending["requested_by"],
            cursor_internal_date=started)
    except Exception as exc:
        # Including the case where AUTH_SECRET is absent and the tokens
        # therefore cannot be encrypted. Storing them in the clear instead is
        # not an option, so the grant is handed back to Google rather than left
        # live against a connection this deployment could not record.
        print(f"[oauth] gmail connection could not be stored: "
              f"{exc.__class__.__name__}", file=sys.stderr)
        oauth_google.revoke(refresh_token)
        return _gmail_redirect("exchange_failed")

    print(f"[oauth] gmail connected for {pending['requested_by']}", file=sys.stderr)

    # Start polling now. The administrator's mental model is that connecting a
    # mailbox starts reading it; requiring a restart would make the Connected
    # badge describe an intention rather than a state.
    try:
        email_ingest.start_poller()
    except Exception as exc:
        print(f"[oauth] poller did not start: {exc.__class__.__name__}", file=sys.stderr)

    return _gmail_redirect("connected")


@app.post("/api/email/oauth/gmail/disconnect")
def gmail_oauth_disconnect(principal: auth.Principal = Security(auth.current_principal,
                                                                scopes=["invoice:admin"])):
    """Revoke the grant at Google and delete the stored credential.

    BOTH HALVES, IN THAT ORDER, AND THE LOCAL HALF HAPPENS EITHER WAY. Google
    being unreachable must not leave an administrator unable to disconnect a
    mailbox -- a token Google still honours but nobody holds is a smaller
    problem than a credential this application cannot let go of. What was
    achieved is reported honestly rather than assumed.

    The poller is stopped only if there is nothing left for it to read: an
    `EMAIL_PROVIDER=imap` deployment that also happened to have Gmail connected
    keeps polling IMAP, which is what it was configured to do.
    """
    connection = storage.get_oauth_connection("gmail")
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No Gmail mailbox is connected")

    revoked = False
    blob = connection.get("refresh_token_encrypted")
    if blob:
        try:
            revoked = oauth_google.revoke(oauth_google.decrypt_token(blob))
        except oauth_google.OAuthError:
            # An undecryptable credential cannot be revoked remotely, which is
            # all the more reason to remove it locally.
            revoked = False

    storage.delete_oauth_connection("gmail")
    print(f"[oauth] gmail disconnected by {principal.username} "
          f"(remote revoke: {'ok' if revoked else 'not confirmed'})", file=sys.stderr)

    if not email_ingest.ingestion_configured():
        email_ingest.stop_poller()

    return {
        "disconnected": True,
        "revoked_at_google": revoked,
        # Said plainly, because it is the one thing an administrator may still
        # need to act on: if we could not reach Google, the grant may survive
        # in the account's own security settings.
        "notice": (None if revoked else
                   "The credential was deleted here, but Google did not confirm the "
                   "revocation. Remove this application's access at "
                   "https://myaccount.google.com/permissions to be certain."),
        "ingestion_active": email_ingest.ingestion_configured(),
    }


@app.get("/api/email/messages/{email_id}/attachments")
def get_email_attachments(email_id: int,
                          principal: auth.Principal = Security(auth.current_principal,
                                                               scopes=["invoice:read"])):
    """What arrived with a message, and what each attachment became.

    Metadata only -- filename, type, size, hash, and the run it produced (or
    why it did not). The attachment bytes are never served from here: once an
    attachment becomes a run, its PDF is reachable through the existing
    document endpoints, under the same permission, rather than through a
    second download path with its own rules.
    """
    if not storage.get_email_message(email_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return {"email_id": email_id, "attachments": storage.list_email_attachments(email_id)}


# --------------------------------------------------------------------------
# analytics (Phase H)
#
# Read-only, every one of them. Nothing under /api/analytics writes a row,
# creates a run, or changes a status -- these endpoints are queries over the
# history the application already keeps (see analytics.py for what each KPI
# means and why no counter is stored anywhere).
#
# AUTHORIZATION. Aggregate invoice analytics need `invoice:read`, the same
# scope that already reads a run, its audit trail and its document: a
# dashboard showing that 12 invoices were held is derived from rows the same
# caller can already fetch individually, so requiring more would be theatre.
#
# `/api/analytics/users` is the exception, because it is the only one about
# PEOPLE rather than invoices -- see analytics.users() for why it shows the
# caller their own row unless they hold `invoice:admin`.
#
# NO NEW SCOPE WAS ADDED, matching what Phases F and G did: a fifth scope
# needs a role to carry it, which means editing every deployment's user store
# for a reporting screen.
#
# Responses carry aggregates and reference data (vendor names, PO numbers,
# rule names) -- never invoice line items, never document bytes, never email
# subjects or addresses, and never a raw audit_json blob.
# --------------------------------------------------------------------------

def analytics_window(
    range_key: str = Query("30d", alias="range",
                           description="today | 7d | 30d | month | all | custom"),
    date_from: str = Query(None, alias="from", description="YYYY-MM-DD (custom range)"),
    date_to: str = Query(None, alias="to", description="YYYY-MM-DD (custom range, inclusive)"),
) -> "analytics.Window":
    """Validate the time-range parameters every analytics endpoint shares.

    One dependency rather than a try/except repeated six times, so every route
    rejects a bad `range` or a malformed date identically -- and so a caller's
    typo is never silently reinterpreted as the default window.

    `range`, `from` and `to` are the query names because they are what an
    operator would type; `from` is a Python keyword and `range` shadows a
    builtin, so both are taken through an alias rather than renaming the API.
    """
    try:
        return analytics.resolve_window(range_key, date_from, date_to)
    except analytics.AnalyticsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@app.get("/api/analytics/overview")
def analytics_overview(window: "analytics.Window" = Depends(analytics_window),
                       principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """Headline KPIs, the decision mix, value by currency, and the open backlog."""
    return analytics.overview(window)


@app.get("/api/analytics/trends")
def analytics_trends(window: "analytics.Window" = Depends(analytics_window),
                     principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """One row per UTC calendar day, with empty days present as explicit zeroes."""
    try:
        return analytics.trends(window)
    except analytics.AnalyticsError as exc:
        # A range wider than the daily-bucket cap. Refused rather than
        # truncated, so the caller knows it did not get the series it asked for.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@app.get("/api/analytics/processing")
def analytics_processing(window: "analytics.Window" = Depends(analytics_window),
                         principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """Run and per-stage timing, extraction routes, and extraction budget use."""
    return analytics.processing(window)


@app.get("/api/analytics/reviews")
def analytics_reviews(window: "analytics.Window" = Depends(analytics_window),
                      principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """The human-review funnel, its latency, and what reviewers decided.

    Aggregate only. Per-person figures are /api/analytics/users."""
    return analytics.reviews(window)


@app.get("/api/analytics/vendors")
def analytics_vendors(window: "analytics.Window" = Depends(analytics_window),
                      limit: int = Query(None, ge=1, le=analytics.MAX_GROUP_LIMIT),
                      principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """Per-vendor invoice behaviour, and every PO's budget position."""
    try:
        resolved = analytics.resolve_limit(limit)
    except analytics.AnalyticsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return analytics.vendors(window, resolved)


@app.get("/api/analytics/email")
def analytics_email(window: "analytics.Window" = Depends(analytics_window),
                    principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """The email ingestion funnel: what arrived, what triage filtered, what
    verification admitted, and what became an invoice run. Counts and statuses
    only -- no sender addresses, no subjects, no message content."""
    return analytics.email(window)


@app.get("/api/analytics/users")
def analytics_users(window: "analytics.Window" = Depends(analytics_window),
                    principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """Reviewer workload.

    Your own activity, unless you hold `invoice:admin`, in which case the whole
    team's -- the same "your own, unless you are an administrator" shape
    `/review/release` already uses. The response says which of the two it is in
    `scope`, so a client never has to infer it from the row count.

    The decision is made HERE from the authenticated principal, never from a
    query parameter: a `?user=` filter the caller could set would be an
    authorization check the caller performs on themselves.
    """
    return analytics.users(window, viewer=principal.username,
                           see_everyone=principal.has("invoice:admin"))


@app.get("/api/analytics/dashboard")
def analytics_dashboard(window: "analytics.Window" = Depends(analytics_window),
                        limit: int = Query(None, ge=1, le=analytics.MAX_GROUP_LIMIT),
                        principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """Every section of the Analytics screen, in one request.

    The screen has seven panels and was fetching them from the seven endpoints
    above, in parallel. Each of those pays for being a request -- a TLS round
    trip from the browser, authentication, rate-limit accounting, its own
    connection borrows -- and then they queue behind one another on the way to
    the database, so the page cost roughly the SUM of the seven rather than the
    slowest. This returns the same seven payloads, under the same key names,
    from one pass over the window.

    THE SEVEN ENDPOINTS ARE UNCHANGED AND STILL SERVED. This composes them
    rather than replacing them: a client that wants one panel still asks for
    one panel, and every value here is the return of the very function the
    single endpoint calls, so the two cannot disagree.

    Authorised exactly as they are -- the same `invoice:read` scope behind the
    same reporting limiter, and `users` still decides its own scope from the
    authenticated principal (see `/api/analytics/users`), so the combined
    payload is never a way to read a colleague's figures that the single one
    would refuse.
    """
    return analytics.dashboard(
        window,
        viewer=principal.username,
        see_everyone=principal.has("invoice:admin"),
        limit=analytics.resolve_limit(limit),
    )



# --------------------------------------------------------------------------
# Logs, filtering, grouping and exports (Phase I)
#
# WHAT THESE ENDPOINTS ARE
#
# Phase D and Phase F both write append-only histories, and both have been
# readable ONE ENTITY AT A TIME ever since: `/api/runs/{id}/activity` answers
# "what happened to this invoice", `/api/email/messages/{id}` answers it for
# one message. Neither can answer "what happened yesterday", "what has this
# vendor been doing", or "show me every rejection this month". These do.
#
# They READ. Nothing under /api/logs writes, and there is no logs table --
# `invoice_activity` and `email_activity` are the log (see logs.py).
#
# AUTHORIZATION reuses the existing scopes, as Phases F, G and H all did.
#
#   * Reading log rows is `invoice:read`. This is not a widening: since Phase
#     D, `/api/runs/{id}/activity` has returned EVERY actor's events for a run
#     to any caller with `invoice:read`. A cross-run list of those same rows
#     therefore exposes nothing that scope could not already reach, and
#     demanding more for the list view would be theatre.
#
#   * `?group_by=actor` is the exception, because it is the only thing here
#     that produces a PER-PERSON REPORT rather than a view of invoice history.
#     It follows the rule `/api/analytics/users` already set (7c.5): your own
#     row, unless you hold `invoice:admin`. The response says which, in
#     `scope`, and the decision is made from the authenticated principal --
#     never from a query parameter, and an `actor=` filter cannot override it.
#
#   * The export runs at the SAME scope, through the SAME filter object and
#     the same query builder as the list. There is no second WHERE clause that
#     could drift, so "the export cannot show more than the list" is true by
#     construction rather than by two implementations agreeing.
#
# NO NEW SCOPE was created, for the reason Phase H recorded: a fifth scope
# needs a role to carry it, which means editing every deployment's user store.
# --------------------------------------------------------------------------

def log_filters(
    window: "analytics.Window" = Depends(analytics_window),
    stream: str = Query("all", description="all | invoice | email"),
    actor: str = Query(None, description="username, or __system__ for system events"),
    event: str = Query(None, description="event type, e.g. REJECTED"),
    vendor: str = Query(None),
    run_id: int = Query(None, description="one invoice run"),
    invoice_number: str = Query(None),
    po_number: str = Query(None, alias="po"),
    decision: str = Query(None, description="the rules' verdict: APPROVED | NEEDS_REVIEW | REJECTED"),
    run_status: str = Query(None, alias="status", description="the ledger status"),
    source: str = Query(None, description="MANUAL_UPLOAD | EMAIL"),
    email_status: str = Query(None, description="ADMITTED | QUARANTINED | RELEASED | DISCARDED"),
    rule_failed: str = Query(None, description="a rule name from the audit trail"),
    q: str = Query(None, description="free-text search"),
    order: str = Query("desc", description="desc (newest first) | asc"),
) -> "logs.LogFilters":
    """Validate every log filter once, for every endpoint that takes them.

    ONE dependency, deliberately: the list, the grouped view and the CSV
    export all receive the identical object, so a filter cannot mean one thing
    on screen and another in the downloaded file. It also means a bad value is
    rejected the same way -- same 400, same message -- whichever endpoint it
    was sent to.

    The date window comes from `analytics_window`, the same dependency the
    analytics endpoints use, so `range`/`from`/`to` behave identically on both
    screens. A second date parser here is exactly the trap the Phase I brief
    named, and reusing this one is why the log and the dashboard beside it
    cannot disagree about what "last 30 days" means.

    `status` is taken through an alias because the endpoint bodies already
    bind the name `status` to FastAPI's status-code module.
    """
    try:
        return logs.LogFilters(
            window, stream=stream, actor=actor, event=event, vendor=vendor,
            run_id=run_id, invoice_number=invoice_number, po_number=po_number,
            decision=decision, status=run_status, source=source,
            email_status=email_status, rule_failed=rule_failed, search=q,
            order=order)
    except (logs.LogError, analytics.AnalyticsError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@app.get("/api/logs")
def get_logs(
    filters: "logs.LogFilters" = Depends(log_filters),
    page: int = Query(1, ge=1),
    page_size: int = Query(logs.DEFAULT_PAGE_SIZE, ge=1, le=logs.MAX_PAGE_SIZE),
    group_by: str = Query(None, description="event | actor | vendor | day | "
                                            "decision | status | source | stream | run"),
    limit: int = Query(None, description="rows returned when grouping"),
    principal: auth.Principal = Depends(ratelimit.rate_limit_reporting),
):
    """Activity across every invoice and message, filtered, searched and paged.

    Two shapes from one endpoint, chosen by `group_by`, because they are the
    same query answered at two altitudes -- the rows, or the counts behind
    them. A separate /api/logs/group would have meant a second copy of
    fourteen query parameters, and the two copies would drift.

    Paging is OFFSET-based over a TOTAL ordering (timestamp, stream, event id).
    The tiebreak is not decoration: `save_run_checked` writes
    PROCESSING_COMPLETED and REVIEW_REQUIRED inside one transaction with the
    same timestamp string, so ordering on time alone would let them swap
    between page 1 and page 2 -- which shows one row twice and drops the
    other.
    """
    try:
        if group_by:
            return logs.group(filters, group_by, logs.resolve_group_limit(limit),
                              viewer=principal.username,
                              see_everyone=principal.has("invoice:admin"))
        resolved_page, resolved_size = logs.resolve_page(page, page_size)
        return logs.search(filters, resolved_page, resolved_size)
    except logs.LogError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@app.get("/api/logs/stages")
def get_log_stages(
    filters: "logs.LogFilters" = Depends(log_filters),
    page: int = Query(1, ge=1),
    page_size: int = Query(logs.DEFAULT_PAGE_SIZE, ge=1, le=logs.MAX_PAGE_SIZE),
    stage: str = Query(None, description="a stage name, e.g. VENDOR_CHECK"),
    stage_status: str = Query(None, description="ok | warn | fail"),
    principal: auth.Principal = Depends(ratelimit.rate_limit_reporting),
):
    """The pipeline's own history: one row per STAGE, across runs.

    The third record this phase opens up, and the one that is not an event
    stream -- `runs.stages_json` is a JSON array on the run, so it gets its
    own view rather than being flattened into the activity union (logs.py).

    Narrowed by the same filter object every other log endpoint uses, so
    "Globex, last 7 days" means the identical set of runs here and on the
    activity list. The filters a stage cannot have -- actor, event type,
    message status -- are REFUSED with a 400 naming them, rather than ignored
    (which would return rows the caller did not ask for) or answered with an
    empty page (which would read as "these runs have no stages").
    """
    try:
        resolved_page, resolved_size = logs.resolve_page(page, page_size)
        return logs.stage_rows(filters, resolved_page, resolved_size,
                               stage=stage, stage_status=stage_status)
    except logs.LogError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@app.get("/api/logs/stages/export")
def export_log_stages(filters: "logs.LogFilters" = Depends(log_filters),
                      stage: str = Query(None),
                      stage_status: str = Query(None),
                      principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """The filtered per-stage log as CSV.

    Same scope, same filter object and same row generator as the view above --
    so, exactly as with the activity export, "the export cannot show more than
    the list" holds by construction rather than by two queries agreeing.
    """
    try:
        stream = logs.export_stages_csv(filters, stage=stage,
                                        stage_status=stage_status)
        # The generator validates its own arguments on the first pull, and a
        # generator raises nothing until then -- so it is started HERE, inside
        # the try, rather than letting a bad `stage` surface mid-download as a
        # broken CSV with a 200 already sent.
        first = next(stream)
    except logs.LogError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    def body():
        yield first
        for chunk in stream:
            yield chunk

    filename = logs.export_filename("stage-log")
    return StreamingResponse(
        body(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Max-Rows": str(logs.MAX_EXPORT_ROWS),
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/logs/facets")
def get_log_facets(window: "analytics.Window" = Depends(analytics_window),
                   principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """The values worth offering as filter options: who acted, which events
    occurred, which vendors and POs exist, which rules have failed.

    Served by the API rather than assembled in the browser, so a filter panel
    offers what the DATABASE contains rather than what happens to be on the
    page the client already fetched.
    """
    return logs.facets(window)


@app.get("/api/logs/export")
def export_logs(filters: "logs.LogFilters" = Depends(log_filters),
                principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """The filtered log as CSV.

    STREAMED, and generated server-side: the rows are read a chunk at a time
    and written straight to the response, so a large export never sits whole
    in this process's memory.

    THE FILTERS ARE THE ONES IN THE QUERY STRING, resolved by the same
    dependency the list endpoint uses. Exporting more than the caller was
    shown would require a different filter object, and there is only one.

    Values are neutralised against spreadsheet formula injection before they
    are written (logs.csv_safe) -- a review note beginning `=` is data, and
    must not become live content in whoever opens the file.
    """
    filename = logs.export_filename()
    return StreamingResponse(
        logs.export_csv(filters),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The row cap is announced in a header as well as in the file, so
            # a scripted client can tell a truncated export from a complete
            # one without parsing the CSV.
            "X-Export-Max-Rows": str(logs.MAX_EXPORT_ROWS),
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/logs/{stream}/{event_id}")
def get_log_event(stream: str, event_id: int,
                  principal: auth.Principal = Depends(ratelimit.rate_limit_reporting)):
    """One event, with its structured metadata and its subject's context.

    Two path segments rather than a composite id, so FastAPI validates the
    event id as an integer and the stream is checked against a fixed pair
    before either reaches a query.

    Returns the event's metadata as a parsed object and, for an invoice event,
    the names of the rules that failed -- never the raw `audit_json` blob, the
    extracted fields, a document storage key, or (for a message event) its
    sender or subject. Phase F's own endpoint owns the message record; a log
    entry links to it by id rather than restating it.
    """
    try:
        row = logs.detail(stream, event_id)
    except logs.LogError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such log event")
    return row

# --------------------------------------------------------------------------
# The assistant (Phase K2)
#
# WHAT THESE ENDPOINTS ARE
#
# A read-only question-answering layer over records this caller can already
# reach. `chat.py` holds the whole design; the two things worth knowing at the
# routing level are:
#
#   * RETRIEVAL IS CHOSEN BY PYTHON, NOT BY THE MODEL. A question resolves to
#     one intent in a frozen table, which names one retriever. The model never
#     picks what to fetch, never receives a database handle, and never emits
#     SQL -- so there is no path from a question, or from text injected into an
#     invoice and echoed back, to a query nobody wrote.
#
#   * AUTHORIZATION HAPPENS BEFORE THE MODEL SEES ANYTHING. The retrievers are
#     handed the authenticated principal and enforce the same scope rules the
#     equivalent endpoints enforce -- in particular the per-person figures keep
#     `/api/analytics/users`'s restriction (§7c.5): your own row unless you
#     hold invoice:admin. The model is never asked to enforce a permission,
#     because by the time it is called the data has already been narrowed.
#
# SCOPE is `invoice:read`, and this is not a widening: every retriever calls a
# function that this caller could already reach through an existing endpoint.
# The assistant rearranges what they can already read; it opens nothing new.
#
# RATE LIMITED like processing rather than like reading, because a question can
# cost a provider request. The daily budget (quota.CHAT) sits behind it.
# --------------------------------------------------------------------------

@app.post("/api/chat")
def post_chat(payload: dict = Body(...),
              principal: auth.Principal = Depends(ratelimit.rate_limit_chat),
              locale: str = Depends(request_locale)):
    """Ask the assistant a question about this application's records.

    Body: {"message": str, "history": [{"role": "user"|"assistant",
                                        "content": str}, ...]}

    The reply always carries `answered_from`, so a client never has to guess
    whether it is looking at application data or at a model's wording of it:

        application_data                  -- retrieved, laid out by the server
        application_data_phrased_by_model -- retrieved, then written up
        application_policy                -- a fixed answer about what this
                                             application does not record

    `sources` names the records the answer was built from and is assembled in
    Python from what was actually read, so it cannot cite an invoice that does
    not exist. `facts` is the retrieved data itself -- returned so the UI can
    show the records beside the prose rather than asking the reader to trust
    the prose.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Expected a JSON object")
    try:
        return chat.answer(payload.get("message"), payload.get("history"),
                           principal, locale=locale)
    except chat.ChatError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except analytics.AnalyticsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@app.get("/api/chat/suggestions")
def get_chat_suggestions(principal: auth.Principal = Security(
        auth.current_principal, scopes=["invoice:read"]),
        locale: str = Depends(request_locale)):
    """Starter questions the assistant can actually answer.

    Served rather than hard-coded in the UI so a suggestion cannot outlive the
    intent behind it: every string here matches a pattern in `chat.INTENTS`.
    `available` says whether a language model is configured -- the assistant
    works either way, but the answers are laid-out records rather than prose
    when it is not, and the UI should say so rather than let it look broken.
    """
    return {
        "suggestions": chat.starter_prompts(locale),
        "available": chat.provider_available(),
        "note": ("Answers come from this application's own records. The "
                 "assistant is read-only and cannot change anything."),
        **i18n.describe(locale),
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


# --------------------------------------------------------------------------
# The client portal (Phase J)
#
# The only endpoints in this application an external party can reach. Every
# one of them goes through `portal_context`, which is the single place a token
# becomes a set of records -- there is no second way into portal.py, and no
# endpoint here resolves visibility for itself.
#
# WHAT MAKES THE ISOLATION HOLD, IN ONE PARAGRAPH: the client identity and its
# vendor binding are read from the LIVE user store on every request and never
# from the token or from anything the caller sent; the visibility predicate is
# applied in SQL before any row is read; and a run id in a path is only ever an
# additional narrowing on top of it. So changing an id in a URL, a query
# string or a body cannot widen what comes back -- it can only ask about a
# record that then fails the predicate and reads as absent.
# --------------------------------------------------------------------------

def portal_context(
    principal: auth.Principal = Depends(ratelimit.rate_limit_portal),
    locale: str = Depends(request_locale),
) -> "portal.ClientContext":
    """Authenticate, authorize, rate limit, then resolve WHO this client is.

    The order is the same one `rate_limit_processing` established and matters
    for the same reason: an unauthenticated flood is refused before it can
    make this process do any work, and the per-user counter is keyed to a
    verified identity rather than to something the caller supplied.

    A PortalError becomes a 403 with the text portal.py wrote for a supplier
    to read. 403 rather than 401 because the credentials were perfectly valid
    -- the account is simply not set up to represent anyone, which is our
    configuration problem and not something re-authenticating would fix.
    """
    try:
        return portal.context_for(principal, locale=locale)
    except portal.PortalError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))


def _portal_run_or_404(ctx: "portal.ClientContext", invoice_id: int):
    """One of this client's invoices, or 404.

    404, NOT 403, for an invoice belonging to somebody else -- and identical
    to the 404 a nonexistent id gets. A 403 here would confirm that the id
    names a real invoice, which is a fact about another company's business and
    exactly what someone walking the id space is trying to learn.
    """
    invoice = portal.get_invoice(ctx, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=i18n.t("portal.error.no_such_invoice", ctx.locale))
    return invoice


@app.get("/api/portal/me")
def portal_me(ctx: "portal.ClientContext" = Depends(portal_context)):
    """Who the portal has this caller down as, and which suppliers they cover.

    Carries `notices` so a misconfigured supplier link is stated plainly
    rather than presenting to the vendor as missing invoices.
    """
    return portal.client_identity(ctx)


@app.get("/api/portal/invoices")
def portal_invoices(limit: int = Query(portal.DEFAULT_PAGE, ge=1, le=portal.MAX_PAGE),
                    offset: int = Query(0, ge=0),
                    state: str = Query(None),
                    ctx: "portal.ClientContext" = Depends(portal_context)):
    """This client's invoices, newest first.

    There is deliberately no `client`, `vendor` or `client_id` parameter. The
    only narrowing a caller may ask for is by state and by page, because every
    other axis is already fixed by who they are -- and a filter a caller
    supplies on the dimension that decides what they may see is not a filter,
    it is an authorization check performed by the person being checked.
    """
    try:
        return portal.list_invoices(ctx, limit=limit, offset=offset, state=state)
    except portal.PortalError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@app.get("/api/portal/invoices/{invoice_id}")
def portal_invoice(invoice_id: int,
                   ctx: "portal.ClientContext" = Depends(portal_context)):
    """One of this client's invoices, with its client-visible timeline."""
    return _portal_run_or_404(ctx, invoice_id)


@app.get("/api/portal/invoices/{invoice_id}/document")
def portal_invoice_document(invoice_id: int,
                            ctx: "portal.ClientContext" = Depends(portal_context)):
    """Metadata for the PDF this invoice was processed from.

    A hand-listed subset of what the internal endpoint returns. `uploaded_by`
    is absent as well as `storage_key` and `storage_backend`: for an invoice
    that arrived by email or by an employee's upload, that field names one of
    our people.
    """
    doc = portal.invoice_document_row(ctx, invoice_id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=i18n.t("portal.error.no_document", ctx.locale))
    # Attributed to the authenticated supplier login, not to NULL. A NULL
    # actor means "the system did this" (§6.1 of the handoff notes), and a
    # vendor opening their own invoice is neither the system nor anonymous --
    # and the AP team is entitled to see, in the same activity history they
    # already read, that the supplier has seen it.
    storage.log_activity(invoice_id, "DOCUMENT_VIEWED", actor=ctx.username,
                         note="Viewed through the client portal",
                         metadata={"client_id": ctx.client_id, "portal": True})
    return {
        "invoice_id": doc["run_id"],
        "filename": doc["original_filename"],
        "mime_type": doc["mime_type"],
        "size_bytes": doc["size_bytes"],
        "sha256": doc["sha256"],
        "received_at": doc["uploaded_at"],
    }


@app.get("/api/portal/invoices/{invoice_id}/document/download")
def portal_invoice_document_download(invoice_id: int, inline: bool = False,
                                     ctx: "portal.ClientContext" = Depends(portal_context)):
    """The invoice PDF itself.

    Visibility is resolved before the document row is looked up, let alone the
    file read, so an unauthorised caller learns nothing about whether that id
    has a document. The bytes come from Phase C's DocumentStore through the
    same server-generated key the internal endpoint uses -- there is no
    portal-specific storage path and no second key format to get wrong.
    """
    doc = portal.invoice_document_row(ctx, invoice_id)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=i18n.t("portal.error.no_document", ctx.locale))
    try:
        data = documents.get_store().read(doc["storage_key"])
    except (FileNotFoundError, OSError, ValueError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=i18n.t("portal.error.document_gone", ctx.locale))

    storage.log_activity(invoice_id, "DOCUMENT_DOWNLOADED", actor=ctx.username,
                         note="Downloaded through the client portal",
                         metadata={"client_id": ctx.client_id, "portal": True,
                                   "inline": inline})
    filename = doc["original_filename"] or f"invoice-{invoice_id}.pdf"
    disposition = "inline" if inline else "attachment"
    return Response(
        content=data,
        media_type=doc["mime_type"] or "application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@app.get("/api/portal/purchase-orders")
def portal_purchase_orders(ctx: "portal.ClientContext" = Depends(portal_context)):
    """The purchase orders raised to this client, and what is left on each.

    The `remaining` figure is the ledger's own -- derived from run_allocations
    joined to APPROVED runs -- so a supplier and a buyer looking at the same
    order read the same number rather than two figures maintained separately.
    """
    return portal.purchase_orders(ctx)


@app.post("/api/portal/invoices")
async def portal_submit_invoice(
    file: UploadFile = File(...),
    principal: auth.Principal = Depends(ratelimit.rate_limit_portal_submit),
    locale: str = Depends(request_locale),
):
    """Submit an invoice through the portal.

    THE SAME PIPELINE, NOT A SECOND ONE. This drives `run_pipeline` -- every
    stage, the same rules, the same confidence gate, the same PO matching, the
    same allocation ledger, the same review routing -- with
    `source="CLIENT_PORTAL"`, which `config.DOCUMENT_SOURCES` recognises
    alongside the manual and email doors. An externally submitted invoice is
    judged by exactly the process an internally uploaded one is.

    DELIBERATELY NOT STREAMED, and that is the one visible difference from the
    internal endpoint. The SSE frames name internal stages and carry their
    detail lines -- extraction routes, vendor lookups, PO balances, tolerance
    arithmetic -- so streaming them to an outside party would hand over the
    running commentary this phase spends the rest of its effort not printing.
    The generator is driven to completion here and only the client projection
    is returned.

    THE DAILY BUDGET IS CHECKED BEFORE ANY WORK HAPPENS. Extraction spends a
    shared provider quota, and the vision route -- the only one that can read a
    scan -- has a free tier of twenty requests a DAY. Reserving the client's
    own allowance first means an external caller can exhaust what it was given
    without ever reaching what the internal pipeline needs.
    """
    ctx = portal_context(principal, locale)

    if not quota.try_consume(quota.portal_key(ctx.client_id)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=i18n.t("portal.error.daily_limit", ctx.locale,
                          limit=config.DAILY_QUOTA_PORTAL_SUBMISSIONS),
            headers={"Retry-After": "3600"},
        )

    pdf_bytes = await _read_capped(file)
    _validate_pdf(pdf_bytes)
    filename = _safe_filename(file.filename)

    final = None
    try:
        async for frame in run_pipeline(filename, pdf_bytes,
                                        uploaded_by=principal.username,
                                        source="CLIENT_PORTAL", portal_client=ctx):
            # Only the closing frame is of any interest here; the stage frames
            # are internal and are consumed and dropped.
            if frame.startswith("data: "):
                event = json.loads(frame[6:])
                if event.get("type") == "final":
                    final = event.get("result")
    except Exception as exc:
        print(f"[error] portal submission failed on {filename!r}: "
              f"{exc.__class__.__name__}", file=sys.stderr)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=i18n.t("portal.error.processing_failed", ctx.locale))

    if not final or not final.get("run_id"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=i18n.t("portal.error.processing_failed", ctx.locale))

    # Re-read through the portal's own visibility predicate rather than
    # projecting the pipeline's in-memory result. Two things fall out of that:
    # the client is shown what was actually COMMITTED (including a downgrade
    # save_run_checked applied at commit time), and the response goes through
    # the same isolation check as every other read -- so this endpoint cannot
    # become the one place a client is handed a record the predicate would
    # have refused.
    submitted = portal.get_invoice(ctx, final["run_id"])
    if submitted is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=i18n.t("portal.error.processing_failed", ctx.locale))
    return {"submitted": True, "invoice": submitted}


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


# Mounted last, and only when there is something to mount (see SERVE_FRONTEND
# above). A "/" mount is a catch-all, so it must come after every /api route --
# it always has. When the export is absent, "/" answers with a small liveness
# document naming the API instead, so hitting the backend's own domain in a
# browser says what this host is rather than 404ing without explanation.
if SERVE_FRONTEND:
    app.mount("/", _AppShell(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    @app.get("/", include_in_schema=False)
    def _api_only_root():
        """API-only deployment: the UI is served elsewhere.

        Says nothing about configuration, versions or which providers are
        reachable -- the same restraint /api/health already observes.
        """
        return {"service": "Invoice Processing API", "ui": "served separately",
                "health": "/api/health"}
