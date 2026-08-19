# CLAUDE.md — project context for a new session

Read this first. It is the handoff for an in-progress build.

---

## 1. What this is

A working AP (accounts payable) automation process built for the **Zamp AI
Solutions Associate case study, PS-1 (Finance / AP)**.

Sign in → upload a vendor invoice PDF → it runs through a 9-stage pipeline live
in the browser → produces **APPROVED / NEEDS_REVIEW / REJECTED** with a
deterministic audit trail, a human accept/reject path for anything held, and a
dashboard of every run.

**Case study deliverables:**
1. A working automated process, live and runnable, with an intuitive UI —
   a **live run view** and a **dashboard**. The UI is explicitly part of the grade.
2. A **5-minute demo video** showing the happy path plus at least one edge case.
3. Later: a live interview demo, running the process in real time and defending
   every design decision.

Grading: does it actually run, is the judgment behind the design sound, and can
it be explained to a **non-technical buyer**. Edge cases were self-defined
(2–4 required; there are 4).

**Working directory:** `c:\Users\adity\OneDrive\Desktop\Invoice processing`
Windows 11. PowerShell is primary; a Bash tool is also available.

---

## 2. Current status

| | |
|---|---|
| Pipeline | ✅ Working, 9 stages, streamed live over SSE |
| Samples | ✅ 7/7 match the manifest, driven through the real pipeline |
| UI | ✅ Run view, dashboard, reference, audit trail, sign-in; light + dark |
| Extraction | ✅ **Groq** for text PDFs, **Gemini Vision** for scans |
| Automated tests | ✅ **329 passing** deterministically, 13 files, both providers mocked |
| Audit trail | ✅ Structured, deterministic, emitted by the rule engine itself |
| Human review | ✅ Accept/reject, recorded beside the automated decision |
| API security | ✅ OAuth2 bearer tokens, scopes, rate limits, input validation |
| Production safety | ✅ `APP_ENV=production` refuses demo creds / missing secret |
| Daily AI budget | ✅ Per-provider circuit breaker, SQLite-backed |
| Original audit defects | ✅ **All 3 fixed** |
| Gemini vision route | ⚠️ Intermittent **503** from Google — see §9 |
| Groq input truncation | ⚠️ **Open bug** — 413 on long documents, see §9 |
| Deployed | ❌ Local only |
| Demo video | ❌ Not recorded |

**Git:** 27 commits. Recent:

```
c0e00e9 Rewrite README for the current state of the project
f78be67 Separate demo config from production, and cap daily AI spend
9cffa9d Require authenticated, scoped, rate-limited access to the API
ff64560 Show the audit trail in the UI and let a reviewer accept or reject
3e45766 Record human review beside the automated decision, never on top of it
0b4f8dc Emit a deterministic audit trail from the decision evaluation
da396f6 Route text PDFs to Groq and keep Gemini for scanned pages only
ff0b1f3 Match vendors by normalised name instead of bidirectional substring
921d107 Require invoice totals to be greater than zero
be7ef8b Validate invoice arithmetic: subtotal + tax must equal the stated total
8a2c30b Compare invoice and PO currency, and hold mismatches for review
c2e81a3 Cap and disambiguate inferred PO matches, and make the warning bite
2865a58 Harden the PO ledger: atomicity, reversal + cascade, configurable tolerance
056d0f2 Harden the extraction layer against indirect prompt injection
5ed4a53 Swap the LLM extraction layer from Anthropic Claude to Google Gemini
```

**[README.md](README.md) is current and accurate** — rewritten `c0e00e9` against
the code, every figure verified. When the two documents disagree, trust the README.

---

## 3. ⚠️ Standing instruction — do not skip

**Phases 2–7 (§8) are NOT started and must not be started unprompted.**

The user works **one discrete step at a time, with a commit after each** — not
batched work. Every step so far was inspected, tested, verified and committed
before the next was requested. Keep doing that.

Open issues in §9 should be **documented and raised, not fixed** without being
asked.

---

## 4. Running it

```powershell
.\start.ps1          # installs deps, generates samples, starts server, opens browser
```

Then <http://127.0.0.1:8000> and sign in.

| Username | Password | Can |
|---|---|---|
| `viewer` | `demo-viewer` | read |
| `analyst` | `demo-analyst` | + process invoices |
| `reviewer` | `demo-reviewer` | + accept/reject held invoices |
| `admin` | `demo-admin` | + override any run's status |

```powershell
# Tests -- no server needed, no API key needed, no network
.\venv\Scripts\python.exe -m pytest tests/ -v

# Manual start
.\venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
```

### Hard-won gotchas — these cost time already

**`start.ps1` launched from a tool call does not survive.** The process tree gets
cleaned up when the call ends, and the browser then reports *"Run failed: Failed
to fetch"* — which looks like an app bug and is not. The server must be started
from a terminal the user owns. Diagnose by checking whether anything is
listening on 8000 before assuming the app broke.

**Set `AUTH_SECRET` in `.env` before demoing.** Without it a fresh signing key is
generated per process, so any server restart silently invalidates the token in
the browser and forces a re-login mid-recording.

**The server holds `data/app.db` open.** Kill it before deleting:

```bash
netstat -ano | grep ":8000" | grep LISTENING | awk '{print $5}' \
  | while read pid; do taskkill //F //PID $pid >/dev/null 2>&1; done
sleep 1.5 && rm -f data/app.db
```

**Tests that drive the API must authenticate.** `tests/conftest.py` provides
`auth_headers(role)`; pass it to `TestClient(main.app, headers=...)`.

**Stripping API keys in a test is not enough.** Entering the `TestClient` context
fires FastAPI's startup event, which calls `config.load_dotenv()` and puts the
real keys straight back. Stub the loader too, or the test quietly makes live
calls (this happened — two security tests were hitting Groq and taking 90s).

**Starting a `TestClient` wipes any PO you inserted.** Startup calls `init_db()`,
which re-seeds from `data/*.json`. Insert fixture POs *after* entering the
context.

**Insert POs with named columns.** A positional `INSERT INTO purchase_orders
VALUES (?,?,?,?,?,?,?)` breaks the moment the schema grows — which it did, twice.

**`/tmp` is not reliably writable** in this Git Bash environment. Use the
scratchpad directory.

**SSE output ends with a blank line**, so `curl ... | tail -1` returns nothing.
Use `grep '"final"'`.

**Sample order matters** (§7) — several cases are history-dependent by design.

**PDFs must stay binary in git.** `.gitattributes` has `*.pdf binary`.

**UI smoke-testing:** wait on `#verdictBar:not(.hidden)` and a `9 / 9 stages`
reading. Do **not** count `#stageList` children — they are pre-rendered
placeholders, and an earlier session produced a false pass by counting them.

---

## 5. Architecture

### Core philosophy — the thing to say in the interview

> **The AI reads, the rules decide.**

Extraction is hard for code and easy for an LLM (every vendor formats invoices
differently). The *decision* must be identical every time and defensible to an
auditor, so **no model ever touches a dollar comparison**. Everything downstream
of extraction is deterministic Python. No prompt contains the words approve,
reject, or tolerance.

Honest caveat worth stating: the LLM still *chooses the inputs* the verdict is
computed from, and those arrive as bare floats the rules trust absolutely. That
is what Phase 2 fixes.

### Request path

```
Sign in → authenticate → authorize (scope) → rate limit → daily AI budget
        → validate the file → pipeline → deterministic rules → audit trail
        → (if held) human accept / reject
```

### Pipeline

```
INGEST → EXTRACT_TEXT → EXTRACT_FIELDS → VALIDATE → VENDOR_CHECK
       → PO_MATCH → DUPLICATE_CHECK → TOLERANCE_CHECK → DECISION
```

Stages **do not short-circuit**. Findings accumulate and only the final stage
judges — an AP clerk should see the whole picture, not the first thing that went
wrong.

Wrinkle: tolerance **arithmetic** happens in stage 6 (`matching.match_po`).
Stage 8 only *reports* what stage 6 decided. The split exists for the run view.

### Decision hierarchy

- **REJECTED** — must not be overridden automatically: duplicates, vendor on file
  but not approved.
- **NEEDS_REVIEW** — recoverable: missing fields, unreadable scan, over tolerance,
  no PO match, currency mismatch, bad arithmetic, invalid total, inferred PO,
  injection-shaped text.
- **APPROVED** — everything passed.

Reject wins over review when both fire.

### Tolerance is deliberately one-sided

```python
within = diff <= tol        # NOT abs(diff) <= tol
```

Billing **over** the remaining PO balance is a problem. Billing **under** it is a
normal partial invoice. Tolerance is `max(1% of remaining, $50)`
(`config.PO_TOLERANCE_PERCENT` / `PO_TOLERANCE_DOLLARS`).

Originally written with `abs()` and it was wrong — it flagged every legitimate
split-PO invoice. The tests caught it.

### Split-PO tracking

No `consumed` column. Balance is derived per run:

```
remaining_before = PO_amount − Σ(totals of prior APPROVED runs on that PO)
```

Only **APPROVED** runs consume budget. Idempotency and reversal are therefore
*structural*: nothing is deducted, so nothing can be deducted twice, and moving a
run out of APPROVED refunds it in the same instant.

### Extraction routes

Chosen by what the document **is** — whether `extract_text()` finds a usable text
layer — never by extension. Same output schema regardless.

| Route | When | Provider | Key |
|---|---|---|---|
| `groq (text)` | text layer present | Groq | `GROQ_API_KEY` |
| `gemini (vision)` | image-only PDF | Gemini | `GEMINI_API_KEY` |
| `regex` | no Groq key, or Groq failed | — | — |
| `none` | nothing readable — empty fields, never guesses | — | — |

**Why the split:** Gemini's free tier is 20 requests per *day* and it is the only
route that can read a picture. Spending it on text PDFs that already have a
working regex fallback traded the irreplaceable route for the one with an
alternative. This is an economics decision, not an architectural one —
`matching.py`, `storage.py` and `rules.py` were untouched by the swap.

**Fallback order is deliberate.** Groq → regex, *not* Groq → Gemini. Falling
through would spend the scarce vision budget on a route that already has a local
fallback. A Gemini-only install (no Groq key) still uses `llm_extract_text`, so
nothing pre-existing was downgraded.

Models are **pinned**: `openai/gpt-oss-120b`, `gemini-3.7-flash`, both
overridable by env. An alias changes the model under a running system, and an AP
process must be able to say which model read an invoice approved months ago.

`llama-3.3-70b-versatile` is **not reachable** on this Groq account. Ask the API
what it can reach (`client.models.list()`) rather than trusting a name from
memory — the same trap `gemini-2.0-flash` sprang.

Rasterisation uses **pypdfium2** — a self-contained wheel, no poppler/tesseract.

### The audit trail

`rules.decide(audit={})` fills a structured record **as it evaluates** — each
`_check(...)` sits next to the branch that sets `reject`/`review` and reads the
same variable. It is not a second pass.

That distinction is the whole point: a trail assembled separately can disagree
with the decision it claims to explain. No model is involved; there is a test
that fails if evaluation so much as touches a model client, and another pinning
that `rules.py` imports no SDK.

Contains: automated decision, deterministic reason, invoice identity, extraction
route/provider, matched PO with `source_file` + `source_row`, the values
compared, variance, tolerance, and every rule passed/failed.

PO provenance is seeded from the record's position in `data/purchase_orders.json`
(1-based); a record carrying its own `source_row` wins, which is what a
spreadsheet export would provide. Nothing is invented — no derivable position
stores `NULL` and the trail says the row is unknown.

### Human review

```
automated_decision   NEEDS_REVIEW      ← written once, never rewritten
human_decision       ACCEPTED
final_decision       HUMAN_APPROVED
```

`status` does move, because that is the column the ledger sums. It moves through
the existing `set_run_status`, so reversal and cascade keep working.

Only runs whose **automated** decision was NEEDS_REVIEW are eligible, enforced in
storage. **One ruling per run** — because `automated_decision` stays NEEDS_REVIEW
forever, the eligibility check would otherwise keep passing and let a caller
silently rewrite who decided what. Reversal is an admin action through `/status`.

Reviewer identity comes from the **token**, never the request body.

### API security

The frontend is an untrusted client. CORS is configured but is **not** a security
boundary — a script ignores it.

- **Authentication** — OAuth 2.0 resource-server pattern, `Authorization: Bearer
  <JWT>`, validated for signature, expiry **and** issuer. Swapping in a hosted
  IdP means verifying against its JWKS and changing nothing else.
- **Authorization** — scopes named for actions: `invoice:read`,
  `invoice:process`, `invoice:review`, `invoice:admin`. Reviewing is separate
  from processing: approving payment is a different authority from feeding a PDF
  to an extractor.
- **Rate limiting** — per user and per IP, sliding window, default 20
  processing/min/user. Authentication runs *first*, so an anonymous flood cannot
  burn a real user's budget.
- **Daily AI budget** (`quota.py`) — a slower breaker. Twenty polite requests an
  hour apart never trip a per-minute limit and would still exhaust Gemini for the
  day. When spent, the provider is **not called** and extraction takes its
  existing safe fallback. Counter lives in SQLite so it survives a restart.
  Deliberately **fails open** if its own table breaks — it is a cost guard, not a
  security control; the security controls all fail closed.
- **Input** — uploads read in capped chunks (not buffered then measured), PDFs
  validated by magic bytes, filenames reduced to a safe basename.
- **Errors** — 401/403/404/409/413/415/429/500 with no stack traces, provider
  messages or config names. A crash mid-SSE cannot become a 500 (headers already
  sent), so the pipeline is wrapped to emit one clean error event.

**Production safety** — `APP_ENV=production` refuses to start on: missing
`AUTH_SECRET` (no ephemeral fallback), demo credentials present, empty user
store, or wildcard CORS. The demo flag lives on the **record**, not the file
path, so copying `users.json` elsewhere does not launder it. All problems are
reported at once.

### Prompt-injection defence

A vendor invoice is **attacker-controlled input**. Four controls, in descending
order of weight:

1. **Architecture.** No model output reaches a verdict. The blast radius of a
   successful injection is *wrong numbers*, never *wrong decision*.
2. **Closed response schema** (Gemini). A document demanding
   `{"status":"APPROVED"}` cannot produce that key. Groq's JSON mode guarantees
   valid JSON, not *which* JSON — its closing boundary is
   `_invoice_from_payload`, which reads only the nine known keys into a fixed
   dataclass. Tested.
3. **Fenced prompt.** `wrap_untrusted()` defangs a closing tag already inside the
   document. Images get the same fence.
4. **Post-extraction guard** — scans extracted strings *and* `raw_text`, forces
   NEEDS_REVIEW, never raises.

**Forces review, never rejection.** Auto-rejecting on a keyword would let anyone
block a competitor's payment by printing a phrase on an invoice.

Patterns are deliberately **narrow** — a guard that flags "System Integration
Services" trains clerks to click through warnings, which is worse than no guard.

### Stack

FastAPI + SQLite + vanilla JS (no build step). `POST /api/runs/stream` streams
stages over SSE, read with `fetch()`. `pyjwt` for tokens; PBKDF2-HMAC-SHA256
password hashing from the stdlib.

---

## 6. Files

```
backend/
  main.py         622 lines. FastAPI app, 9-stage pipeline as an async generator,
                  auth endpoints, error handlers, upload validation.
  extraction.py   744 lines. PDF → text → fields. Groq/Gemini/regex/none,
                  SCHEMA_PROMPT, injection guard. The ONLY module that talks
                  to a model; both SDKs imported lazily.
  rules.py        575 lines. Validation, vendor tri-state, duplicates, decide()
                  and build_audit(). The only place a verdict is produced.
  storage.py      617 lines. SQLite, ledger, write_txn(), save_run_checked(),
                  record_human_review(), migrations via _ensure_columns().
  auth.py         324 lines. OAuth2 bearer tokens, scopes, user store,
                  production config enforcement.
  config.py       244 lines. .env loader, APP_ENV, provider settings, tolerances,
                  rate limits, daily quotas. Operational settings + policy numbers.
  matching.py     174 lines. PO lookup, tolerance_for(), split-PO maths, currency.
  quota.py        142 lines. Daily per-provider budget, SQLite-backed.
  ratelimit.py    130 lines. Sliding-window per user/IP.
  schemas.py       65 lines. ExtractedInvoice, LineItem, StageLog, RunResult.
frontend/         index.html, style.css, app.js — sign-in, run view, dashboard,
                  reference, audit panel, accept/reject. No framework, no build.
data/
  purchase_orders.json / approved_vendors.json / users.json   (TRACKED)
  app.db                                                      (NOT tracked)
sample_invoices/  7 PDFs, generate_invoices.py, manifest.json
tests/            13 files, 329 tests. conftest.py provides auth_headers().
```

### Test suite — 329 tests, 13 files

| File | n | Covers |
|---|---|---|
| `test_api_security.py` | 59 | authn, authz, rate limits, secrets, input, errors |
| `test_vendor_matching.py` | 40 | normalisation, substrings, ambiguity |
| `test_production_safety.py` | 39 | APP_ENV gates, demo creds, daily quota |
| `test_human_review.py` | 28 | accept/reject, ledger effect, eligibility |
| `test_security.py` | 27 | prompt injection, false-positive floor |
| `test_extraction_routing.py` | 23 | Groq/Gemini routing, failure fallbacks |
| `test_audit_trail.py` | 22 | trail structure, provenance, determinism |
| `test_arithmetic.py` | 22 | subtotal + tax == total |
| `test_invalid_amount.py` | 21 | zero / negative totals |
| `test_currency.py` | 16 | mismatch → review, no conversion |
| `test_inferred_po.py` | 13 | distance cap, ambiguity guard |
| `test_po_edge_cases.py` | 12 | split-PO, idempotency, reversal, concurrency |
| `test_samples.py` | 7 | the 7 samples end to end, in manifest order |

Notes for whoever changes them:

* Both providers are **mocked at the transport boundary**, so the suite needs no
  key, no network and no quota. `test_samples.py` is the exception — it honours
  a live key and exercises the real routes, printing which mode ran.
* `test_samples.py` cases share one DB and run in manifest order. An early
  failure cascades — inherent, since later cases are *about* the state earlier
  ones left.
* The concurrency test uses real threads and a `Barrier`, exercising actual
  SQLite locking. Its retry-on-locked loop is part of what it asserts.

### API endpoints

```
GET  /api/health                 public
POST /api/auth/token             OAuth2 password grant  (rate limited per IP)
GET  /api/auth/me                authenticated
POST /api/runs/stream            [invoice:process]  + rate limit + daily budget
GET  /api/runs                   [invoice:read]
GET  /api/runs/{id}              [invoice:read]   includes the audit trail
POST /api/runs/{id}/review       [invoice:review]
POST /api/runs/{id}/status       [invoice:admin]  cascades to held invoices
GET  /api/reference              [invoice:read]
GET  /api/sample-invoices        [invoice:read]
GET  /api/sample-invoices/{name} [invoice:read]
```

### Seed data

| PO | Vendor | Amount | Status |
|---|---|---|---|
| PO-1001 | Acme Office Supplies | $1,240.00 | open |
| PO-1002 | Globex Logistics | $5,000.00 | open |
| PO-1003 | Initech Consulting | $8,200.00 | open |
| PO-1004 | Umbrella Cleaning Co | $600.00 | **closed** |
| PO-1005 | Stark Industrial Parts | $15,400.00 | open |

All five vendors approved (V-001 … V-005).

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

† Sample 05 is route-dependent by design: no key → nothing to read → refuse to
guess → NEEDS_REVIEW; with the vision route → reads INV-9004 / PO-1005 /
$15,400.00 → APPROVED. `manifest.json` carries both, resolved against
`config.has_api_key()`.

**Sample 04 legitimately fails two rules.** Its PDF states Subtotal $8,200 + Tax
$0.00 = Total $8,150, which does not add up. Both routes read it identically, so
the arithmetic check is correct — the fixture is inconsistent. Verdict unaffected.

### The demo money-shot

Run `03b` **alone against a fresh DB** → **APPROVED**. Same bytes, opposite
verdict, because $2,500 against an untouched $5,000 PO is an ordinary partial
invoice. The decision depends on the PO's history, not the file alone. Watch
"Remaining before" go **$5,000 → $2,000 → $0**.

---

## 8. Phase table

| Phase | Work | State |
|---|---|---|
| 0 | Green build, pytest suite | ✅ done |
| 1 | Inferred-PO safety, currency, arithmetic, invalid amounts, vendor matching | ✅ done |
| 2 | `Tracked[T]` provenance, per-route confidence, **confidence gate** | ⬜ |
| 3 | `rules.yaml` versioned policy + typed loader | ◨ thresholds in `config.py`; YAML to do |
| 4 | Transactions; `run_allocations` table | ◨ transactions done; allocations to do |
| 5 | `DecisionTrace` + reference snapshot; stop re-seeding | ◨ audit trail done; snapshot to do |
| 6 | Line-item decomposition, multi-PO consolidation, FX provider | ⬜ |
| 7 | UI: confidence badges, evidence snippets, allocation view | ⬜ |

**Sequencing trap:** multi-PO consolidation is a **ledger** feature, not a
matching feature. The schema stores one `po_number` per run and consumption sums
run totals, so a consolidated invoice would over-consume every PO it touched.
Phase 4 must land before Phase 6.

**Most valuable thing left:** Phase 2's confidence gate — it closes the
low-confidence auto-approve problem as a *class*. But nothing in Phases 2–7
changes a verdict on any of the seven samples, so none of it blocks the case
study.

---

## 9. Open issues

⚠️ **1. Groq 413 on long documents — OPEN BUG, not fixed.**
`groq_extract_text` truncates document text at **60,000 characters**, a limit
inherited from the Gemini path. Groq rejects well before that. Measured against
an 11-page, 30,831-character PDF:

```
30,000 chars -> 413
20,000 chars -> OK
12,000 chars -> OK
```

So **any document past roughly 20k characters (~7+ pages) silently loses the LLM
route** and drops to regex. The failure is safe and visible in the run trail
("Groq text extraction failed - APIStatusError (413). Used regex instead."), but
a real multi-page invoice would be read by regex without anyone intending it. All
seven samples are one page, so it has never surfaced there. Fix is one constant.

⚠️ **2. Gemini vision returns intermittent 503.** Google-side unavailability, not
quota and not our code. Observed 0/5 on one probe and working minutes later. The
fallback is correct (route `none` → NEEDS_REVIEW, nothing fabricated), but the
scanned-sample demo is not reliably reproducible, and `manifest.json` resolves
that sample's badge on key *presence* rather than provider *availability* — so
the badge can contradict the run beside it. **Decide before recording.**

⚠️ **3. `data/app.db` accumulates test runs.** Reset before the demo (§4) and
replay samples 1→7 in order for a clean dashboard.

⚠️ **4. `extraction._first()` strips a leading minus sign** off a captured
amount, so `Total Due: -500.00` extracts as +500. Accounting parentheses are
unaffected. The amount rule cannot catch a sign the extractor discarded.

### Design gaps (deliberate, queued)

- Extracted fields are **bare values** — no confidence, no provenance. Phase 2.
- Business rules are **constants in `config.py`**, not versioned policy. Phase 3.
- Reference data **re-seeded from JSON on every startup**, so editing
  `purchase_orders.json` silently changes what historical runs mean. Phase 5.
- Schema stores **one `po_number` per run** — no multi-PO. Phase 4b/6.
- Rate-limit counters are **per process** — several workers multiply the limit.
- `_guess_vendor` picks the vendor by **line position**.

### Case-study deliverables still outstanding

1. Settle the sample-05 badge question (issue 2).
2. Reset the database and replay 1→7.
3. **Publish to GitHub** — in progress.
4. Deploy somewhere shareable, and smoke-test the *deployed* instance (the DB
   path, the env and SSE buffering all change). `GEMINI_API_KEY`, `GROQ_API_KEY`
   and `AUTH_SECRET` must be **host env vars**, never a committed `.env`.
5. **Record the 5-minute demo video.**

---

## 10. Decisions already made — don't relitigate

| Decision | Why |
|---|---|
| **PS-1** over PS-2/PS-3 | Only PS where inputs are real artefacts and the decision is verifiable. |
| Rules deterministic, LLM extraction-only | Auditability. The headline claim; it survived the audit. |
| Three verdicts, not two | Binary forces guessing on ambiguous invoices; the middle state is where automation hands back to a human. |
| Tolerance one-sided | Over-billing is a problem; under-billing is a normal partial. |
| Balance derived from run history, not a stored counter | No counter can drift from what was actually approved, and it makes idempotency and reversal structural. Reaffirmed when a `remaining_amount` column was proposed. |
| Only APPROVED runs consume budget | A flagged invoice mustn't block the queue behind it. |
| Refuse to guess when unreadable | Empty fields → review, rather than fabricating. `vendor_check` is tri-state: not-on-list (reject) ≠ couldn't-read-a-name (review). |
| pypdfium2 over pytesseract | Self-contained wheel; no system binaries for a reviewer to install. |
| No rule engine (JSON-logic etc.) | One-sided tolerance and ledger-derived balances express badly in a DSL; a sign error in exactly that comparison has been a bug twice. YAML for policy, Python for predicates. |
| FX conversion must not widen auto-approval | A verdict depending on a rate fetched at run time is not reproducible by an auditor. |
| Pydantic `Field()` rejected for confidence | Class-level schema metadata; confidence is per-instance data. Use `Tracked[T]`. |
| **Groq for text, Gemini for vision** | Gemini's free tier is 20/day and is the only route that can read a picture. Economics, not architecture — the swap touched only `extraction.py` + `config.py`. |
| Groq failure → regex, NOT → Gemini | Falling through would spend the scarce vision budget on a route that already has a local fallback. |
| Models **pinned**, not aliased | An alias changes the model under a running system. |
| Audit trail built inside `decide()` | A trail assembled by a second pass can disagree with the decision it claims to explain. |
| Human decision stored **beside** the automated one | An audit wants to know whether a person overrode the process, who, and when — unanswerable once the original verdict is overwritten. |
| One human ruling per run | `automated_decision` stays NEEDS_REVIEW forever, so the eligibility check would otherwise let a client silently rewrite the audit record. |
| Reviewer identity from the token only | A record saying whatever the client typed is not evidence of anything. |
| OAuth2 password grant, not a custom scheme | Standard grant, standard token shape; swapping in a hosted IdP changes only how the signature is verified. |
| No default signing secret, ever | A secret in a repository is not a secret; a deployment that forgot to set one would silently sign forgeable tokens. |
| Quota breaker fails **open** | It is a cost guard, not a security control. Security controls all fail closed; the asymmetry is intentional. |
| Injection guard forces review, never reject | Auto-rejecting on a keyword lets anyone block a competitor's payment. |
| Injection patterns narrow, not fuzzy | A guard that flags "System Integration Services" trains clerks to click through warnings. |
| Test suite honours a live key rather than mocking (samples only) | A mocked LLM proves nothing about whether extraction works. Everything else is mocked so CI needs no key. |

### Bugs already found and fixed — don't reintroduce

1. `Total` regex matched inside `Subtotal` → wrong amount.
2. Invoice-number regex matched prose in a footnote.
3. `abs()` in the tolerance check → every legitimate partial flagged.
4. PO regex emitted both `1002` and `PO-1002` → deduplicated.
5. `requirements.txt` missing `pypdfium2`.
6. **Gemini client garbage-collected mid-call.** Hold the client in a local —
   google-genai closes its transport when the Client is collected. The regex
   fallback *masked* it as a tidy `route=none`, not a crash.
7. **`gemini-2.0-flash` is retired** — ask the API what it can reach.
8. **`llama-3.3-70b-versatile` unavailable on this Groq account** — same lesson.
9. **A run could be reviewed repeatedly**, each ruling overwriting the last.
10. **Non-string `decision` reached `.strip()`** → 500 instead of 400.
11. **`describe_api_error()` leaked config names** (`GEMINI_API_KEY`,
    `config.EXTRACTION_MODEL`) into a message stored on the run and shown in the
    browser.
12. **Path traversal** in `/api/sample-invoices/{name}` — on Windows a
    backslash parent reference escaped the samples directory.
13. **`MAX_UPLOAD_BYTES` was dead** — defined in config, never enforced.

---

## 11. Working conventions

**Commits:** end every message with

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Messages are detailed — what changed, why, and what was verified. Match that.
Commit after each discrete step, not in batches.

**Verify empirically, don't assume.** Every claim in the README was checked
against the code before it was written. Keep doing that.

**Be honest about what doesn't work.** The user has responded well to being told
the app was broken, that `requirements.txt` would break a clone, that tests were
making live API calls, and that a security test was passing against a file it
was not sending. Don't paper over gaps.

**Temp scripts:** the scratchpad directory, or `_*.py` in the project root
(gitignored). Delete them afterwards.

**Browser checks:** Playwright + Chromium are installed in the venv.
