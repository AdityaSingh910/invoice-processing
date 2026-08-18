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
| Extraction route in use | **`regex`** — no `ANTHROPIC_API_KEY` set |
| Automated tests | ✅ **7/7 passing** — `tests/test_samples.py`, manifest-driven, isolated DB |
| Known defects | **3 live bugs**, all documented, none fixed |
| Deployed | ❌ Local only, no git remote, no hosting |
| Demo video | ❌ Not recorded |

**Git:** local repo only, 7 commits, working tree clean.

```
(head)  Add tests/test_samples.py — Phase 0 Step 3, 7/7 passing
e103369 Add CLAUDE.md — session handoff document
f521b21 Update README for the architect review
ab1a559 Add REFACTOR_STRATEGY.md — architect review with implementation patterns
b2db656 Rewrite README for current state; fix stale requirements.txt
51e16b9 Reconcile main.py with extract_invoice(); pipeline runs again
56102dc Baseline: current state, pipeline broken mid-refactor
```

**Last verified 2026-08-18.** Re-checked against the code that session, all still
true: every line count in §6, every code reference in §8 (all three bugs), the six
API endpoints, the seed data, and the manifest order. The app was also run — all
three tabs render with no JS console errors. Nothing in this file was found stale
except its own commit count, now corrected.

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

⚠️ **The two LLM routes are wired but still never exercised end to end.**
Every run so far took `regex`, and sample 05 (scanned) lands on `route=none`
for want of a key.

What *has* been verified: the rasterisation half of the vision route. Running
`render_pages_png()` on `05_scanned_no_text.pdf` produces a clean, fully legible
1224×1584 PNG, so the pypdfium2 plumbing works. The only unproven link is the
API call itself (`extraction.py:170`). To enable, create `.env` in the project
root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is gitignored; the key is never sent to the browser. Model is
`claude-sonnet-5` (`config.EXTRACTION_MODEL`). Rasterisation uses **pypdfium2**
— a self-contained wheel, deliberately chosen so there is no poppler/tesseract
system dependency. OCR via pytesseract was removed entirely.

### Stack

FastAPI + SQLite + vanilla JS (no build step). `POST /api/runs/stream` streams
stages over SSE; the frontend reads it with `fetch()`.

---

## 6. Files

```
backend/
  main.py         283 lines. FastAPI app, the 9-stage pipeline as an async
                  generator yielding SSE events, _abort_unreadable() path,
                  all endpoints.
  extraction.py   394 lines. PDF → text (pdfplumber) → fields. Three routes,
                  SCHEMA_PROMPT for the LLM, regex fallback with tiered
                  patterns, _guess_vendor positional heuristic, PdfUnreadable.
  matching.py      84 lines. PO lookup (explicit refs then inferred),
                  tolerance_for(), empty_match(), split-PO balance maths.
  rules.py        151 lines. validate_required_fields, vendor_check (tri-state),
                  duplicate_check, and decide() — the only place a verdict is
                  produced.
  storage.py      196 lines. SQLite. Seeds POs/vendors from data/*.json on
                  EVERY startup. consumed_amount_for_po, find_duplicate,
                  save_run, list_runs.
  schemas.py       65 lines. ExtractedInvoice, LineItem, StageLog, RunResult.
  config.py        53 lines. .env loader, upload/page caps, model name.
                  Operational settings only — no business rules.
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
                       AND is the source of truth for tests
tests/
  test_samples.py  7 parametrized cases, one per sample, run sequentially in
                   manifest order against a temp DB. Verdicts come from
                   manifest.json; each case also pins the numbers behind the
                   verdict (PO balances, duplicate citation, extraction route).
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

### The demo money-shot

Run `03b` **alone against a fresh DB** → it comes back **APPROVED**. Same bytes,
opposite verdict, because $2,500 against an untouched $5,000 PO is an ordinary
partial invoice. The decision depends on the PO's history, not the file alone.
That is the single best thing to show in the video.

Watch "Remaining before" go **$5,000 → $2,000 → $0** across the three runs.

---

## 8. Known bugs — 3 live, none fixed

🐞 **1. Inferred PO match doesn't block approval.** (`matching.py:44-51` in `match_po`, `rules.py:103` in `decide`) When no PO reference is extracted, the process binds to the
vendor's nearest-amount PO with **no distance cap**, then logs a `warn`-level
reason — but `warn` doesn't change the verdict. An invoice that never named a PO
can auto-approve against a PO the process picked. Three defects in one: no cap,
no ambiguity handling, and a decorative severity level.

🐞 **2. Currency extracted, never read.** Zero references to `currency` in
`matching.py` or `rules.py` (verified by grep). The PO table *has* a `currency`
column nobody consults. A €3,000 invoice against a $5,000 PO is compared as
`3000` vs `5000`.

🐞 **3. PO ledger concurrency race.** Every `storage` function opens and closes
its own connection, so a transaction cannot span read-balance → decide → write.
Two concurrent invoices for one PO both read the same remaining balance and can
both be approved, committing more than the PO authorises. **The only defect that
produces a wrong number rather than a wrong routing.**

### Design gaps (deliberate, queued)

- Extracted fields are **bare values** — no confidence, no provenance. A total
  read off the page is indistinguishable from one the code synthesised as
  `subtotal + tax` (`extraction.py:315` in `regex_extract`).
- **All business rules hardcoded.** Tolerance is two magic numbers in a one-line
  function. `config.py` holds only operational settings.
- Vendor matching is **bidirectional substring** (`storage.py:112` in `find_vendor`) — `Acme
  Corp` matches approved `Acme Office Supplies`.
- **Reference data re-seeded from JSON on every startup**, so editing
  `purchase_orders.json` silently changes what historical runs mean.
- No arithmetic consistency check — nothing verifies `subtotal + tax == total`.
- `config.status()` and `config.extraction_mode()` are **dead code** — nothing
  calls them, so the UI has no way to show which extraction route is live.
- `_guess_vendor` (`extraction.py:238`) picks the vendor by **line position**.

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

### Remaining

**Immediate — nothing engineering-side is blocking.** Phase 0 is done. The two
outstanding items are *case-study deliverables*, and they outrank every phase
below: **record the 5-minute demo video**, and **produce a shareable link**
(no git remote, no deployment — GitHub repo, ngrok tunnel, or Render/Railway/Fly).
The work cannot be graded while it only runs on one laptop.

Notes on the suite, for whoever changes it next:

* It does **not** strip `ANTHROPIC_API_KEY`. With a key present the suite runs the
  real `llm (text)` and `llm (vision)` routes; without one it runs `regex` /
  `none`. The fixture prints which mode ran, because a green suite means a
  different thing in each. Know the tradeoff: in live mode the suite costs money,
  needs a network, and is only as reproducible as the model. **Nobody has run it
  in live mode yet** — there is still no `.env`.
* Cases share one DB and run in manifest order. An early failure cascades — that
  is inherent, since the later cases are *about* the state the earlier ones left.
* Verified to actually bite: running `-k overflow` alone turns `03b` **APPROVED**
  and fails the suite, which is the same-bytes/opposite-verdict demo from §7.

**Then (gated on user confirmation) — Phases 1-7**, per the table in README /
REFACTOR_STRATEGY:

| Phase | Work |
|---|---|
| 1 | Cap inferred PO matches + make `warn` bite; currency → review; vendor normalised-exact |
| 2 | `Tracked[T]` provenance wrapper, per-route confidence, **confidence gate** |
| 3 | `rules.yaml` versioned policy + typed loader |
| 4 | Transaction boundaries (`BEGIN IMMEDIATE` + WAL); `run_allocations` table |
| 5 | `DecisionTrace` + reference snapshot; stop re-seeding on startup |
| 6 | Line-item decomposition, multi-PO consolidation, FX provider |
| 7 | UI: confidence badges, evidence snippets, allocation view |

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
| Balance derived from run history, not a stored counter | No counter can drift out of sync with what was actually approved. |
| Only APPROVED runs consume budget | A flagged invoice mustn't block the queue behind it. |
| Refuse to guess when unreadable | Returns empty fields → review, rather than fabricating. `vendor_check` is deliberately **tri-state**: not-on-list (reject) ≠ couldn't-read-a-name (review). |
| pypdfium2 over pytesseract | Self-contained wheel; no system binaries for a reviewer to install. |
| No rule engine (JSON-logic etc.) | One-sided tolerance and ledger-derived balances express badly in a DSL; a sign error in exactly that comparison has already been a bug twice. YAML for policy, Python for predicates. |
| FX conversion must not widen auto-approval | Auto-approving on a rate fetched at run time makes the verdict depend on a third party and the clock. Default `convert_and_review`. |
| Pydantic `Field()` rejected for confidence | It is class-level schema metadata; confidence is per-instance data. Use a generic `Tracked[T]` wrapper. |
| Seed data tracked in git, `app.db` not | Tests assert against seed data; the DB is rebuilt on startup. |

### Bugs already found and fixed — don't reintroduce

1. `Total` regex matched inside `Subtotal` → wrong amount extracted.
2. Invoice-number regex matched prose in a footnote → a missing field looked present.
3. `abs()` in the tolerance check → every legitimate partial invoice flagged.
4. PO regex emitted both `1002` and `PO-1002` → deduplicated; a bare number could
   collide with an unrelated PO.
5. `requirements.txt` missing `pypdfium2`, still listing dead `pytesseract`.

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
