# Invoice Processing — PDF to Decision

An automated AP process: sign in, upload a vendor invoice PDF, watch it move
through extraction, validation, PO matching and rule checks live, and get a
decision — **APPROVED** / **NEEDS_REVIEW** / **REJECTED** — with a structured
audit trail behind it, a human accept/reject path for anything held, and a
dashboard of every run.

Built for the Zamp AI Solutions Associate case study, **PS-1 (Finance / AP)**.

---

## Where the project stands right now

**The app runs. All 10 sample invoices produce their expected verdicts, and the
suite is green.**

| | |
|---|---|
| Pipeline | Working, 9 stages, streamed live to the browser |
| Sample invoices | 10 / 10 matching the manifest, driven through the real pipeline |
| UI | **Next.js 15 + React 19 + Tailwind v4**, four sections, light-first enterprise design with an explicit dark-mode toggle |
| Extraction | **Groq** for text PDFs, **Gemini Vision** for scans |
| Automated tests | **528 passing** deterministically, 20 files, no live API calls |
| Audit trail | Structured, deterministic, emitted by the rule engine itself |
| Human review | Accept / reject on NEEDS_REVIEW, recorded beside the automated decision |
| Review collaboration | Claimable review queue (database-enforced, leased), full activity history per invoice |
| API security | OAuth 2.0 bearer tokens, scopes, rate limits, input validation |
| Database | PostgreSQL via `DATABASE_URL` — no SQLite fallback anywhere |
| Document storage | Uploaded PDFs persist after processing — metadata in Postgres, bytes behind a swappable local/S3 store |
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

**11. Invoices covering several POs.** The ledger charged a run's whole total to
one `po_number`, so a consolidated invoice over-consumed the first PO it named by
the value of the others and never touched them. Fixed in the order the design
required: a `run_allocations` table first, behaviour-neutral and verified against
a migrated legacy database, then multi-PO matching on top of it. The split itself
is calculated rather than read, so such an invoice is always held for a person —
see [Invoices covering several POs](#invoices-covering-several-pos).

**12. Currency mismatch, revisited.** A mismatch used to hold unconditionally,
on the grounds that a rate fetched at run time is not reproducible by an
auditor — a decision this project once explicitly refused to relitigate. That
objection is about *when* the rate is fetched, not conversion itself, so it
does not hold against a table that is pinned and versioned. Added: FX
conversion at a pinned rate (approve when it resolves within tolerance), and a
hard reject when the invoice states the PO's own raw number under a different
currency — no correct conversion produces that, so it isn't an ordinary
discrepancy for a human to puzzle over. See
[Currency mismatch and FX conversion](#currency-mismatch-and-fx-conversion).

**13. Confidence, provenance and the confidence gate.** The most valuable
thing left in the phase table — explicitly not started until asked for
directly. Every extracted field now carries a confidence score, a source
location and a quoted piece of evidence (self-reported by the model, or a
deterministic heuristic for regex), and three fields central to the decision
can hold a run for review if the extractor itself is unsure of them — never
reject, only ever hold. The human-review screen gained a **reviewer brief**:
why a run was flagged, which field(s), the evidence behind each, and one
deterministic suggested next step, ahead of Accept/Reject. See
[Confidence, provenance and the confidence gate](#confidence-provenance-and-the-confidence-gate).

**14. Frontend redesign, and a dark-mode toggle.** The UI was rebuilt as a
light-first enterprise finance interface — warm-neutral surfaces, one accent,
three semantic colours, radii capped at 10px, IBM Plex Sans/Mono with tabular
numerals on every ledger figure. A later request added an explicit dark-mode
toggle on top, deliberately not tied to the OS's `prefers-color-scheme` — see
[Visual design and dark mode](#visual-design-and-dark-mode). Getting there
included a debugging detour worth recording: a set of "the UI looks like a
dark hacker tool, redesign everything" screenshots turned out to be a browser
extension force-darkening the page client-side, not the app's own CSS — caught
by fetching the served stylesheet directly and by rendering the app with a
clean, extension-free headless browser.

---

## Quick start (Windows)

**Requires PostgreSQL first.** The app has no SQLite fallback — set
`DATABASE_URL` in `.env` (copy `.env.example`) and point it at a reachable
instance:

```powershell
docker-compose up -d          # local Postgres matching .env.example, if you have Docker
```

or install PostgreSQL directly (`winget install PostgreSQL.PostgreSQL.16` on
Windows) and create a dedicated database/role rather than using the
`postgres` superuser for the app.

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

**447 tests across 18 files.** They mock both providers, so they need no API
key, no network and no quota. With keys present, `tests/test_samples.py` additionally exercises the
real Groq and Gemini routes end to end — the fixture prints which mode ran,
because a green suite means a different thing in each. Requires PostgreSQL
reachable via `DATABASE_URL`; every other test gets an isolated schema per
run, created and dropped automatically.

> Two tests need a note: `tests/test_extraction_routing.py` and
> `tests/test_reset_demo.py` run directly against the real application schema
> rather than an isolated one (no `db` fixture in either file — a pre-existing
> characteristic, not something the Postgres migration introduced). If the
> local vision budget is spent, some `test_extraction_routing.py` cases fail
> even though the providers are mocked; running right after `reset-demo` gives
> the true result.

### Resetting the demo

The sample invoices are deliberately history-dependent — the split-PO story only
works as 02 → 03 → 03b, and 06 is only a duplicate because 01 ran first. Every
run is recorded, so a second pass turns the happy path into a duplicate of
itself and leaves PO-1001 with no budget. The verdicts stay correct; the samples
just stop demonstrating what they were written to demonstrate.

Two ways back to a clean slate:

- **In the app** — sign in as `admin` and use **Reset demo data** on Overview.
- **From a terminal** — `.\reset-demo.ps1`, or `.\reset-demo.ps1 -Replay` to
  clear and then drive all ten samples through the API in order.

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
| `07_multi_po_wayne.pdf` | One invoice covering two POs | **NEEDS_REVIEW** ‡ |
| `08_fx_match_oscorp.pdf` | Different currency, converts to an exact match | **APPROVED** § |
| `09_currency_number_collision_lexcorp.pdf` | Same raw number, wrong currency | **REJECTED** § |

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

‡ **Sample 07 is the one that is held even though nothing is wrong with it.**
It bills $6,500 against `PO-1006` ($4,000) and `PO-1007` ($2,500), which together
authorise exactly that — every dollar is approved, the vendor is on the list, the
arithmetic is right. What the document never says is *which PO each line belongs
to*. The process works out the obvious division, shows it, and still refuses to
act on it, because a division it calculated is a proposal and not an
authorisation. Accept it as a reviewer and both POs are charged their own share.
See [Invoices covering several POs](#invoices-covering-several-pos).

Sample `04` also flags an arithmetic inconsistency: the document states
`Subtotal $8,200 + Tax $0.00 = Total $8,150`, which does not add up. Both
extraction routes read it identically, so the check is correct — the fixture
itself is inconsistent. The verdict is unaffected.

§ **Samples 08 and 09 are the same shape, opposite outcome, deliberately paired.**
Both bill in EUR against a USD PO. Sample 08's `€2,000.00` converts to exactly
`$2,160.00` at the pinned rate — a genuinely different currency landing on a
genuinely matching value, so it approves, with the rate and its table version
named in the audit trail. Sample 09 states `€5,000.00` against a `$5,000.00`
PO — the identical digits, not a converted equivalent, which no correct
conversion produces — so it is rejected outright rather than held: at the
pinned rate it is actually `$5,400.00`, meaning paying the face value would
silently underpay by $400. See
[Currency mismatch and FX conversion](#currency-mismatch-and-fx-conversion).

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
  file but not approved, documents that are not invoices, and an invoice that
  states the PO's own number under a different currency.
- **NEEDS_REVIEW** — recoverable: missing fields, low extraction confidence on
  a field central to the decision, unreadable scan, amount over tolerance, no
  PO match, a currency mismatch with no pinned rate (or one that still doesn't
  fit after conversion), bad arithmetic, an invalid total, an inferred PO, an
  invoice covering several POs with no stated split, or text that reads as an
  instruction to the extractor.
- **APPROVED** — everything passed. Includes a currency mismatch that a
  pinned, versioned exchange rate resolves within tolerance.

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

### Invoices covering several POs

A consolidated invoice references more than one purchase order. Two things have
to be right for that to work, and they are separate problems.

**Representing it.** The ledger records how much of each run was charged to
*which* PO, in a `run_allocations` table, and consumption sums those allocations.
It used to sum run totals against a single `po_number` column, which meant an
invoice covering two POs was charged entirely to the first — over-consuming it by
the value of the second while the second stayed untouched. An ordinary invoice is
now simply a run with one allocation, so there is no special case.

This is **not** the stored counter that was rejected earlier. An allocation is an
immutable fact about a run; whether it *counts* is still derived at read time by
joining to `status='APPROVED'`. Nothing is deducted, so nothing can be deducted
twice, and reversing a run refunds every PO it touched in the same instant.

**Dividing it.** Nothing on the document says how much belongs to each PO — line
items carry no PO references — so any division is computed rather than read. The
process computes one (fill each PO to its remaining balance, in the order the
invoice named them) and then **refuses to act on it**: a multi-PO invoice is
always `NEEDS_REVIEW`, even when the combined balance covers it comfortably.

That is the same objection that already holds an *inferred* single-PO match,
applied to the division instead of the binding. Approving would commit money
against purchase orders in amounts no document and no person ever specified. The
proposal is stored and shown, so the reviewer confirms figures rather than
working them out, and the audit trail records `allocation_basis: calculated` to
distinguish a computed split from a single-PO charge.

If the invoice exceeds every balance combined, the excess lands on the last PO
rather than vanishing — the allocations must sum to the invoice total, or the
ledger is describing money nobody billed — and that PO is flagged as over.

### Currency mismatch and FX conversion

An invoice in one currency matched against a PO in another used to hold for
review unconditionally — "no conversion, no rate lookup, no third party,
because a verdict that depends on an exchange rate fetched at run time is not
reproducible by an auditor." That objection is about *when* the rate is
fetched, not about conversion itself, so it does not apply to a table that is
**pinned** and **versioned** (`config.FX_RATES` / `FX_RATES_VERSION`, the same
pinning argument already used for the extraction models). Three outcomes now,
not one:

1. **The pinned rate resolves the conversion within tolerance.** APPROVED.
   `€2,000.00` converting to exactly `$2,160.00` against a `$2,160.00` PO is a
   genuinely different currency and genuinely the same value — the audit trail
   names the rate and the table version that priced it, so the decision stays
   reproducible.
2. **The invoice states the *same raw number* as the PO, in a different
   currency** — `€1,500.00` against a `$1,500.00` PO. No correct conversion
   produces identical digits in a different currency, so this is not an
   ordinary discrepancy for a human to reconcile against the numbers in front
   of them; it reads as a currency-code error or a copied figure, and paying
   the face value would silently over- or under-pay by the full FX
   difference. **REJECTED outright**, not held — the audit trail names the
   correctly-converted figure so the reviewer sees the gap immediately.
3. **No pinned rate exists for the pair, or the converted amount still doesn't
   fit.** Held for a human, exactly as every mismatch was before this
   feature existed. Nothing is guessed.

The ledger always consumes the **converted** amount, not the raw
foreign-currency digits — `run_allocations` and the PO balance are in the PO's
currency, so a `€2,000.00` invoice against a USD PO consumes exactly
`$2,160.00`, and reversing it refunds exactly that. The raw invoice total is
still shown, in its own currency, everywhere the document itself is quoted.

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

### Confidence, provenance and the confidence gate

Every extracted field can carry provenance — a confidence score, where it came
from, and a quoted snippet of the document backing it:

```
Invoice total: $50,000
Confidence:    96%
Source:        page 1
Evidence:      "Total Due: $50,000.00"
Read by:       Groq · text layer
```

**Where the score comes from.** The LLM routes (Groq/Gemini) self-report a
confidence (0–1) and a short verbatim quote for each field, in the same JSON
call that reads the value — no second pass. Regex has no self-assessment, so it
gets a deterministic heuristic instead: an explicitly labelled match
(`"Invoice #:"`) scores high, a positional guess (`_guess_vendor`, a known weak
heuristic) scores lower, and a value computed rather than printed (a total
synthesised as `subtotal + tax` because none was stated) scores lower still —
deliberately below the gate threshold.

Stated honestly rather than glossed over: **model self-reported confidence
skews high and is not independently calibrated.** It is still a genuine
signal — a model unsure about a field it read is meaningfully different from
one that read it cleanly — just not a guarantee, and the gate's own wording
says so.

**Evidence is checked, not trusted blindly.** A model can hallucinate a quote
as easily as a value, so every quoted snippet is verified against the actual
extracted text; an unverified quote is shown labelled as such rather than
presented as confirmed.

**The gate.** Three fields — vendor name, invoice number, total, the same ones
already required for approval — can hold a run for review if the extractor
itself scored them below 65% confidence. Deliberately narrow: it only fires
when a field **is present but uncertain**, a different failure class from a
field that's simply missing, and it only ever **holds, never rejects** — the
same rule every other extraction-uncertainty signal in this pipeline follows
(an unreadable scan, the injection guard). Low confidence about a *reading* is
not evidence the invoice itself is wrong.

### Human review, briefed

When a run is held or rejected, a **reviewer brief** sits above the Accept /
Reject buttons — everything needed to decide, assembled from data the rule
engine already computed, nothing generated for the occasion:

- **Why it was flagged** — the same deterministic reason in the audit trail.
- **Which field(s)** are implicated — every failing check maps to the field(s)
  it concerns (a static lookup by rule name, e.g. an arithmetic mismatch names
  `subtotal`, `tax` and `total`), de-duplicated across every failing check, not
  just the first.
- **The evidence** behind each one — confidence, source, and the quoted
  snippet, straight from provenance.
- **A suggested next step** — one deterministic sentence, looked up from the
  same rule that produced the reason ("confirm the vendor's approval status",
  "request a corrected invoice", "convert manually and confirm the correct
  amount") — never generated, never a guess.

Purchase-order context (the three-way match, the balance bar) and who-ruled-
when were already surfaced elsewhere in the run view and are not duplicated
here.

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

### Document storage

The uploaded PDF survives the run that processed it. The database only ever
holds **metadata** — original filename, MIME type, size, a SHA-256 hash, who
uploaded it, when, and how (`MANUAL_UPLOAD` today; `EMAIL` is recognised for
when ingestion exists) — plus an opaque storage key. The bytes live behind a
small `DocumentStore` interface (`backend/documents.py`), so the invoice
pipeline never knows or cares whether a document sits on local disk or in an
S3 bucket:

- **`local`** (the default) — files under `DOCUMENT_STORAGE_DIR`
  (`./data/documents`). Needs nothing installed or configured.
- **`s3`** — an S3-compatible bucket, for a deployment with no shared local
  disk between instances. `boto3` is imported lazily, only when this backend
  is selected, so a local-only install never needs it.

The storage key is **never** the original filename — it is a server-generated
UUID, checked against a fixed shape before it is ever joined onto a path or an
object key, so a corrupted or hand-edited database row can never be used to
read or write outside where documents actually live. The original filename is
kept only as display metadata, and it is already the sanitised name (no
directory component, no control characters) computed at upload time, not the
raw client-supplied one.

`GET /api/runs/{id}/document` returns metadata only — never the storage
backend or key, which are nobody's business outside the process.
`GET /api/runs/{id}/document/download` returns the real bytes, requiring the
same `invoice:read` scope as reading the run itself; `?inline=1` asks for
`Content-Disposition: inline` (for an embedded viewer) instead of the default
`attachment`. Persisting the document is never allowed to fail the run it
belongs to — a storage problem is logged and the run still completes with its
decision, the same fail-safe posture as the daily extraction-quota breaker.

`POST /api/admin/reset-demo` (and `.\reset-demo.ps1`) clears document rows and
their backing files along with the runs they belong to, so the samples stay
repeatable exactly as before.

### Review collaboration and activity history

Several employees can work the same review queue at once, and the database —
not the frontend — is the authority on what happened and who owns what.

**Two separate concepts, kept separate.** The audit trail (`audit_json` on
`runs`) explains why the *deterministic rules* reached a verdict — written
once by `decide()`, never appended to. The new `invoice_activity` table
records what *people* (and the system, acting on their behalf) did about it
afterwards: claimed a review, released it, added a note, accepted or
rejected, viewed or downloaded the source document. It is append-only — a
later event never overwrites an earlier one, so the full history of who
touched a run stays readable.

**Claiming is a lease, not a lock a person has to remember to release.** One
employee at a time may claim a `NEEDS_REVIEW` run (`POST
/api/runs/{id}/review/claim`); a second claim attempt gets a `409` naming who
currently holds it. Enforced with `SELECT ... FOR UPDATE` on the run row —
the same tool `save_run_checked` already uses to serialise two invoices
racing one PO — so two employees racing this endpoint cannot both win, proven
under real threads against real Postgres, not a mock. Every claim carries a
lease (`REVIEW_CLAIM_LEASE_MINUTES`, default 15); a closed tab or a lost
connection does not block the invoice forever, because the *next* claim
attempt after the lease lapses simply finds it expired and takes over. There
is no background sweep job — staleness is resolved lazily, the same
philosophy PO balances already use.

A run leaving `NEEDS_REVIEW` for any reason — a human ruling, a cascade
re-evaluation freeing PO budget, an admin override — automatically releases
whatever claim was on it; there is nothing left to protect once review is no
longer needed. A review submitted while someone *else* holds the claim is
refused with the same `409` shape as claiming itself.

`GET /api/runs/{id}/activity` returns the chronological history plus who (if
anyone) currently holds the claim; `GET /api/runs/{id}` also carries
`current_claim` directly, so opening one specific invoice never needs a
second round trip to answer "is someone already looking at this?" Claiming,
releasing and adding a standalone comment (`POST /api/runs/{id}/comment`) all
require `invoice:review`, the same scope reviewing itself already requires;
reading activity requires only `invoice:read`, matching every other run-level
read. Reviewer identity is always the authenticated token's username, never a
request-body field — the claim endpoints do not even accept one.

**The decision itself is submitted atomically, not just the claim.** Two
concurrent submissions on one run — a double-clicked Accept, a retried
request, or a genuine Accept-vs-Reject race — resolve to exactly one landed
ruling; the loser gets the same `409` "already been reviewed" a repeat
submission always got, rather than both writes landing and the activity
history recording two conflicting outcomes for one invoice. Enforced with the
same `SELECT ... FOR UPDATE` row lock claiming already uses, now held for the
whole check-then-write sequence rather than across several separate
transactions.

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
  The counter lives in PostgreSQL so it survives a restart.
- **Input validation** — uploads capped and read in chunks, PDFs validated by
  magic bytes rather than extension or client-declared type, filenames reduced
  to a safe basename.
- **Errors** — proper status codes (401/403/404/409/413/415/429/500) with no
  stack traces, provider messages or configuration names in the response.

### Visual design and dark mode

The UI is a **light-first enterprise finance interface**: warm-neutral
white/off-white surfaces distinguished by elevation rather than borders, a
single interactive accent, and exactly three semantic colours — approved,
held, rejected — that mean something and never decorate. Radii are capped at
10px; larger corners read as consumer software, not a finance tool. Every
figure that must be scanned and compared (money, invoice/PO numbers, dates,
run IDs) is set in IBM Plex Mono with tabular numerals, so a dense screen of
numbers reads as a ledger rather than prose that happens to contain digits.

**Dark mode is an explicit toggle in the sidebar, not `prefers-color-scheme`.**
A finance product should look the same in a demo as it did in design review —
letting the OS decide the theme means a screen-share or a recording can look
different from what was designed, and a form this dense reads as a "hacker"
tool the instant it goes dark unasked. The whole app already reads every
colour through CSS custom properties, so the dark palette is a second set of
values for the same tokens (`:root[data-theme="dark"]`) — nothing downstream
needed a component-level dark variant. The choice is persisted, and applied
before the page paints so a returning dark-mode user never sees a flash of
the light theme.

### Stack

- **Backend** — FastAPI. `POST /api/runs/stream` streams stages over SSE as they
  execute; other endpoints serve run history, audit trails and reference data.
- **Extraction** — `pdfplumber` for text, `pypdfium2` for page rasterisation
  (a self-contained wheel: no poppler or tesseract to install).
- **Storage** — PostgreSQL, via `DATABASE_URL`; seeded from `data/*.json` on startup. `psycopg2`, no ORM.
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
GET  /api/runs/{id}/document     metadata for the source PDF       [invoice:read]
GET  /api/runs/{id}/document/download  the PDF itself (?inline=1)  [invoice:read]
POST /api/runs/{id}/review       accept / reject a held invoice    [invoice:review]
POST /api/runs/{id}/review/claim   claim exclusive review ownership[invoice:review]
POST /api/runs/{id}/review/release release a review claim          [invoice:review]
POST /api/runs/{id}/comment      add a note without deciding       [invoice:review]
GET  /api/runs/{id}/activity     who did what, and when, plus the current claim [invoice:read]
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

`DATABASE_URL` is required in **every** environment, not just production —
there is no SQLite fallback to fall back to. Point it at a managed Postgres
instance in production (RDS, Cloud SQL, a host's managed Postgres add-on,
etc.), never at a container that dies with the deploy.

Environment variables worth knowing:

```
DATABASE_URL=                 # required everywhere — postgresql://user:pass@host:port/db
APP_ENV=development           # production | prod | live triggers the checks above
AUTH_SECRET=                  # required in production
AUTH_USERS_FILE=              # override the demo user store
AUTH_TOKEN_TTL_MINUTES=480
CORS_ORIGINS=                 # empty means same-origin only
RATE_LIMIT_PROCESS_PER_MINUTE=20
DAILY_QUOTA_VISION=20         # matches Gemini's free tier
DAILY_QUOTA_TEXT=500
DOCUMENT_STORE_BACKEND=local  # local | s3 -- see "Document storage" below
DOCUMENT_STORAGE_DIR=./data/documents
DOCUMENT_S3_BUCKET=           # required if DOCUMENT_STORE_BACKEND=s3
REVIEW_CLAIM_LEASE_MINUTES=15 # see "Review collaboration and activity history"
```

---

## Known problems

**All three defects the audit and architect review found are fixed** — inferred
PO matches no longer auto-approve, currency mismatches are caught, and the PO
ledger race is closed with a `SELECT ... FOR UPDATE` row lock on the specific
purchase order being charged (verified under real threads: 8 concurrent $2,000
invoices against a $10,000 PO give exactly 5 approved, 3 held, $0.00
remaining).

What is still true, by design rather than accident, and queued for later phases:

- **Business rules are constants in `config.py`, not versioned policy.** There
  is no `rules.yaml`, no policy version, and no way to say which policy approved
  a given invoice.
- Reference data is **re-seeded from JSON on every startup**, so editing
  `purchase_orders.json` silently changes what historical runs refer to.
- A multi-PO invoice's **split is proposed, not read**, so it always needs a
  human. Deriving it from the document would need per-line-item PO references,
  which is line-item decomposition — Phase 6.
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
| **2** | `Tracked[T]` provenance wrapper, per-route confidence, the **confidence gate** | ✅ done |
| **3** | `rules.yaml` — pull every threshold out of Python, stamp the version on each run | ◨ thresholds centralised; YAML + loader to do |
| **4** | Transaction boundaries and the `run_allocations` ledger table | ✅ done |
| **5** | `DecisionTrace` + reference snapshot; stop re-seeding on startup | ◨ audit trail done; snapshot to do |
| **6** | Line-item decomposition, multi-PO consolidation, FX provider | ◨ multi-PO done; currency mismatch resolves against a pinned rate table; line items + a broader/live FX provider to do |
| **7** | UI: confidence badges, evidence snippets, allocation view | ◨ allocation view + confidence badges + reviewer brief done; nothing queued |

**The most valuable thing left** was Phase 2's confidence gate — done, closing
the low-confidence auto-approve problem as a *class* rather than case by case.
See [Confidence, provenance and the confidence gate](#confidence-provenance-and-the-confidence-gate).
Nothing in the phases still open changes a verdict on any of the ten samples, so
none of it is what is blocking the case study.

**The sequencing trap this already hit:** multi-PO consolidation was a *ledger*
feature, not a matching feature. Teaching the matcher to bind several POs while
the schema still stored one `po_number` per run would have over-consumed every PO
an invoice touched. The allocations table landed first, on its own and
behaviour-neutral, and multi-PO matching second. Phase 4 before Phase 6, as
planned.

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
  storage.py      PostgreSQL: seed data, run history, ledger, human review
  documents.py    Document storage abstraction: local disk or S3-compatible
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
  lib/            API client, auth context, theme (dark-mode toggle), metrics,
                  formatting, types
frontend/         The original vanilla UI, kept as a no-build fallback
data/             Seed POs + vendors + demo users (tracked); app.db is vestigial
                  (pre-Postgres SQLite file, unused by any code now);
                  documents/ holds uploaded PDFs (local backend, gitignored)
sample_invoices/  10 PDFs, the generator, and manifest.json of scenarios
scripts/          replay_samples.py, migrate_sqlite_to_postgres.py
docker-compose.yml  Local PostgreSQL matching .env.example's DATABASE_URL
scripts/          replay_samples.py — drives the samples in manifest order
tests/            20 files, 528 tests, both providers mocked
reset-demo.ps1    Clears run history so the samples can be replayed
AUDIT.md              Architecture self-audit — what is wrong and why
REFACTOR_STRATEGY.md  Architect review — fix logic, schemas, sequencing
PROCESS_MAP.md        The on-paper design done before building
CLAUDE.md             Session handoff notes
```
