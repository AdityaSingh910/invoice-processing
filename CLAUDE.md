# CLAUDE.md — project context for a new session

Read this first. It is the handoff for an in-progress build.

---

## 1. What this is

A working AP (accounts payable) automation process built for the **Zamp AI
Solutions Associate case study, PS-1 (Finance / AP)**.

Upload a vendor invoice PDF → it runs through a 9-stage pipeline live in the
browser → produces **APPROVED / NEEDS_REVIEW / REJECTED** with a full reasoning
trail, plus a dashboard of every run.

**Case study deliverables:**
1. A working automated process, live and runnable, with an intuitive UI —
   a **live run view** (each stage as it executes) and a **dashboard** (history,
   status, outputs across runs). The UI is explicitly part of the grade.
2. A **5-minute demo video** showing the happy path plus at least one edge case.
3. Later: a live interview demo, running the process in real time and defending
   every design decision.

The grading criteria are: does it actually run, is the judgment behind the design
sound, and can it be explained to a **non-technical buyer**. Edge cases were
self-defined (2–4 required; there are 4).

**Working directory:** `c:\Users\adity\OneDrive\Desktop\Invoice processing`
Windows 11. PowerShell is primary; a Bash tool is also available.

---

## 2. Current status

| | |
|---|---|
| Pipeline | ✅ Working, 9 stages, streamed live over SSE |
| Samples | ✅ **7/7 passing** from a clean DB |
| UI | ✅ Run view, dashboard, reference tab; light + dark |
| Extraction route in use | **live** — `llm (text)` / `llm (vision)` verified against `gemini-3.7-flash` |
| Automated tests | ✅ **7/7 passing** — `tests/test_samples.py`, manifest-driven, isolated DB |
| Prompt-injection hardening | ✅ Fenced prompt, closed response schema, post-extraction guard — 34 tests |
| Known defects | ✅ **all 3 documented bugs FIXED** (§8) |
| Deployed | ❌ Local only, no git remote, no hosting |
| Demo video | ❌ Not recorded |

**Git:** local repo only, 16 commits at the time of writing (this update is
the 17th), working tree clean.

```
2865a58 Harden the PO ledger: atomicity, reversal + cascade, configurable tolerance
056d0f2 Harden the extraction layer against indirect prompt injection
b7f4b00 Update CLAUDE.md: Gemini swap, live route verification, and what it cost
d7790c3 Fix Gemini client lifetime and model pin; vision route verified working
5ed4a53 Swap the LLM extraction layer from Anthropic Claude to Google Gemini
dd9c1bc Update CLAUDE.md: current git log, line counts, unproven LLM routes
21bc180 Implement pytest suite and enable API vision extraction for sample 05
bedc0db Add tests/test_samples.py — Phase 0 Step 3, 7/7 passing
e103369 Add CLAUDE.md — session handoff document
f521b21 Update README for the architect review
ab1a559 Add REFACTOR_STRATEGY.md — architect review with implementation patterns
b2db656 Rewrite README for current state; fix stale requirements.txt
51e16b9 Reconcile main.py with extract_invoice(); pipeline runs again
56102dc Baseline: current state, pipeline broken mid-refactor
```

**Last verified 2026-08-19.** Re-checked against the code: every line count in
§6, every code reference in §8 (all three bugs), the six API endpoints, the seed
data, and the manifest order. The app was run — all three tabs render with no JS
console errors.

**The LLM routes are no longer a promise.** Both have now made real API calls
against `gemini-3.7-flash` with a live key. Sample 05 was extracted from a page
image and came back APPROVED end to end, every expected field matching:

```
Route Used       llm-vision                (expected llm-vision)
Vendor Name      Stark Industrial Parts    (expected Stark Industrial Parts)
Invoice Number   INV-9004                  (expected INV-9004)
PO Reference     ['PO-1005']               (expected PO-1005)
Total Amount     15400.0                   (expected 15400.00)
invoice_date     2026-07-22   currency USD   (bonus, not asserted)
```

`pytest` is 7/7 with the live routes active (65s, against 16s on regex).

**The remaining caveat is quota, not correctness** — see the 429 gotcha in §4.

Since then, two hardening rounds landed and are covered by **46 tests**: prompt
injection (§5) and the PO ledger (§5, §8 #3). The concurrency race was verified
fixed under real threads, and reversal + cascade end to end over HTTP.

---

## 3. ⚠️ Standing instruction — do not skip

**Phase 1 is gated. Do not start it without explicit user confirmation.**

The user asked for Phase 0 in three steps, stopping and committing after each:
1. ✅ git init + baseline commit
2. ✅ reconcile `main.py`, get 7/7 green
3. ✅ `tests/test_samples.py` as a real pytest file — **7/7 passing**

**Phase 0 is complete.** They said: *"Don't start Phase 1 until I confirm."*
That still holds, and was reaffirmed when Step 3 was commissioned. Phase 1
touches decision behaviour (capping inferred PO matches, making `warn` bite),
so the suite now exists precisely to catch regressions there — but do not
start it unprompted.

The user also prefers **one step at a time with a commit after each**, not
batched work.

---

## 4. Running it

```powershell
# Tests -- no server needed, runs the pipeline in-process against a temp DB
.\venv\Scripts\python.exe -m pytest tests/ -v

# Full start (creates venv, installs deps, generates samples, opens browser)
.\start.ps1

# Manual
.\venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Then <http://127.0.0.1:8000>.

### Hard-won gotchas — these cost time already

**The server holds `data/app.db` open.** `rm data/app.db` fails with *"Device or
resource busy"* while it runs. Always kill the server first:

```bash
netstat -ano | grep ":8000" | grep LISTENING | awk '{print $5}' \
  | while read pid; do taskkill //F //PID $pid >/dev/null 2>&1; done
sleep 1.5 && rm -f data/app.db
```

**Starting a `TestClient` wipes any PO you inserted.** FastAPI's startup event
calls `init_db()`, which does `DELETE FROM purchase_orders` and re-seeds from
`data/purchase_orders.json`. Insert fixture POs *after* entering the TestClient
context, not before, or they vanish and every balance assertion reads `None`.
This is the re-seed-on-startup design gap in §8 showing its teeth.

**The Gemini free tier rate-limits hard (HTTP 429).** Running all 7 samples
back to back exhausts quota within a minute, and the pipeline then falls back to
`regex` -- silently, by design, since a note is attached but the verdict still
comes out right on these fixtures. Verified: 4 of 7 samples fell back this way on
a second consecutive full run. **Before demoing "the AI reads it", pause between
runs or the live route may not actually fire.** A 429 is indistinguishable from
any other API failure in the run view; both read as "used regex instead".

**`/tmp` is not reliably writable** in this Git Bash environment. Use the
scratchpad directory for temp files, not `/tmp` and not the project root.

**SSE output ends with a blank line**, so `curl ... | tail -1` returns nothing.
Use `grep '"final"'` to pull the result event.

**Sample order matters** (see §7) — several cases are history-dependent by
design. Running them out of order gives the wrong verdict and looks like a bug.

**PDFs must stay binary in git.** `.gitattributes` has `*.pdf binary`. Without
it Git on Windows applies CRLF conversion and silently corrupts the fixtures on
checkout — it warned about exactly this before the attribute was added.

---

## 5. Architecture

### Core philosophy — the thing to say in the interview

> **The AI reads, the rules decide.**

Extraction is hard for code and easy for an LLM (every vendor formats invoices
differently). The *decision* must be identical every time and defensible to an
auditor, so **no model ever touches a dollar comparison**. Everything downstream
of extraction is deterministic Python. No prompt contains the words approve,
reject, or tolerance.

The audit confirmed this holds. The caveat worth stating honestly: the LLM still
*chooses the inputs* the verdict is computed from (which number is the total,
which strings are PO references), and those currently arrive as bare floats the
rules trust absolutely. That is what Phase 2 fixes.

### Pipeline

```
INGEST → EXTRACT_TEXT → EXTRACT_FIELDS → VALIDATE → VENDOR_CHECK
       → PO_MATCH → DUPLICATE_CHECK → TOLERANCE_CHECK → DECISION
```

Stages **do not short-circuit**. A missing invoice number at stage 4 does not
stop stages 5–8; findings accumulate and only the final stage judges. An AP clerk
should see the whole picture, not the first thing that went wrong.

One wrinkle worth knowing before reading the code: the tolerance **arithmetic**
happens in stage 6 (`PO_MATCH` → `matching.match_po`), which computes
`remaining_before`, `tolerance`, `diff` and `within_tolerance` in one pass.
Stage 8 (`TOLERANCE_CHECK`) only *reports* what stage 6 already decided. The
stage split exists for the run view, not for the logic.

### Decision hierarchy

- **REJECTED** — must not be overridden automatically: duplicates, vendor on file
  but not approved.
- **NEEDS_REVIEW** — recoverable: missing fields, unreadable scan, over tolerance,
  no PO match.
- **APPROVED** — everything passed.

Reject wins over review when both fire.

### Tolerance is deliberately one-sided

```python
within = diff <= tol        # NOT abs(diff) <= tol
```

Billing **over** the remaining PO balance is a problem — the vendor wants money
nobody authorised. Billing **under** it is a normal partial invoice. Tolerance is
`max(2% of remaining, $25)`.

This was originally written with `abs()` and it was wrong — it flagged every
legitimate split-PO invoice. Good story: the tests caught it.

### Split-PO tracking

No `consumed` column. Remaining balance is derived per run:

```
remaining_before = PO_amount − Σ(totals of prior APPROVED runs on that PO)
```

Only **APPROVED** runs consume budget, so a flagged invoice doesn't block the
queue behind it, and the run history *is* the ledger — no counter can drift.

### Extraction routes

Same output schema regardless, so matching and rules never know which ran.

| Route | When | Needs key |
|---|---|---|
| `llm (text)` | PDF has an embedded text layer | Yes |
| `llm (vision)` | Scanned PDF — pages rasterised and sent to the model | Yes |
| `regex` | No key, or LLM call failed | No |
| `none` | Nothing readable — returns empty fields rather than guessing | — |

✅ **Both LLM routes are verified working** against `gemini-3.7-flash`
(2026-08-19). `llm (vision)` reads `05_scanned_no_text.pdf` correctly — vendor,
invoice number, PO reference, total, and date — and the invoice reaches APPROVED
end to end. `llm (text)` also returns clean fields. Entry points:
`llm_extract_vision` at `extraction.py:302`, `llm_extract_text` at
`extraction.py:287`.

The provider is configured through `.env` in the project root:

```
GEMINI_API_KEY=...
```

`.env` is gitignored; the key is never sent to the browser. Get a key free at
<https://aistudio.google.com/apikey>.

Provider is **Google Gemini** via Google AI Studio (`google-genai`); model is
`gemini-3.7-flash` (`config.EXTRACTION_MODEL`; the env var name lives in
`config.API_KEY_ENV`, so swapping providers again touches one constant). Both
routes ask for `response_mime_type="application/json"`, but `_parse_llm_json`
still runs on the reply — a mime type is a strong constraint, not a guarantee.

Rasterisation uses **pypdfium2** — a self-contained wheel, deliberately chosen so
there is no poppler/tesseract system dependency. OCR via pytesseract was removed
entirely.

**Model pinning is deliberate.** `gemini-flash-latest` is an available alias and
is *not* used: an alias changes the model under a running system, and an AP
process must be able to say which model read an invoice approved months ago.

### PO ledger: reversal, cascade, tolerance

**There is no `remaining_amount` column, and there must not be one.** Balance is
derived on every read (`po.amount - SUM(approved run totals)`). Two of the
hardest problems in AP balance tracking are therefore not problems here:

* **Idempotency is structural.** Nothing is deducted, so nothing can be deducted
  twice. Re-evaluating an approved invoice recomputes the same sum. A stored
  counter would need a guard flag and would be one missed path away from
  double-spending a PO.
* **Reversal is structural.** `set_run_status()` moving a run out of APPROVED
  drops it from the SUM in the same instant. There is no refund step to forget,
  and no way for a counter to drift from what was actually approved.

What is *not* free, and is implemented:

* **Atomicity** — see §8 #3.
* **Cascade re-evaluation** — `rules.reevaluate_po_queue(po_number)`. When budget
  frees up, held invoices are re-checked oldest-first, so the invoice that queued
  first gets the money. Exposed as `POST /api/runs/{id}/status`.
  **Only invoices held purely on balance are eligible.** One held for a missing
  invoice number, a duplicate, or a security flag stays held — otherwise a
  reversal becomes a way to launder a blocked invoice into APPROVED. Tested.
* **Configurable tolerance** — `config.PO_TOLERANCE_PERCENT` (1%) and
  `PO_TOLERANCE_DOLLARS` ($50), whichever is larger. Covers tax and freight added
  after a PO is raised. An invoice approved *over* the balance is never silent:
  `over_within_tolerance` triggers a `warn`-level audit note naming the overage
  in dollars. Under-billing is unbounded and handled separately as a partial.

### Prompt-injection defence

A vendor invoice is **attacker-controlled input**. Anyone who can send an invoice
can print text on it addressed to the extractor. Four controls, in descending
order of how much weight they actually carry:

1. **Architecture (was already there).** No model output reaches a verdict.
   `decide()` computes status from numbers and the PO ledger. There is no field
   an extractor could set that changes it — so the blast radius of a successful
   injection is *wrong numbers*, never *wrong decision*.
2. **Closed response schema** (`extraction.RESPONSE_SCHEMA`). The reply is
   decoded against a fixed shape, so a document demanding `{"status":"APPROVED"}`
   cannot produce that key. The bad output is unrepresentable, not merely
   discouraged. This is the load-bearing *new* control.
3. **Fenced prompt.** Document text is wrapped in
   `<untrusted_document_content>` via `wrap_untrusted()`, which also defangs a
   closing tag already inside the document — otherwise a document could close the
   fence early and have the rest read as trusted prompt. Images get the same
   fence: text printed on a scan is exactly as untrusted as a text layer.
   `SCHEMA_PROMPT` frames the model as a passive transcriber with no authority
   and explicitly tells it to transcribe hostile text rather than obey or drop it.
4. **Post-extraction guard** (`validate_extracted_security`). Scans extracted
   strings *and* `raw_text` for instruction-shaped phrases, and forces
   NEEDS_REVIEW. Never raises — a guard that crashes is free denial of service.

**It forces review, never rejection.** Auto-rejecting on a keyword would hand
anyone a way to block a competitor's payment by printing a phrase on an invoice.
A duplicate still outranks it and still REJECTs; there is a test for that.

Two findings worth keeping:

* **Field-only scanning was not enough.** The regex extractor drops a hostile
  line that matches no field pattern, so the guard saw nothing while the
  injection sat in the document in plain sight. What an indirect injection
  targets is the text the *model* reads, so `raw_text` is screened too. Found by
  the end-to-end test, not by reading the code.
* **The patterns are deliberately narrow.** Scoring or fuzzy matching would flag
  "System Integration Services" and train a clerk to click through the warning —
  worse than no guard. `tests/test_security.py` pins six benign-but-similar
  strings that must stay silent.

### Stack

FastAPI + SQLite + vanilla JS (no build step). `POST /api/runs/stream` streams
stages over SSE; the frontend reads it with `fetch()`.

---

## 6. Files

```
backend/
  main.py         341 lines. FastAPI app, the 9-stage pipeline as an async
                  generator yielding SSE events, _abort_unreadable() path,
                  all endpoints.
  extraction.py   633 lines. PDF → text (pdfplumber) → fields. Three routes,
                  SCHEMA_PROMPT for the LLM, regex fallback with tiered
                  patterns, _guess_vendor positional heuristic, PdfUnreadable.
                  The ONLY module that talks to a model — google-genai is
                  imported lazily inside _client() so the regex route still
                  works where the SDK was never installed.
  matching.py      153 lines. PO lookup (explicit refs then inferred),
                  tolerance_for(), empty_match(), split-PO balance maths.
  rules.py        281 lines. validate_required_fields, vendor_check (tri-state),
                  duplicate_check, and decide() — the only place a verdict is
                  produced.
  storage.py      356 lines. SQLite. Seeds POs/vendors from data/*.json on
                  EVERY startup. consumed_amount_for_po, find_duplicate,
                  save_run, list_runs.
  schemas.py       65 lines. ExtractedInvoice, LineItem, StageLog, RunResult.
  config.py        82 lines. .env loader, upload/page caps, API_KEY_ENV
                  ("GEMINI_API_KEY"), EXTRACTION_MODEL, api_key(),
                  has_api_key(). Operational settings only — no business rules.
frontend/
  index.html      Three tabs: Run, Dashboard, Reference.
  style.css       CSS custom properties, dark mode via prefers-color-scheme.
  app.js          SSE consumption, live stage rendering, PO balance bar,
                  dashboard, modal. No framework, no build.
data/
  purchase_orders.json / approved_vendors.json   Seed data (TRACKED in git)
  app.db                                         Runtime DB (NOT tracked)
sample_invoices/
  *.pdf (7)            Test fixtures
  generate_invoices.py Regenerates them (reportlab)
  manifest.json        Scenario labels + expected verdicts; drives the UI list
                       AND is the source of truth for tests. Sample 05 carries
                       BOTH `expect` and `expect_with_vision` — see §7.
tests/
  test_currency.py       16 cases. Matching currencies unchanged, mismatch →
                   review with a clear finding, no conversion, unknown currency
                   not invented, and REJECTED still outranking the review.
  test_inferred_po.py    13 cases. The distance cap, the ambiguity guard, that
                   the inferred warning drives the verdict, and that explicit
                   matching / split-PO / duplicate behaviour did not move.
  test_po_edge_cases.py  12 cases. Split-PO execution, idempotency under repeat
                   evaluation, reversal + cascade (including what must NOT
                   cascade), tolerance boundaries, and the concurrency race
                   driven with real threads.
  test_security.py 27 cases. Injection detection, false-positive floor, prompt
                   and schema shape, decision-layer behaviour, and one hostile
                   PDF built at test time (never committed) driven end to end.
  test_samples.py  269 lines. 7 parametrized cases, one per sample, run sequentially in
                   manifest order against a temp DB. Verdicts come from
                   manifest.json, resolved against config.has_api_key(); each
                   case also pins the numbers behind the verdict (PO balances,
                   duplicate citation, extraction route). Does NOT strip the API
                   key -- with one present it runs the real LLM routes.
AUDIT.md              What is wrong and why — the self-audit
REFACTOR_STRATEGY.md  How to fix it — architect review, code patterns
PROCESS_MAP.md        The on-paper design done before building
README.md             Project overview + status
```

### API endpoints

```
POST /api/runs/stream          multipart PDF → SSE stream of stages, then final
GET  /api/runs                 run history
GET  /api/runs/{id}            single run
POST /api/runs/{id}/status     change a run's status; cascades to invoices held
                               on the same PO. Body: {"status": ..., "note": ...}
GET  /api/reference            POs + vendors
GET  /api/sample-invoices      list + manifest metadata
GET  /api/sample-invoices/{n}  fetch one sample PDF
```

### Seed data

| PO | Vendor | Amount | Status |
|---|---|---|---|
| PO-1001 | Acme Office Supplies | $1,240.00 | open |
| PO-1002 | Globex Logistics | $5,000.00 | open |
| PO-1003 | Initech Consulting | $8,200.00 | open |
| PO-1004 | Umbrella Cleaning Co | $600.00 | **closed** |
| PO-1005 | Stark Industrial Parts | $15,400.00 | open |

All five vendors are `approved` (V-001 … V-005).

---

## 7. Sample invoices — **order matters**

| Order | File | Scenario | Expected |
|---|---|---|---|
| 1 | `01_happy_path_acme.pdf` | Clean, explicit PO, within tolerance | APPROVED |
| 2 | `02_split_po_globex_a.pdf` | $3,000 partial against PO-1002 | APPROVED |
| 3 | `03_split_po_globex_b.pdf` | $2,000, exactly exhausts PO-1002 | APPROVED |
| 4 | `03b_split_po_globex_overflow.pdf` | $2,500 against exhausted PO | NEEDS_REVIEW |
| 5 | `04_missing_invoice_number.pdf` | Amounts fine, no audit key | NEEDS_REVIEW |
| 6 | `05_scanned_no_text.pdf` | Image-only PDF | NEEDS_REVIEW † |
| 7 | `06_duplicate_of_01.pdf` | Resubmission of INV-2201 | REJECTED |

**Run 2 → 3 → 4 in order** or the split-PO story doesn't work. **Run 1 before 7**
or the duplicate has nothing to collide with.

† **Sample 05's verdict is route-dependent, and deliberately so.** With no key
there is nothing to read, so the process refuses to guess → NEEDS_REVIEW. With a
key the vision route reads INV-9004 / PO-1005 / $15,400.00 off the page image,
which matches open PO-1005 exactly → **APPROVED**. The manifest carries both:
`expect` and `expect_with_vision`. The test suite and `/api/sample-invoices`
both resolve against `config.has_api_key()`, so the UI badge can never
contradict the run beside it. Verified by flipping the flag; the other six
samples do not move.

**Both halves are now observed, not predicted.** With a live key the vision
route returns Stark Industrial Parts / INV-9004 / PO-1005 / $15,400.00 and the
run reaches APPROVED. Without one it is `route=none` → NEEDS_REVIEW. This is the
*second* same-bytes/opposite-verdict story in the project, and a better one than
the split-PO case for a non-technical audience: the file did not change, the
capability did.

### The demo money-shot

Run `03b` **alone against a fresh DB** → it comes back **APPROVED**. Same bytes,
opposite verdict, because $2,500 against an untouched $5,000 PO is an ordinary
partial invoice. The decision depends on the PO's history, not the file alone.
That is the single best thing to show in the video.

Watch "Remaining before" go **$5,000 → $2,000 → $0** across the three runs.

---

## 8. Known bugs — 3 live, none fixed

✅ **1. Inferred PO match doesn't block approval — FIXED** (Phase 1, 2026-08-19).
Was three defects in one: no distance cap, no ambiguity handling, and a
decorative severity level. `min(pos, key=nearest)` always returned something, so
an invoice that never named a PO could auto-approve against one the process
guessed.

Now, in `match_po`, inference must pass **both** guards: the PO amount within
`tolerance_for()` of the invoice total (reusing the configured tolerance rather
than a second magic number), and **exactly one** candidate qualifying. Failing
either binds nothing, and `po_match["inference"]` records why
(`"ambiguous"` / `"no_close_candidate"`) so the trail explains itself. In
`decide()`, an accepted inferred match now sets `review = True`: the invoice
never named this PO, so the match is a suggestion for a human to confirm, not
grounds for approval. Explicit references are untouched and still authoritative.

Verified against the pre-change code: it APPROVED a $1,000 invoice that named no
PO, and silently picked one of two identical $1,000 POs. Both are NEEDS_REVIEW
now. 13 tests in `tests/test_inferred_po.py`.

✅ **2. Currency extracted, never read — FIXED** (Phase 1, 2026-08-19). Was:
zero references to `currency` in `matching.py` or `rules.py`, so a €3,000 invoice
against a $5,000 PO was compared as `3000` vs `5000` and read as a comfortable
partial.

`match_po` now emits `invoice_currency`, `po_currency` and `currency_mismatch`
(compared case- and whitespace-insensitively, and **only when both sides are
known** — an absent currency is not evidence of a different one). `decide()`
turns a mismatch into NEEDS_REVIEW with a `fail`-level finding, placed *before*
the amount reasoning because when the units differ none of that reasoning means
anything.

**No conversion, no rate lookup, no FX provider** — a verdict that depended on a
rate fetched at run time would not be reproducible by an auditor, which is the
property the whole design exists to protect. Amounts pass through untouched.

Verified end to end: a EUR invoice against an untouched USD PO-1002 has
`within_tolerance=True` and `is_partial=True` — every numeric check passes — yet
returns NEEDS_REVIEW on exactly one fail-level finding, and consumes no budget.
16 tests in `tests/test_currency.py`.

⚠️ **Known limitation, unchanged by this fix:** the extractor falls back to
`"USD"` when a document carries no currency signal, so a genuinely unmarked
invoice is indistinguishable from a USD-marked one. That is an extraction
concern, not a matching one, and was left alone deliberately.

✅ **3. PO ledger concurrency race — FIXED.** Was: every `storage` function opened
its own connection, so a transaction could not span read-balance → decide →
write, and two concurrent invoices for one PO could both approve past the
balance. The only defect that produced a wrong *number* rather than a wrong
routing.

Fixed by `storage.write_txn()` (`BEGIN IMMEDIATE` + WAL) and
`save_run_checked()`, which re-reads the consumed total inside the write lock and
downgrades a stale APPROVED to NEEDS_REVIEW before inserting. The verdict is
still computed outside the lock — it has to be, since extraction can take seconds
and holding a write lock across a model call would serialise the whole system —
so this is optimistic concurrency with an authoritative final check.

Verified under real threads: 8 concurrent $2,000 invoices against a $10,000 PO
yield exactly 5 APPROVED, 3 NEEDS_REVIEW, $0.00 remaining. Before the fix all 8
approved and the PO was overspent by $6,000.

### Design gaps (deliberate, queued)

- Extracted fields are **bare values** — no confidence, no provenance. A total
  read off the page is indistinguishable from one the code synthesised as
  `subtotal + tax` (`extraction.py:455` in `regex_extract`).
- **All business rules hardcoded.** Tolerance is two magic numbers in a one-line
  function. `config.py` holds only operational settings.
- Vendor matching is **bidirectional substring** (`storage.py:160` in `find_vendor`) — `Acme
  Corp` matches approved `Acme Office Supplies`.
- **Reference data re-seeded from JSON on every startup**, so editing
  `purchase_orders.json` silently changes what historical runs mean.
- No arithmetic consistency check — nothing verifies `subtotal + tax == total`.
- `config.status()` and `config.extraction_mode()` are **dead code** — nothing
  calls them, so the UI has no way to show which extraction route is live.
- `_guess_vendor` (`extraction.py:378`) picks the vendor by **line position**.

Full detail: [AUDIT.md](AUDIT.md). Fix patterns: [REFACTOR_STRATEGY.md](REFACTOR_STRATEGY.md).

---

## 9. Done vs remaining

### Done

- Process map on paper, then the build; 7 sample PDFs + PO/vendor dataset.
- Full pipeline, SSE live run view, dashboard, reference tab.
- **UI rebuild**: verdict bar, segmented **PO balance bar** (consumed / this
  invoice / remaining, overflow hatched red), severity-coded reasoning,
  per-stage timings, labelled samples, PO-consumption view, dark mode.
- "Accept any external PDF" extraction rewrite (LLM text + vision + better regex).
- `AUDIT.md` — honest self-audit answering 5 architecture questions.
- `REFACTOR_STRATEGY.md` — architect review with implementation patterns.
- **Phase 0 complete** — Steps 1 & 2 (git baseline; reconciliation), and
  Step 3: `tests/test_samples.py`, 7/7 green, isolated from `data/app.db`.
- Fixed `requirements.txt` — it was missing `pypdfium2` and would have broken a
  fresh clone.
- **Provider swapped to Google Gemini** (`google-genai`, Google AI Studio),
  replacing Anthropic. Contained entirely to the extraction layer — `matching.py`
  and `rules.py` are byte-for-byte unchanged, which is the whole point of the
  architecture. `anthropic` removed from `requirements.txt` and the venv.
- **Both LLM routes verified live** — see §2. This closed the largest standing
  unknown in the project.
- **Prompt-injection hardening** — fenced prompt, closed response schema,
  post-extraction guard, 27 security tests. A hostile invoice that would
  otherwise auto-approve (approved vendor, matched PO, within tolerance, no
  duplicate) is now held for review.
- **API failures now say why** — `describe_api_error()` maps status codes, so
  "out of quota" is distinguishable from "bad key" without a debugger. This is a
  security concern, not just convenience: when the LLM route fails it falls back
  to regex, which means the hardened prompt is not running.

### Remaining

**Immediate — nothing engineering-side is blocking.** Phase 0 is done and the
LLM routes are proven. The two outstanding items are *case-study deliverables*,
and they outrank every phase below:

1. **Record the 5-minute demo video.** Read the 429 gotcha in §4 first — pace the
   runs, or the live route quietly stops firing mid-demo.
2. **Produce a shareable link.** No git remote, no deployment — options are a
   GitHub repo, an ngrok tunnel, or Render/Railway/Fly. Note that a real
   deployment needs `GEMINI_API_KEY` set as a host env var, not a committed
   `.env`.

The work cannot be graded while it only runs on one laptop. Neither item needs
another line of pipeline code.

The suite is now five files — `test_samples.py` (7 end-to-end sample verdicts),
`test_security.py` (27 injection cases), `test_po_edge_cases.py` (12 ledger
cases), `test_inferred_po.py` (13 matching cases), `test_currency.py` (16
currency cases). **75 total.** Notes for whoever changes them next:

* It does **not** strip `GEMINI_API_KEY`. With a key present the suite runs the
  real `llm (text)` and `llm (vision)` routes; without one it runs `regex` /
  `none`. The fixture prints which mode ran, because a green suite means a
  different thing in each. **Both modes have now been run green** — 16s on regex,
  65s live. In live mode the suite needs a network and burns quota, and a 429
  makes it fall back rather than fail, so a green live run is not by itself proof
  the model was consulted. Read the printed mode and the routes.
* Cases share one DB and run in manifest order. An early failure cascades — that
  is inherent, since the later cases are *about* the state the earlier ones left.
* Verified to actually bite: running `-k overflow` alone turns `03b` **APPROVED**
  and fails the suite, which is the same-bytes/opposite-verdict demo from §7.
* `test_po_edge_cases.py` uses a per-test `db` fixture and drives rules/storage
  directly rather than through PDFs — the scenarios are about the ledger, and
  generating $1,000,000 fixture invoices would test reportlab, not balance logic.
* The concurrency test uses real threads and a `Barrier`, so it exercises actual
  SQLite locking. Its retry-on-locked loop is not incidental: it is what a caller
  must do, and is part of what the test asserts.

**Then (gated on user confirmation) — Phases 1-7**, per the table in README /
REFACTOR_STRATEGY:

| Phase | Work | State |
|---|---|---|
| 1 | Cap inferred PO matches + make `warn` bite; currency → review; vendor normalised-exact | ◨ **inferred-PO and currency DONE**; vendor normalised-exact still to do |
| 2 | `Tracked[T]` provenance wrapper, per-route confidence, **confidence gate** | ⬜ |
| 3 | `rules.yaml` versioned policy + typed loader | ◨ tolerance moved to `config.py`; YAML + loader still to do |
| 4 | Transaction boundaries (`BEGIN IMMEDIATE` + WAL); `run_allocations` table | ◨ **transactions DONE** (§8 #3); allocations table still to do |
| 5 | `DecisionTrace` + reference snapshot; stop re-seeding on startup | ⬜ |
| 6 | Line-item decomposition, multi-PO consolidation, FX provider | ⬜ |
| 7 | UI: confidence badges, evidence snippets, allocation view | ⬜ |

The `run_allocations` table is the remaining half of Phase 4 and it still gates
Phase 6: the schema stores one `po_number` per run and consumption sums run
totals, so a consolidated invoice would over-consume every PO it touched. The
transaction work landing does **not** unblock multi-PO.

**Sequencing trap:** multi-PO consolidation is a **ledger** feature, not a
matching feature. The schema stores one `po_number` per run and consumption sums
run totals, so a consolidated invoice would over-consume every PO it touched.
Phase 4 must land before Phase 6.

**For the case study itself, still outstanding:** record the 5-minute demo
video; decide on a shareable link (no git remote, no deployment — options are a
GitHub repo, an ngrok tunnel, or a real deploy to Render/Railway/Fly).

---

## 10. Decisions already made — don't relitigate

| Decision | Why |
|---|---|
| **PS-1** over PS-2/PS-3 | Only PS where inputs are real artefacts and the decision is verifiable. PS-3 needs live web research — fragile in a live demo, and the output can't be proved correct. |
| Rules deterministic, LLM extraction-only | Auditability. It is the headline claim and it survived the audit. |
| Three verdicts, not two | Binary would force guessing on ambiguous invoices; the middle state is where automation hands back to a human. |
| Tolerance one-sided | Over-billing is a problem; under-billing is a normal partial invoice. |
| Balance derived from run history, not a stored counter | No counter can drift out of sync with what was actually approved — and it makes idempotency and reversal structural rather than defended. Reaffirmed 2026-08-19 when a `remaining_amount` column was proposed. |
| Only APPROVED runs consume budget | A flagged invoice mustn't block the queue behind it. |
| Refuse to guess when unreadable | Returns empty fields → review, rather than fabricating. `vendor_check` is deliberately **tri-state**: not-on-list (reject) ≠ couldn't-read-a-name (review). |
| pypdfium2 over pytesseract | Self-contained wheel; no system binaries for a reviewer to install. |
| No rule engine (JSON-logic etc.) | One-sided tolerance and ledger-derived balances express badly in a DSL; a sign error in exactly that comparison has already been a bug twice. YAML for policy, Python for predicates. |
| FX conversion must not widen auto-approval | Auto-approving on a rate fetched at run time makes the verdict depend on a third party and the clock. Default `convert_and_review`. |
| Pydantic `Field()` rejected for confidence | It is class-level schema metadata; confidence is per-instance data. Use a generic `Tracked[T]` wrapper. |
| Seed data tracked in git, `app.db` not | Tests assert against seed data; the DB is rebuilt on startup. |
| **Google Gemini over Anthropic** | User's call, 2026-08-19. Free tier removes the "do I want to pay to demo this" question. The swap touched only `extraction.py` + `config.py` — proof the provider was never load-bearing. |
| Model **pinned**, not `gemini-flash-latest` | An alias changes the model under a running system. An AP process must be able to say which model read an invoice approved months ago. |
| `gemini-3.7-flash` over `gemini-3.6-flash` | Both extracted every field correctly from the scan; 3.7 did it in 3.7s vs 11.6s. Speed matters in a live demo. |
| Injection guard forces review, never reject | Auto-rejecting on a keyword lets anyone block a competitor's payment by printing a phrase on their invoice. |
| Injection patterns narrow, not fuzzy | A guard that flags "System Integration Services" trains clerks to click through warnings — worse than no guard. |
| Guard never edits the invoice | The run view should show what the document actually said; the value is that a human sees the real text. |
| Test suite honours a live key rather than mocking | A mocked LLM proves nothing about whether extraction works. The cost is non-determinism, accepted knowingly — see §9. |

### Bugs already found and fixed — don't reintroduce

1. `Total` regex matched inside `Subtotal` → wrong amount extracted.
2. Invoice-number regex matched prose in a footnote → a missing field looked present.
3. `abs()` in the tolerance check → every legitimate partial invoice flagged.
4. PO regex emitted both `1002` and `PO-1002` → deduplicated; a bare number could
   collide with an unrelated PO.
5. `requirements.txt` missing `pypdfium2`, still listing dead `pytesseract`.
6. **Gemini client garbage-collected mid-call.**
   `_client().models.generate_content(...)` left the `Client` unreferenced;
   google-genai closes its HTTP transport when the Client is collected, so every
   call died with *"Cannot send a request, as the client has been closed."* Hold
   the client in a local. Note what made this expensive: the regex fallback
   *masked* it — it surfaced as a tidy `route=none`, not a crash.
7. **`gemini-2.0-flash` is retired** — the API 404s on it. Ask the API what it
   can reach (`client.models.list()`) rather than trusting a model name from
   documentation or memory.

---

## 11. Working conventions

**Commits:** end every message with

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Messages so far are detailed — what changed, why, and what was verified. Match
that. Commit after each discrete step, not in batches.

**Verify empirically, don't assume.** Every claim in `AUDIT.md` was checked by
reading code or grepping. When the app was broken, that was confirmed by running
it, not inferred. Keep doing that.

**Be honest about what doesn't work.** The user has responded well to being told
the app was broken, that `requirements.txt` would break a clone, and that the LLM
routes are untested. Don't paper over gaps.

**Temp scripts:** the scratchpad directory, or `_*.py` in the project root
(gitignored). Delete them afterwards.

**Browser checks:** Playwright + Chromium are installed in the venv. Useful for
screenshotting the UI and catching JS console errors.

```bash
# Run the 7 samples against a live server (throwaway pattern; formalise in Step 3)
./venv/Scripts/python.exe -c "import json; ..."   # see git history for check.py
```
