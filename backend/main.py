"""FastAPI app: invoice processing pipeline with a live (SSE) run view + dashboard."""
import asyncio
import json
import time
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import extraction
import matching
import rules
import storage

app = FastAPI(title="Invoice Processing")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.on_event("startup")
def _startup():
    storage.init_db()


def sse(event_type, payload):
    return f"data: {json.dumps({'type': event_type, **payload})}\n\n"


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

    # 2. EXTRACT_TEXT (+ OCR fallback)
    text, has_text_layer, ocr_attempted, ocr_succeeded = extraction.extract_text_and_ocr_flag(pdf_bytes)
    if has_text_layer:
        detail = f"Extracted {len(text)} characters of embedded text."
        st = "ok"
    elif ocr_succeeded:
        detail = f"No embedded text layer — OCR recovered {len(text)} characters."
        st = "warn"
    else:
        detail = "No embedded text layer, and OCR is unavailable in this environment. No text recovered."
        st = "fail"
    yield sse("stage", {"stage": stage("EXTRACT_TEXT", st, detail)})
    await asyncio.sleep(0.25)
    mark()

    # 3. EXTRACT_FIELDS
    extracted_obj = extraction.extract_fields(text) if text else extraction.ExtractedInvoice(
        raw_text="", extraction_method="none", has_text_layer=False,
        ocr_attempted=ocr_attempted, ocr_succeeded=ocr_succeeded,
    )
    extracted_obj.has_text_layer = has_text_layer
    extracted_obj.ocr_attempted = ocr_attempted
    extracted_obj.ocr_succeeded = ocr_succeeded
    extracted = extracted_obj.to_dict()
    found = [k for k in ["vendor_name", "invoice_number", "invoice_date", "total"] if extracted.get(k)]
    yield sse("stage", {"stage": stage(
        "EXTRACT_FIELDS", "ok" if found else "warn",
        f"Method: {extracted['extraction_method']}. Found: {', '.join(found) if found else 'nothing usable'}."
    )})
    await asyncio.sleep(0.3)
    mark()

    # 4. VALIDATE
    missing = rules.validate_required_fields(extracted)
    yield sse("stage", {"stage": stage(
        "VALIDATE", "fail" if missing else "ok",
        f"Missing required field(s): {', '.join(missing)}." if missing else "All required fields present."
    )})
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
        has_text_layer, ocr_attempted, ocr_succeeded, missing, vendor_ok, vendor_detail,
        dup_row, dup_detail, po_match,
    )
    yield sse("stage", {"stage": stage("DECISION", "ok", f"Final status: {status}.")})
    await asyncio.sleep(0.15)
    mark()

    run_id = storage.save_run(filename, status, extracted, po_match, stages, reasons)

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

    items = []
    for name in os.listdir(d):
        if not name.lower().endswith(".pdf"):
            continue
        meta = manifest.get(name, {})
        items.append({
            "filename": name,
            "label": meta.get("label"),
            "note": meta.get("note"),
            "expect": meta.get("expect"),
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
