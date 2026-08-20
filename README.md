# Invoice Processing — PDF to Decision

An automated AP process: sign in, upload a vendor invoice PDF, watch it move
through extraction, validation, PO matching and rule checks live, and get a
decision — **APPROVED** / **NEEDS_REVIEW** / **REJECTED** — with a structured
audit trail behind it, a human accept/reject path for anything held, and a
dashboard of every run.

Built for the Zamp AI Solutions Associate case study, **PS-1 (Finance / AP)**.

---

## Where the project stands right now

**The app runs. All 7 sample invoices produce their expected verdicts, and the
suite is green.**

| | |
|---|---|
| Pipeline | Working, 9 stages, streamed live to the browser |
| Sample invoices | 7 / 7 matching the manifest, driven through the real pipeline |
| UI | **Next.js 15 + React 19 + Tailwind v4**, four sections, light + dark |
| Extraction | **Groq** for text PDFs, **Gemini Vision** for scans |
| Automated tests | **359 passing** deterministically, 15 files, no live API calls |
| Audit trail | Structured, deterministic, emitted by the rule engine itself |
| Human review | Accept / reject on NEEDS_REVIEW, recorded beside the automated decision |
| API security | OAuth 2.0 bearer tokens, scopes, rate limits, input validation |
| Non-invoice detection | Rejects documents that contain no invoice, saying so |
| Demo reset | One click for an admin, or `.\reset-demo.ps1` |
| Original audit defects | **All fixed** — see [Known problems](#known-problems) |
| Deployed anywhere | No — runs locally only |
| Demo video | Not recorded |

[AUDIT.md](AUDIT.md) is the deliberately unflattering self-review that started
most of this work, and [REFACTOR_STRATEGY.md](REFACTOR_STRATEGY.md) is the
architect-level response. Both are still worth reading, but note that the live
bugs they describe have since been fixed — they are a record of how the design
was arrived at, not of the current state.

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

**2. UI rebuild.** Verdict bar, a segmented **PO balance bar** (consumed / this
invoice / remaining, overflow hatched red), severity-coded reasoning, per-stage
timings, labelled sample scenarios, PO-consumption view, dark mode.

**3. "Accept any external PDF" work.** Rewrote extraction to handle invoices it
has never seen: LLM over text, LLM over page images (replacing the OCR
dependency), and a much more tolerant regex fallback.

**4. Audit.** Paused feature work to answer five questions honestly: where the
LLM makes business decisions, whether rules live in config or code, whether
fields carry confidence and provenance, whether the trace is reconstructable,
and whether a low-confidence extraction can reach auto-approve.
[AUDIT.md](AUDIT.md), then [REFACTOR_STRATEGY.md](REFACTOR_STRATEGY.md) — which
found **a third defect the audit missed**, a concurrency race in the PO ledger.

**5. Phase 0 — back to green.** Git baseline, `main.py` reconciled with the new
extraction API, and `tests/test_samples.py` as a real pytest suite.

**6. Phase 1 — five business-rule fixes**, each its own verified commit:
inferred-PO safety, currency mismatch, invoice arithmetic, zero/negative
amounts, vendor normalisation. 112 tests.

**7. Provider split.** Text PDFs moved to **Groq**; **Gemini** kept for scans
only. An economics decision, not an architectural one — Gemini's free tier is 20
requests per *day* and it is the only route that can read a picture, so it is no
longer spent on documents that already have a text layer. `matching.py`,
`storage.py` and the decision engine were untouched by the swap, which is the
whole point of the architecture.

**8. Deterministic audit trail.** `rules.decide()` now emits a structured record
as it evaluates: the values compared, the PO and where its record came from, the
variance, the tolerance, every rule that passed or failed, and the reason. No
model is involved in any of it.

**9. Human-in-the-loop review.** Accept / reject on anything held, recorded
*beside* the automated decision rather than on top of it.

**10. API security.** The API had none: every endpoint was reachable by anyone
who knew the URL and the reviewer's identity was whatever the client typed.
Now OAuth 2.0 bearer tokens, scopes, per-user rate limits, real input validation
and a daily extraction budget.

---

## Quick start (Windows)

```powershell
.\start.ps1
```

Creates a venv, installs dependencies, generates the sample invoices if missing,
builds the UI on first run, and opens <http://127.0.0.1:8000>.

The UI build needs Node (18+). If npm is not installed the app still starts and
serves the original vanilla frontend instead — nothing is blocked.

### Manual start

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe sample_invoices\generate_invoices.py   # first run only
cd frontend-next; npm install; npm run build; cd ..            # first run only
.\venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
```

### Working on the UI

```powershell
cd frontend-next
npm run dev        # :3000, proxies /api to the backend on :8000
npm run build      # regenerates the static export FastAPI serves
```

### Signing in

The API requires authentication, so the app opens on a sign-in screen. Demo
accounts ship in `data/users.json`:

| Username | Password | Can |
|---|---|---|
| `viewer` | `demo-viewer` | read runs, audit trails, reference data |
| `analyst` | `demo-analyst` | the above + process invoices |
| `reviewer` | `demo-reviewer` | the above + accept / reject held invoices |
| `admin` | `demo-admin` | the above + override any run's status |

These are demo credentials and are flagged as such in the file. A production
start refuses to boot while they exist — see [Running in production](#running-in-production).

> Set `AUTH_SECRET` in `.env` before a demo. Without it a fresh signing key is
> generated per process, so every server restart silently invalidates the token
> in your browser and you have to sign in again mid-recording.

### Running the tests

```powershell
.\venv\Scripts\python.exe -m pytest tests\ -q
```

**359 tests across 15 files.** They mock both providers, so they need no API
key, no network and no quota. With keys present, `tests/test_samples.py` additionally exercises the
real Groq and Gemini routes end to end — the fixture prints which mode ran,
because a green suite means a different thing in each.

> One test needs a note: `tests/test_extraction_routing.py` reads the real
> daily-quota counter in `data/app.db`, so if the local vision budget is spent,
> four of its cases fail even though the providers are mocked. Running against a
> clean database gives the true result.

### Resetting the demo

The sample invoices are deliberately history-dependent — the split-PO story only
works as 02 → 03 → 03b, and 06 is only a duplicate because 01 ran first. Every
run is recorded, so a second pass turns the happy path into a duplicate of
itself and leaves PO-1001 with no budget. The verdicts stay correct; the samples
just stop demonstrating what they were written to demonstrate.

Two ways back to a clean slate:

- **In the app** — sign in as `admin` and use **Reset demo data** on Overview.
- **From a terminal** — `.\reset-demo.ps1`, or `.\reset-demo.ps1 -Replay` to
  clear and then drive all seven samples through the API in order.

Both clear run history only. Purchase orders, vendors and users are seed data in
`data/*.json` and are reloaded on startup, so nothing is lost that re-running an
invoice cannot rebuild.

---

## Using it

- **Run tab** — drop in a PDF, or click a bundled sample. Each stage lights up
  live as it executes; the decision panel then shows the verdict, the PO balance
  bar, the reasoning trail, every extracted field, and a **Decision details**
  panel with the full audit trail. If the invoice was held for review and your
  account has permission, **Accept** and **Reject** appear beneath the evidence.
- **Dashboard tab** — every run, filterable by status, click-through to the full
  stage log and audit trail, plus PO consumption across all POs. A `human` chip
  marks runs a person ruled on.
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
| `05_scanned_no_text.pdf` | Image-only PDF, no text layer | **NEEDS_REVIEW** † |
| `06_duplicate_of_01.pdf` | Resubmission of `INV-2201` | **REJECTED** |

**Order matters.** Several cases are history-dependent by design:

- Run `02` → `03` → `03b` to see split-PO balance tracking. Each APPROVED run
  consumes part of `PO-1002`; by `03b` there is nothing left.
- Run `01` before `06` or the duplicate has nothing to collide with.

Run `03b` alone against a fresh database and it is **APPROVED** — same bytes,
opposite verdict, because $2,500 against an untouched $5,000 PO is an ordinary
partial invoice. The decision depends on the PO's history, not the file alone.

† **Sample 05's verdict is route-dependent, deliberately.** With no Gemini key
there is nothing to read, so the process refuses to guess → NEEDS_REVIEW. With
one, the vision route reads `INV-9004` / `PO-1005` / `$15,400.00` off the page
image and it approves. `manifest.json` carries both expectations and the UI
resolves against key presence. Same bytes, different verdict, because a
different capability was available — a better story for a non-technical audience
than the split-PO case.

Sample `04` also flags an arithmetic inconsistency: the document states
`Subtotal $8,200 + Tax $0.00 = Total $8,150`, which does not add up. Both
extraction routes read it identically, so the check is correct — the fixture
itself is inconsistent. The verdict is unaffected.

---

## How it works

### The core idea

**The AI reads, the rules decide.** Extraction is genuinely hard for code —
every vendor formats invoices differently — and easy for an LLM. But the
*decision* must be identical every time and defensible to an auditor, so no
model touches a dollar comparison. Everything downstream of extraction is
deterministic Python, and no prompt contains the words approve, reject or
tolerance.

### Pipeline

```
Sign in → authenticate → authorize → rate limit → validate the file
   ↓
INGEST → EXTRACT_TEXT → EXTRACT_FIELDS → VALIDATE → VENDOR_CHECK
       → PO_MATCH → DUPLICATE_CHECK → TOLERANCE_CHECK → DECISION
   ↓
audit trail → (if held) human accept / reject
```

Stages do **not** short-circuit. A missing invoice number at stage 4 does not
stop stages 5–8 — findings accumulate and only the final stage judges, so a
reviewer sees the whole picture rather than the first thing that went wrong.

### Is it even an invoice?

Every check after extraction assumes the input **is** an invoice and asks
whether it may be paid. One check asks the prior question, because without it a
CV or a contract lands in the review queue described as a defective invoice —
"missing required fields", which is true and useless.

There is no keyword list. Searching the text for the word *invoice* is both too
weak and too strong: it misses invoices in other languages, and it fires on any
document that merely **discusses** invoicing. The extractor is already a
classifier — when a model reads a page and finds no vendor, no invoice number,
no amount, no date, no PO reference and not one line item, the document does not
contain an invoice. Any single one of those signals is enough to accept it as
one, because invoice formats vary enormously.

This **rejects** rather than holds: a hold means "a human must decide whether to
pay this", and there is nothing to decide about a CV. It fires only when a model
route actually read the document — if extraction fell back to regex or failed,
an empty result is evidence about the *extractor*, not the document, so a
scanned invoice is still held for a person.

### Decision hierarchy

- **REJECTED** — things the process must not override: duplicates, vendors on
  file but not approved, documents that are not invoices.
- **NEEDS_REVIEW** — recoverable: missing fields, unreadable scan, amount over
  tolerance, no PO match, currency mismatch, bad arithmetic, an invalid total,
  an inferred PO, or text that reads as an instruction to the extractor.
- **APPROVED** — everything passed.

Reject wins over review when both fire.

### Tolerance is deliberately one-sided

```python
within = diff <= tol      # not abs(diff) <= tol
```

Billing **over** the remaining PO balance is a problem — the vendor wants money
nobody authorised. Billing **under** it is a normal partial invoice. Tolerance
is the larger of 1% of the remaining balance or $50
(`config.PO_TOLERANCE_PERCENT` / `PO_TOLERANCE_DOLLARS`).

### Split-PO tracking

There is no "consumed" column. The remaining balance is derived on every run by
summing the totals of previously **APPROVED** runs matched to that PO:

```
remaining_before = PO amount − Σ(prior approved invoices)
```

Only approved runs consume budget, so a flagged invoice sitting in review
doesn't block the queue behind it, and the run history *is* the ledger — no
counter can drift out of sync. It also makes idempotency and reversal
structural rather than defended: there is nothing to deduct twice, and moving a
run out of APPROVED refunds it in the same instant.

### Extraction routes

The route is chosen by what the document **is** — whether a usable text layer
can be read out of it — never by its extension. Every route returns the same
`ExtractedInvoice`, so matching and rules never know which ran:

| Route | When | Provider | Needs key |
|---|---|---|---|
| `groq (text)` | PDF has an embedded text layer | Groq | `GROQ_API_KEY` |
| `gemini (vision)` | Scanned / image-only PDF — page images sent to the model | Google Gemini | `GEMINI_API_KEY` |
| `regex` | No Groq key, or the Groq call failed | — | No |
| `none` | Nothing readable — returns empty fields rather than guessing | — | — |

Configure in `.env` at the project root:

```
GROQ_API_KEY=...        # https://console.groq.com/keys
GEMINI_API_KEY=...      # https://aistudio.google.com/apikey
```

`.env` is gitignored and neither key is ever sent to the browser.

**Why the split.** Gemini's free tier allows 20 requests per *day*, and it is
the only route that can read a picture. Spending it on text PDFs — which already
have a working regex fallback — traded the one irreplaceable route for the one
with an alternative. Groq covers text and is far more generous.

**On failure**, each route takes its existing safe path and never fabricates:
Groq falls back to regex (deliberately *not* to Gemini, which would spend the
scarce budget), and Gemini falling over leaves route `none` → empty fields →
a human. Neither failure can produce an APPROVED.

Models are **pinned**, not aliased (`openai/gpt-oss-120b`, `gemini-3.7-flash`),
both overridable by environment variable. An alias changes the model under a
running system, and an AP process must be able to say which model read an
invoice approved months ago.

### The audit trail

Every run stores a structured record of how its decision was reached:

```
Automated Decision: NEEDS_REVIEW
Reason:             Invoice total exceeds PO remaining amount.
PO:                 PO-1002   (explicit match, open)
Source:             purchase_orders.json, row 2
Invoice total       $2,500.00
PO remaining        $0.00
Variance            $2,500.00
Tolerance used      $50.00
Rules  ✓ Security screen   ✓ Document readable  ✓ Required fields present
       ✓ Invoice amount    ✓ Invoice arithmetic ✓ Duplicate check
       ✓ Vendor approved   ✓ PO matched         ✓ Currency match
       ✗ PO remaining check
```

It is emitted **by the same evaluation that produces the decision** — each check
is recorded next to the branch that sets the verdict — not by a second pass that
re-derives it. A trail assembled separately can disagree with the decision it
claims to explain; this one cannot. No LLM writes any of it.

PO records carry `source_file` and `source_row` so a balance can be traced back
to the procurement row it came from. Nothing is invented: a record with no
derivable position stores `NULL` and the trail says the row is unknown.

### Human review

When the process holds an invoice, a reviewer opens the audit trail and rules on
it. The automated decision is never overwritten:

```
automated_decision   NEEDS_REVIEW      ← what the rules concluded, permanently
human_decision       ACCEPTED
final_decision       HUMAN_APPROVED
```

`status` does move, because that is the column the ledger sums — an accepted
invoice has to consume its PO budget. Reviewer identity comes from the
authenticated token and nothing else; a `reviewer` field in the request body is
ignored. One ruling per run: reversing one is an admin action through the status
endpoint, which leaves its own trail.

### API security

The frontend is treated as an untrusted client. CORS is configured but is not a
security boundary — a script ignores it entirely.

- **Authentication** — OAuth 2.0 resource-server pattern. Every protected call
  carries `Authorization: Bearer <JWT>`, validated for signature, expiry and
  issuer. Tokens are minted locally via the password grant; swapping in a hosted
  IdP means verifying against its JWKS and changing nothing else.
- **Authorization** — scopes named for actions: `invoice:read`,
  `invoice:process`, `invoice:review`, `invoice:admin`. Reviewing is separate
  from processing, because approving payment is a different authority from
  feeding a PDF to an extractor.
- **Rate limiting** — per user and per IP, configurable, default 20 processing
  requests/minute/user. Authentication runs first, so an anonymous flood cannot
  burn a real user's budget.
- **Daily extraction budget** — a slower circuit breaker in front of both
  providers. Twenty polite requests an hour apart never trip a per-minute limit
  and would still exhaust Gemini for the day. When a budget is spent the
  provider is not called at all and extraction takes its existing safe fallback.
  The counter lives in SQLite so it survives a restart.
- **Input validation** — uploads capped and read in chunks, PDFs validated by
  magic bytes rather than extension or client-declared type, filenames reduced
  to a safe basename.
- **Errors** — proper status codes (401/403/404/409/413/415/429/500) with no
  stack traces, provider messages or configuration names in the response.

### Stack

- **Backend** — FastAPI. `POST /api/runs/stream` streams stages over SSE as they
  execute; other endpoints serve run history, audit trails and reference data.
- **Extraction** — `pdfplumber` for text, `pypdfium2` for page rasterisation
  (a self-contained wheel: no poppler or tesseract to install).
- **Storage** — SQLite at `data/app.db`, seeded from `data/*.json` on startup.
- **Auth** — `pyjwt`; PBKDF2-HMAC-SHA256 password hashing from the standard library.
- **Frontend** — Next.js 15 (App Router), React 19, Tailwind v4, TypeScript.
  Reads the SSE stream with `fetch()` and a `ReadableStream` reader.

  The production build is a **static export**: `npm run build` emits plain
  HTML/JS into `frontend-next/out/`, which FastAPI serves at `/`. No Node
  process runs at serve time, the UI stays same-origin with the API (so no
  CORS and no base URL to get wrong), and the whole app is still one command on
  one port. `next dev` on :3000 proxies `/api` to :8000 for development.

  The original vanilla frontend is still in `frontend/` and is served
  automatically if the export has never been built, so a clone without npm
  still boots a working UI.

### API endpoints

```
GET  /api/health                 public liveness probe
POST /api/auth/token             OAuth 2.0 password grant → bearer token
GET  /api/auth/me                who you are and what your token permits
POST /api/runs/stream            multipart PDF → SSE stage stream  [invoice:process]
GET  /api/runs                   run history                       [invoice:read]
GET  /api/runs/{id}              one run, including its audit trail[invoice:read]
POST /api/runs/{id}/review       accept / reject a held invoice    [invoice:review]
POST /api/runs/{id}/status       override any run's status         [invoice:admin]
POST /api/admin/reset-demo       clear run history so samples replay[invoice:admin]
GET  /api/reference              POs + approved vendors            [invoice:read]
GET  /api/sample-invoices        the bundled scenarios             [invoice:read]
GET  /api/sample-invoices/{name} one sample PDF                    [invoice:read]
```

---

## Running in production

Set `APP_ENV=production` and the app refuses to start on any of the following,
reporting all of them at once:

- `AUTH_SECRET` unset — there is no ephemeral fallback in production, because
  it invalidates every session on restart and differs between workers.
- Demo credentials present in the user store. The flag lives on the record, not
  the file path, so copying `data/users.json` elsewhere does not launder it.
  Point `AUTH_USERS_FILE` at a real store, or replace the token issuer with your
  identity provider.
- An empty or unreadable user store, or `CORS_ORIGINS` containing `*`.

Everything convenient about the demo — published passwords, zero-config signing
keys — is allowed in development and fatal in production, deliberately. Each is
the kind of mistake that leaves the app working perfectly and quietly insecure.

Environment variables worth knowing, all optional in development:

```
APP_ENV=development           # production | prod | live triggers the checks above
AUTH_SECRET=                  # required in production
AUTH_USERS_FILE=              # override the demo user store
AUTH_TOKEN_TTL_MINUTES=480
CORS_ORIGINS=                 # empty means same-origin only
RATE_LIMIT_PROCESS_PER_MINUTE=20
DAILY_QUOTA_VISION=20         # matches Gemini's free tier
DAILY_QUOTA_TEXT=500
```

---

## Known problems

**All three defects the audit and architect review found are fixed** — inferred
PO matches no longer auto-approve, currency mismatches are caught, and the PO
ledger race is closed with `BEGIN IMMEDIATE` + WAL (verified under real threads:
8 concurrent $2,000 invoices against a $10,000 PO give exactly 5 approved, 3
held, $0.00 remaining).

What is still true, by design rather than accident, and queued for later phases:

- Extracted fields are **bare values** — no confidence, no pointer to where in
  the document they came from. A total read off the page is indistinguishable
  from one the code synthesised as `subtotal + tax`. This is Phase 2 and the
  most valuable thing left to build.
- **Business rules are constants in `config.py`, not versioned policy.** There
  is no `rules.yaml`, no policy version, and no way to say which policy approved
  a given invoice.
- Reference data is **re-seeded from JSON on every startup**, so editing
  `purchase_orders.json` silently changes what historical runs refer to.
- The schema stores **one `po_number` per run**, so a consolidated invoice
  spanning several POs would over-consume each of them.
- `extraction._first()` strips a **leading minus sign** off a captured amount,
  so `Total Due: -500.00` extracts as positive 500. Accounting parentheses are
  unaffected. The amount rule cannot catch a sign the extractor discarded.
- Rate-limit counters are **per process** — several uvicorn workers each keep
  their own, multiplying the effective limit.

**Operationally:** Gemini's vision endpoint has been returning intermittent
`503`s. The fallback is safe — the invoice is held, nothing is fabricated — but
the scanned-sample demo is not reliably reproducible, and `manifest.json`
resolves that sample's badge on key *presence* rather than provider
availability, so the badge can briefly contradict the run beside it.

---

## What's next

| Phase | Work | State |
|---|---|---|
| **0** | Green build, pytest suite | ✅ done |
| **1** | Inferred-PO safety, currency, arithmetic, invalid amounts, vendor matching | ✅ done |
| **2** | `Tracked[T]` provenance wrapper, per-route confidence, the **confidence gate** | ⬜ |
| **3** | `rules.yaml` — pull every threshold out of Python, stamp the version on each run | ◨ thresholds centralised; YAML + loader to do |
| **4** | Transaction boundaries and the `run_allocations` ledger table | ◨ transactions done; allocations to do |
| **5** | `DecisionTrace` + reference snapshot; stop re-seeding on startup | ◨ audit trail done; snapshot to do |
| **6** | Line-item decomposition, multi-PO consolidation, FX provider | ⬜ |
| **7** | UI: confidence badges, evidence snippets, allocation view | ⬜ |

**The most valuable thing left** is Phase 2's confidence gate — it closes the
low-confidence auto-approve problem as a *class* rather than case by case.
Nothing in Phases 2–7 changes a verdict on any of the seven samples, though, so
none of it is what is blocking the case study.

**One sequencing trap worth knowing:** multi-PO consolidation is a *ledger*
feature, not a matching feature. The schema stores one `po_number` per run and
consumption sums run totals, so a consolidated invoice would over-consume every
PO it touched. It needs the allocations table, which needs transaction
boundaries first. Phase 4 before Phase 6, always.

**For the case study itself:** deploy it somewhere shareable and record the
5-minute demo video. Neither needs another line of pipeline code.

Full plan with rationale, code patterns and exit criteria:
[REFACTOR_STRATEGY.md](REFACTOR_STRATEGY.md) · findings behind it:
[AUDIT.md](AUDIT.md#refactor-plan).

---

## Repository layout

```
backend/
  main.py         FastAPI app, the 9-stage pipeline, SSE streaming, endpoints
  extraction.py   PDF → text/images → structured fields (Groq / Gemini / regex)
  matching.py     PO lookup, split-PO balance maths, tolerance, currency
  rules.py        Validation, vendor, duplicates, decision + audit trail
  storage.py      SQLite: seed data, run history, ledger, human review
  auth.py         OAuth 2.0 bearer tokens, scopes, production config checks
  ratelimit.py    Per-user / per-IP sliding-window limits
  quota.py        Daily per-provider extraction budget (circuit breaker)
  schemas.py      Shared dataclasses
  config.py       Operational settings, .env loading, environment switch
frontend-next/    The UI. Next.js 15 + React 19 + Tailwind v4, TypeScript
  app/            Root layout, the single client-rendered page, design tokens
  components/
    layout/       App shell — sidebar, page chrome, responsive drawer
    pages/        Overview, Process invoice, Invoices, Purchase orders
    invoice/      Run detail: stages, three-way match, audit trail, review
    ui/           Primitives — button, badge, panel, modal, toast, icons
  lib/            API client, auth context, metrics, formatting, types
frontend/         The original vanilla UI, kept as a no-build fallback
data/             Seed POs + vendors + demo users (tracked); app.db (not tracked)
sample_invoices/  7 PDFs, the generator, and manifest.json of scenarios
scripts/          replay_samples.py — drives the samples in manifest order
tests/            15 files, 359 tests, both providers mocked
reset-demo.ps1    Clears run history so the samples can be replayed
AUDIT.md              Architecture self-audit — what is wrong and why
REFACTOR_STRATEGY.md  Architect review — fix logic, schemas, sequencing
PROCESS_MAP.md        The on-paper design done before building
CLAUDE.md             Session handoff notes
```
