# Invoice Processing — PDF to Decision

An automated AP process: upload a vendor invoice PDF, watch it move through
extraction, validation, PO matching, and rule checks live, and get a decision
(**APPROVED** / **NEEDS_REVIEW** / **REJECTED**) with the full reasoning trail —
plus a dashboard of every run.

See [PROCESS_MAP.md](PROCESS_MAP.md) for the design (pipeline stages, decision
hierarchy, and why the LLM/rules split is where it is).

## Quick start (Windows)

```powershell
.\start.ps1
```

This creates a venv, installs dependencies, generates the sample invoices if
they aren't already there, and opens http://127.0.0.1:8000 in your browser.

## Manual start

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe sample_invoices\generate_invoices.py   # first run only
.\venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000.

## Using it

- **Run tab** — drop in a PDF (or click one of the bundled sample invoices) and
  hit "Run process." Each pipeline stage lights up live as it executes, then
  the decision panel shows the status, the full reasoning trail, and every
  field that was extracted.
- **Dashboard tab** — every run ever processed, filterable by status, with
  click-through to the full stage log for any run.
- **Reference Data tab** — the purchase order dataset and approved vendor list
  the process checks against.

## Sample invoices (`sample_invoices/`)

Regenerate anytime with `python sample_invoices/generate_invoices.py`.

| File | Scenario | Expected result |
|---|---|---|
| `01_happy_path_acme.pdf` | Clean invoice, explicit PO, amount within tolerance | **APPROVED** |
| `02_split_po_globex_a.pdf` | First of two invoices against one PO | **APPROVED** (partial) |
| `03_split_po_globex_b.pdf` | Second invoice — exactly exhausts the remaining balance | **APPROVED** |
| `03b_split_po_globex_overflow.pdf` | Third invoice against the now-exhausted PO | **NEEDS_REVIEW** |
| `04_missing_invoice_number.pdf` | Vendor omitted the invoice number | **NEEDS_REVIEW** |
| `05_scanned_no_text.pdf` | Scanned image, no embedded text layer | **NEEDS_REVIEW** |
| `06_duplicate_of_01.pdf` | Resubmission of invoice `INV-2201` | **REJECTED** |

Run `02` and `03` before `03b` to see the split-PO balance tracking play out
(each APPROVED run consumes part of `PO-1002`), and run `01` before `06` to see
duplicate detection catch the resubmission.

## Architecture

- **Backend**: FastAPI (`backend/`) — one endpoint streams pipeline stages
  live over SSE as the invoice is processed (`POST /api/runs/stream`); the
  rest serve the run history and reference data for the dashboard.
- **Extraction**: `backend/extraction.py` — pdfplumber pulls embedded text;
  if a page has no text layer (scanned image), it tries OCR via
  `pytesseract` + `pdf2image` and honestly reports when that's unavailable
  rather than guessing. Field extraction uses the Anthropic API when
  `ANTHROPIC_API_KEY` is set in the environment, and falls back to a
  deterministic regex extractor otherwise — same output schema either way.
- **Matching & rules**: `backend/matching.py` (PO lookup + split-PO balance
  tracking) and `backend/rules.py` (required fields, vendor approval,
  duplicate detection, final decision aggregation) — plain, deterministic
  code, since the decision needs to be the same answer every time and
  auditable to a non-technical AP reviewer.
- **Storage**: SQLite (`data/app.db`), seeded from `data/purchase_orders.json`
  and `data/approved_vendors.json` on every startup.
- **Frontend**: `frontend/` — vanilla HTML/CSS/JS, no build step. Consumes the
  SSE stream directly via `fetch()` for the live run view.

## Optional: LLM extraction

Set `ANTHROPIC_API_KEY` before starting the server to use Claude for field
extraction instead of the regex fallback — useful for messier, inconsistently
formatted real-world invoices where a fixed regex schema breaks down:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
.\start.ps1
```
