"""FastAPI app: invoice processing pipeline with a live (SSE) run view + dashboard."""
import asyncio
import json
import time
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import Body, FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import config
import extraction
import matching
import rules
import storage
from schemas import ExtractedInvoice

app = FastAPI(title="Invoice Processing")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.on_event("startup")
def _startup():
    config.load_dotenv()
    storage.init_db()


def sse(event_type, payload):
    return f"data: {json.dumps({'type': event_type, **payload})}\n\n"


# Stages after EXTRACT_TEXT, in order. Used to close out a run that cannot proceed.
_REMAINING_AFTER_TEXT = ["EXTRACT_FIELDS", "VALIDATE", "VENDOR_CHECK", "PO_MATCH",
                         "DUPLICATE_CHECK", "TOLERANCE_CHECK"]


async def _abort_unreadable(filename, message, stages, stage):
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
    run_id = storage.save_run(filename, status, extracted, po_match, stages, reasons)
    yield sse("final", {"result": {
        "run_id": run_id, "filename": filename, "status": status, "reasons": reasons,
        "extracted": extracted, "po_match": po_match, "stages": stages,
    }})


async def run_pipeline(filename: str, pdf_bytes: bytes):
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
        async for evt in _abort_unreadable(filename, str(exc), stages, stage):
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
        if po_match["within_tolerance"] and po_match["is_partial"]:
            tol_detail = f"Diff ${po_match['diff']:.2f} — partial invoice, within remaining PO balance."
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
    status, reasons = rules.decide(
        extract_info, missing, vendor_ok, vendor_detail, dup_row, dup_detail, po_match,
        arithmetic=arithmetic, amount=amount,
    )
    yield sse("stage", {"stage": stage("DECISION", "ok", f"Final status: {status}.")})
    await asyncio.sleep(0.15)
    mark()

    # Commit under the ledger write lock, which re-verifies the PO balance and
    # downgrades a stale APPROVED rather than overspending the PO.
    run_id, status, extra = storage.save_run_checked(
        filename, status, extracted, po_match, stages, reasons,
        tolerance_for=matching.tolerance_for)
    if extra:
        reasons = list(reasons) + [extra]

    result = {
        "run_id": run_id,
        "filename": filename,
        "status": status,
        "reasons": reasons,
        "extracted": extracted,
        "po_match": po_match,
        "stages": stages,
    }
    yield sse("final", {"result": result})


@app.post("/api/runs/stream")
async def create_run_stream(file: UploadFile = File(...)):
    pdf_bytes = await file.read()
    return StreamingResponse(
        run_pipeline(file.filename, pdf_bytes),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs")
def get_runs():
    return storage.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    run = storage.get_run(run_id)
    if not run:
        return {"error": "not found"}
    return run


@app.post("/api/runs/{run_id}/status")
def change_run_status(run_id: int, payload: dict = Body(...)):
    """Change a run's status, then re-evaluate anything queued on the same PO.

    This is the reversal path. There is no balance to refund: consumption is
    derived from APPROVED runs, so moving a run out of APPROVED frees its budget
    the moment the row is updated. Freed budget then cascades to invoices that
    were held only because the PO was exhausted.
    """
    new_status = (payload or {}).get("status")
    note = (payload or {}).get("note")
    ok, old_status, po_number = storage.set_run_status(run_id, new_status, note)
    if not ok:
        return {"error": "unknown run, or invalid status"}

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


@app.get("/api/reference")
def get_reference():
    return {"purchase_orders": storage.list_purchase_orders(), "vendors": storage.list_vendors()}


@app.get("/api/sample-invoices")
def list_sample_invoices():
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
def get_sample_invoice(name: str):
    from fastapi.responses import FileResponse
    d = os.path.join(os.path.dirname(__file__), "..", "sample_invoices")
    path = os.path.join(d, name)
    if not os.path.isfile(path) or not name.lower().endswith(".pdf"):
        return {"error": "not found"}
    return FileResponse(path, media_type="application/pdf")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
