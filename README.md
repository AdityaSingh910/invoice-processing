# Invoice Processing — PDF to Decision

An automated AP process: upload a vendor invoice PDF, watch it move through
extraction, validation, PO matching and rule checks live, and get a decision
— **APPROVED** / **NEEDS_REVIEW** / **REJECTED** — with the full reasoning
trail, plus a dashboard of every run.

Built for the Zamp AI Solutions Associate case study, **PS-1 (Finance / AP)**.

---

## Where the project stands right now

**The app runs. All 7 sample invoices produce their expected verdicts.**

| | |
|---|---|
| Pipeline | Working, 9 stages, streamed live to the browser |
| Sample invoices | 7 / 7 passing from a clean database |
| UI | Run view, dashboard, reference data — all working, light + dark |
| Extraction route in use | **regex** (no API key set — see [Extraction routes](#extraction-routes)) |
| Automated tests | **None yet** — next task |
| Known defects | **3 open**, documented below — 2 from the audit, 1 concurrency race |
| Deployed anywhere | No — runs locally only |

Read [AUDIT.md](AUDIT.md) before trusting any of this in a real AP context. It
is a deliberately unflattering self-review and it found real problems.
[REFACTOR_STRATEGY.md](REFACTOR_STRATEGY.md) is the architect-level response:
exact fix logic, schemas and sequencing for everything the audit surfaced.

---

## How we got here

Roughly in order, so the git history makes sense:

**1. Build.** Chose PS-1, mapped the process on paper ([PROCESS_MAP.md](PROCESS_MAP.md)),
generated 7 synthetic invoice PDFs plus a PO/vendor dataset, then built the
pipeline, the SSE live run view and the dashboard. Three bugs surfaced during
testing and were fixed: a `Total` regex that matched inside `Subtotal`, an
invoice-number regex that matched prose in a footnote, and — the important one
— an `abs()` in the tolerance check that flagged every legitimate partial
invoice as an exception.

**2. UI rebuild.** Replaced the flat key-value result panel with a verdict bar,
a segmented **PO balance bar** (consumed / this invoice / remaining, with
overflow hatched in red), severity-coded reasoning, per-stage timings, labelled
sample scenarios and a PO-consumption view on the dashboard. Dark mode added.

**3. "Accept any external PDF" work.** Rewrote extraction to handle invoices it
has never seen: LLM over text, LLM over page images (replacing the OCR
dependency), and a much more tolerant regex fallback. **This left the app
broken mid-refactor** — `main.py` was still calling functions that had been
deleted.

**4. Audit.** Paused feature work to answer five questions honestly: where the
LLM makes business decisions, whether rules live in config or code, whether
fields carry confidence and provenance, whether the trace is reconstructable,
and whether a low-confidence extraction can sneak through to auto-approve. The
answers are in [AUDIT.md](AUDIT.md), along with a phased refactor plan.

**5. Phase 0 — get back to green** (in progress):

- ✅ **Step 1** — `git init`, `.gitignore`, `.gitattributes`, baseline commit of
  the broken state so the fix could be diffed against it.
- ✅ **Step 2** — reconciled `main.py` with the new extraction API. **7/7 samples
  passing again.**
- ⬜ **Step 3** — turn the throwaway verification script into a real
  `tests/test_samples.py`.

**6. Architect review.** A second pass over the audit findings, this time
producing implementation patterns rather than just a list of problems:
[REFACTOR_STRATEGY.md](REFACTOR_STRATEGY.md). It adds the exact fix logic for
both live bugs, the `Tracked[T]` provenance wrapper and confidence gate, designs
for three harder edge cases (multi-PO consolidation, FX drift, unlisted
surcharges), a versioned `rules.yaml`, and a replayable `DecisionTrace` schema.

It also found **a third defect the audit missed** — a concurrency race in the
PO ledger (see [Known problems](#known-problems)) — and pushed back on three
points, most importantly that FX conversion must not silently widen
auto-approval.

Phase 1 (fixing the live bugs) has **not** started.

---

## Quick start (Windows)

```powershell
.\start.ps1
```

Creates a venv, installs dependencies, generates the sample invoices if missing,
and opens <http://127.0.0.1:8000>.

### Manual start

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe sample_invoices\generate_invoices.py   # first run only
.\venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
```

> The server holds `data/app.db` open. To reset run history, **stop the server
> first**, then delete the file — it is rebuilt from the seed JSON on startup.

---

## Using it

- **Run tab** — drop in a PDF, or click a bundled sample. Each stage lights up
  live as it executes; the decision panel then shows the verdict, the PO balance
  bar, the reasoning trail and every extracted field.
- **Dashboard tab** — every run, filterable by status, click-through to the full
  stage log, plus PO consumption across all POs.
- **Reference tab** — the purchase orders and approved vendors being checked against.

---

## Sample invoices

Regenerate anytime with `python sample_invoices/generate_invoices.py`.

| File | Scenario | Expected |
|---|---|---|
| `01_happy_path_acme.pdf` | Clean invoice, explicit PO, within tolerance | **APPROVED** |
| `02_split_po_globex_a.pdf` | First of two invoices against one PO | **APPROVED** (partial) |
| `03_split_po_globex_b.pdf` | Second invoice — exactly exhausts the balance | **APPROVED** |
| `03b_split_po_globex_overflow.pdf` | Third invoice against the exhausted PO | **NEEDS_REVIEW** |
| `04_missing_invoice_number.pdf` | Amounts fine, but no audit key | **NEEDS_REVIEW** |
| `05_scanned_no_text.pdf` | Image-only PDF, no text layer | **NEEDS_REVIEW** |
| `06_duplicate_of_01.pdf` | Resubmission of `INV-2201` | **REJECTED** |

**Order matters.** Several cases are history-dependent by design:

- Run `02` → `03` → `03b` to see split-PO balance tracking. Each APPROVED run
  consumes part of `PO-1002`; by `03b` there is nothing left.
- Run `01` before `06` or the duplicate has nothing to collide with.

Run `03b` alone against a fresh database and it is **APPROVED** — same bytes,
opposite verdict, because $2,500 against an untouched $5,000 PO is an ordinary
partial invoice. The decision depends on the PO's history, not the file alone.

---

## How it works

### The core idea

**The AI reads, the rules decide.** Extraction is genuinely hard for code —
every vendor formats invoices differently — and easy for an LLM. But the
*decision* must be identical every time and defensible to an auditor, so no
model touches a dollar comparison. Everything downstream of extraction is
deterministic Python.

### Pipeline

```
INGEST → EXTRACT_TEXT → EXTRACT_FIELDS → VALIDATE → VENDOR_CHECK
       → PO_MATCH → DUPLICATE_CHECK → TOLERANCE_CHECK → DECISION
```

Stages do **not** short-circuit. A missing invoice number at stage 4 does not
stop stages 5–8 — findings accumulate and only the final stage judges, so a
reviewer sees the whole picture rather than the first thing that went wrong.

### Decision hierarchy

- **REJECTED** — things the process must not override: duplicates, vendors on
  file but not approved.
- **NEEDS_REVIEW** — recoverable: missing fields, unreadable scan, amount over
  tolerance, no PO match.
- **APPROVED** — everything passed.

Reject wins over review when both fire.

### Tolerance is deliberately one-sided

```python
within = diff <= tol      # not abs(diff) <= tol
```

Billing **over** the remaining PO balance is a problem — the vendor wants money
nobody authorised. Billing **under** it is a normal partial invoice. Tolerance
is `max(2% of remaining, $25)`.

### Split-PO tracking

There is no "consumed" column. The remaining balance is derived on every run by
summing the totals of previously **APPROVED** runs matched to that PO:

```
remaining_before = PO amount − Σ(prior approved invoices)
```

Two consequences: only approved runs consume budget (a flagged invoice sitting
in review doesn't block the queue behind it), and the run history *is* the
ledger — no counter can drift out of sync.

### Extraction routes

Three routes, degrading in a defined order. Same output schema either way, so
matching and rules never know which ran:

| Route | When | Needs API key |
|---|---|---|
| `llm (text)` | PDF has an embedded text layer | Yes |
| `llm (vision)` | Scanned / image-only PDF — page images sent to the model | Yes |
| `regex` | No API key, or the LLM call failed | No |
| `none` | Nothing readable — returns empty fields rather than guessing | — |

**Currently running on `regex`**, because no key is set. To enable the LLM and
vision routes, create a `.env` file in the project root:

```
GEMINI_API_KEY=...
```

Get one free at <https://aistudio.google.com/apikey>.

`.env` is gitignored and the key is never sent to the browser — the UI is only
told whether a key is present.

> ⚠️ The `llm (text)` and `llm (vision)` routes are **wired but not yet
> exercised** — every test so far has run through `regex`. Sample 05 (scanned)
> currently lands on `route=none` for this reason, which is why it reads
> "NEEDS_REVIEW — nothing could be read" rather than actually being read by vision.

### Stack

- **Backend** — FastAPI. `POST /api/runs/stream` streams stages over SSE as they
  execute; other endpoints serve run history and reference data.
- **Extraction** — `pdfplumber` for text, `pypdfium2` for page rasterisation
  (a self-contained wheel: no poppler or tesseract to install).
- **Storage** — SQLite at `data/app.db`, seeded from `data/*.json` on startup.
- **Frontend** — vanilla HTML/CSS/JS, no build step, reads the SSE stream with
  `fetch()`.

---

## Known problems

From [AUDIT.md](AUDIT.md) and [REFACTOR_STRATEGY.md](REFACTOR_STRATEGY.md).
Three are live bugs, not just design gaps:

🐞 **Inferred PO matches don't block approval.** When no PO reference is
extracted, the process binds to the nearest-amount PO for that vendor with *no
distance cap*, adds a `warn`-level reason — and `warn` doesn't change the
verdict. An invoice that never named a PO can auto-approve against one the
process picked.

🐞 **Currency is extracted and never read.** Zero references in `matching.py` or
`rules.py`. The PO table has a `currency` column nobody consults. A €3,000
invoice against a $5,000 PO is compared as `3000` vs `5000`.

🐞 **The PO ledger has a concurrency race.** Every `storage` function opens and
closes its own connection, so a transaction cannot span read-balance → decide →
write. Two invoices for the same PO processed concurrently both read the same
remaining balance and can both be approved, committing more than the PO
authorises. This is the only known defect that produces a wrong **number**
rather than a wrong routing. Fix — `BEGIN IMMEDIATE` + WAL, with the slow
extraction step kept outside the lock — in
[REFACTOR_STRATEGY.md §3.4](REFACTOR_STRATEGY.md).

Also, by design rather than accident, and all queued for later phases:

- Extracted fields are **bare values** — no confidence, no pointer to where in
  the document they came from. A total read off the page is indistinguishable
  from one the code synthesised as `subtotal + tax`.
- **All business rules are hardcoded.** Tolerance is two magic numbers in a
  one-line function. `config.py` holds only operational settings.
- Vendor matching is **bidirectional substring** — `Acme Corp` matches approved
  `Acme Office Supplies`.
- Reference data is **re-seeded from JSON on every startup**, so editing
  `purchase_orders.json` silently changes what historical runs refer to.
- No arithmetic consistency check — nothing verifies `subtotal + tax == total`.

---

## What's next

| Phase | Work |
|---|---|
| **0** | Finish Step 3 — real pytest suite |
| **1** | Cap inferred PO matches + make `warn` actually bite; currency mismatch → review; normalised vendor matching |
| **2** | `Tracked[T]` provenance wrapper, per-route confidence, and the **confidence gate** |
| **3** | `rules.yaml` — pull every threshold out of Python, stamp the version on each run |
| **4** | Transaction boundaries and the `run_allocations` ledger table |
| **5** | `DecisionTrace` + reference snapshot; stop re-seeding on startup |
| **6** | Line-item decomposition, multi-PO consolidation, FX provider |
| **7** | UI: confidence badges, evidence snippets, allocation view |

**If only three things get built:** the live bugs (Phase 1), the confidence gate
(Phase 2 — it closes the low-confidence auto-approve problem as a *class* rather
than case by case), and the transaction fix (Phase 4a — the only defect that
corrupts a number rather than a routing).

**One sequencing trap worth knowing:** multi-PO consolidation is a *ledger*
feature, not a matching feature. The schema stores one `po_number` per run and
consumption sums run totals, so a consolidated invoice would over-consume every
PO it touched. It needs the allocations table, which needs transaction
boundaries first. Phase 4 before Phase 6, always.

Full plan with rationale, code patterns and exit criteria:
[REFACTOR_STRATEGY.md](REFACTOR_STRATEGY.md) · findings behind it:
[AUDIT.md](AUDIT.md#refactor-plan).

---

## Repository layout

```
backend/
  main.py         FastAPI app, the 9-stage pipeline, SSE streaming
  extraction.py   PDF → text/images → structured fields (3 routes)
  matching.py     PO lookup, split-PO balance maths, tolerance
  rules.py        Required fields, vendor, duplicates, final decision
  storage.py      SQLite: seed data, run history
  schemas.py      Shared dataclasses
  config.py       Operational settings, .env loading
frontend/         index.html, style.css, app.js — no build step
data/             Seed POs + vendors (tracked); app.db (not tracked)
sample_invoices/  7 PDFs, the generator, and manifest.json of scenarios
AUDIT.md              Architecture self-audit — what is wrong and why
REFACTOR_STRATEGY.md  Architect review — fix logic, schemas, sequencing
PROCESS_MAP.md        The on-paper design done before building
```
