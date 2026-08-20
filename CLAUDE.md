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
(2–4 required; there are 6: split-PO tracking, missing required field, scanned/
no-text-layer, duplicate detection, multi-PO consolidation, currency mismatch
+ FX conversion).

**Working directory:** `c:\Users\adity\OneDrive\Desktop\Invoice processing`
Windows 11. PowerShell is primary; a Bash tool is also available.

---

## 2. Current status

| | |
|---|---|
| Pipeline | ✅ Working, 9 stages, streamed live over SSE |
| Samples | ✅ 10/10 match the manifest, driven through the real pipeline |
| UI | ✅ **Next.js 15 + React 19 + Tailwind v4**, 4 sections, light + dark |
| Extraction | ✅ **Groq** for text PDFs, **Gemini Vision** for scans |
| Automated tests | ✅ **446 passing** deterministically, 18 files, both providers mocked |
| Audit trail | ✅ Structured, deterministic, emitted by the rule engine itself |
| Human review | ✅ Accept/reject, recorded beside the automated decision |
| API security | ✅ OAuth2 bearer tokens, scopes, rate limits, input validation |
| Production safety | ✅ `APP_ENV=production` refuses demo creds / missing secret |
| Daily AI budget | ✅ Per-provider circuit breaker, SQLite-backed |
| Non-invoice detection | ✅ Rejects documents containing no invoice, saying so |
| Multi-PO invoices | ✅ `run_allocations` ledger; split calculated, always held |
| Currency mismatch + FX | ✅ Pinned-rate conversion; same-number collision rejects |
| Confidence gate + provenance | ✅ Phase 2 — self-reported/heuristic, gates the decision |
| Reviewer brief | ✅ Why flagged, field, evidence, suggested resolution — before Accept/Reject |
| Demo reset | ✅ Admin button in the UI, plus `.\reset-demo.ps1` |
| Original audit defects | ✅ **All 3 fixed** |
| Gemini vision route | ⚠️ Intermittent **503** from Google — see §9 |
| Groq input truncation | ⚠️ **Open bug** — 413 on long documents, see §9 |
| Published | ✅ **<https://github.com/AdityaSingh910/invoice-processing>** (public) |
| Deployed (hosted) | ❌ Runs locally only |
| Demo video | ❌ Not recorded |

**Git:** 39 commits on `main`, pushed to
<https://github.com/AdityaSingh910/invoice-processing> (public). Working tree
clean, local and remote identical. Verified at push time that `.env`,
`data/app.db` and `data/app.db.bak` are absent from the published tree, that
`frontend-next/node_modules`, `.next/` and `out/` are ignored, and that no key
material appears in any commit. Recent:

```
824d45b Recognise documents that are not invoices, and reject them saying so
8da60b7 Let an admin clear run history from the UI, and explain duplicate rejections
634bdc7 Add a one-command reset so the samples keep telling their intended story
e71d34c Redesign the UI as a premium AP product: surfaces, hierarchy, density
bdf12a0 Rebuild the UI as a production-grade AP application
738e2a5 Redesign again: colourful, friendly, and in plain language
f3a1058 Never cache the HTML shell, so the browser cannot pin an old build
5c6c69a Redesign the UI: new visual system, same screens and information
4844e4e Rebuild the frontend as a Next.js + Tailwind app on the same backend
c9fc298 Record the GitHub publish in CLAUDE.md and add a fresh-session checklist
e836bd9 Update CLAUDE.md to the current state of the project
c0e00e9 Rewrite README for the current state of the project
f78be67 Separate demo config from production, and cap daily AI spend
9cffa9d Require authenticated, scoped, rate-limited access to the API
```

**[README.md](README.md) is current and accurate** — every figure re-verified
against the code at the same time as this file. When the two disagree, trust the
README.

---

## 3. ⚠️ Standing instruction — do not skip

**The remaining phases (§8) must not be started unprompted.** Phase 4, the
multi-PO part of Phases 6/7, the FX/currency reversal, and now Phase 2
(confidence + provenance + the confidence gate) are all done — every one of
them only because the user explicitly asked. Phase 3, Phase 5, and the rest of
6/7 (line-item decomposition, a broader/live FX provider, evidence snippets
beyond what Phase 2 already added) are untouched and stay that way until asked.
The FX reversal in particular is recorded in §10 as *why* it changed, not as a
silent overwrite — read that entry before assuming a prior "don't relitigate"
decision is still in force; this project has now shown one can be reopened when
the user asks for it by name.

The user works **one discrete step at a time, with a commit after each** — not
batched work. Every step so far was inspected, tested, verified and committed
before the next was requested. Keep doing that. The multi-PO work was two
commits for this reason: the ledger first, behaviour-neutral and separately
verified, then the matching on top of it.

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
# Reset run history so the samples tell their intended story again.
# Also available in the UI: sign in as admin -> Overview -> "Reset demo data".
.\reset-demo.ps1              # clear only
.\reset-demo.ps1 -Replay      # clear, then drive all 10 through the API in order

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
  but not approved, **document is not an invoice**, invoice states the PO's own
  number under a different currency (§ Currency mismatch and FX).
- **NEEDS_REVIEW** — recoverable: missing fields, unreadable scan, over tolerance,
  no PO match, a currency mismatch with no pinned rate or that still doesn't fit
  after conversion, bad arithmetic, invalid total, inferred PO, multi-PO invoice
  with no stated split, injection-shaped text.
- **APPROVED** — everything passed. Includes a currency mismatch a pinned rate
  resolves within tolerance.

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

### Split-PO tracking and the allocation ledger

No `consumed` column. Balance is derived per run:

```
remaining_before = PO_amount − Σ(allocations to that PO from prior APPROVED runs)
```

Only **APPROVED** runs consume budget. Idempotency and reversal are therefore
*structural*: nothing is deducted, so nothing can be deducted twice, and moving a
run out of APPROVED refunds it in the same instant.

**`run_allocations` records how much of each run went to which PO.** This used to
be `SUM(runs.total) WHERE po_number=?`, which assumed one PO per invoice — so an
invoice covering two POs was charged entirely to the first (measured: PO-1001 at
−$5,000 while PO-1002 stayed untouched at $5,000).

It is **not** the stored counter that was rejected twice. A counter would be
authoritative and would need an explicit refund on reversal. An allocation is an
*immutable fact* about a run — this invoice billed $X to PO-Y — and whether it
**counts** is still derived at read time by joining to `runs.status='APPROVED'`.
Every property the derived design was chosen for survives intact.

A single-PO run is simply a run with one allocation, so the ledger has no special
case. Legacy runs are backfilled from `(po_number, total)`, which is the one
allocation they always implied; the migration is idempotent and moves no balance.

### Invoices covering several POs

`match_po` binds **every** referenced PO and `split_across()` divides the total:
fill each to its remaining balance, in the order the invoice named them, with the
last absorbing any excess so the allocations always sum to the invoice total.
Top-level `po_match` figures describe the **combined** position; `po_numbers`,
`allocations` and `is_multi` carry the detail.

**A multi-PO invoice is always NEEDS_REVIEW**, even when the combined balance
covers it comfortably. Nothing on the document states the split — line items
carry no PO references — so the division is *computed*. Approving it would commit
money against POs in amounts no document and no person specified. Same objection
as an inferred single-PO match, applied to the division rather than the binding.
The proposal is stored and shown so the reviewer confirms figures instead of
working them out; the audit trail records `allocation_basis: calculated`.

The cascade (`reevaluate_po_queue`) deliberately **skips** multi-PO runs: they
are held on the unstated split, not a short balance, so freeing budget elsewhere
must not release them.

### Currency mismatch and FX conversion

**This reverses a prior "decisions already made" entry** ("FX conversion must
not widen auto-approval") — done explicitly at the user's request, not
unprompted. The old rule held any currency mismatch for review, unconditionally,
because a rate fetched at run time is not reproducible by an auditor. That
objection is about *when* the rate is fetched; it does not apply to a table that
is pinned and versioned (`config.FX_RATES` / `FX_RATES_VERSION`, same pinning
argument as the extraction models). Three outcomes now:

1. **Pinned rate resolves within tolerance → APPROVED.** `matching.fx_convert()`
   converts at `FX_RATES[from]/FX_RATES[to]`; the ledger consumes the converted
   amount (`po_match["fx"]["converted_total"]`), never the raw foreign-currency
   digits. The audit trail's `currency.fx` block names the rate and version.
2. **Same raw number, different currency → REJECTED.** `1500` billed as EUR
   against a `1500` USD PO. No correct conversion produces identical digits in a
   different currency, so this is not an ordinary discrepancy — it reads as a
   currency-code error or a copied figure, and paying face value silently mis-
   pays by the full FX difference. `po_match["currency_same_number_suspected"]`;
   checked against both `po_amount` and `remaining_before`. This is the ONE
   place a currency finding rejects rather than holds.
3. **No pinned rate, or converted amount still doesn't fit → NEEDS_REVIEW.**
   Unchanged from before this feature existed.

`po_match["invoice_total"]` stays the RAW total in the invoice's own currency,
always — `po_match["fx"]["converted_total"]` is the PO-currency equivalent used
for every balance comparison. A UI pairing `invoice_total` with `invoice_currency`
must never show the converted figure; both frontends had exactly this bug
(`money()` hardcoded `$` for the invoice's own total, subtotal and tax — visible
once a non-USD sample could reach a clean run view) and it was fixed alongside
this feature, not before it, because nothing exposed it earlier.

`extraction.MONEY` regex was extended to recognise a 3-letter currency CODE
before an amount (`"EUR 2,000.00"`), not just a symbol — needed for sample 08/09
to extract under the regex fallback. `_to_float()` already stripped non-numeric
characters, so this needed no downstream change.

### Confidence, provenance and the confidence gate (Phase 2)

**Built at explicit request** — Phase 2 was "the most valuable thing left" and
explicitly not-yet-started until asked for directly. Two scoping questions were
asked and answered before writing code: should low confidence actually change
the verdict (yes — wired in, not just displayed), and where does the score come
from (the model self-reports it; regex gets a heuristic).

`ExtractedInvoice.provenance: dict` — `{field_name: {confidence, source,
evidence, evidence_verified}}` — additive only. Every existing consumer reading
`extracted["total"]` etc. as a bare value is unaffected; the dict field just
rides along in `extracted_json`, no DB migration needed (same reason
`run_allocations` did the opposite and needed one: allocations are structural
to the ledger, provenance is descriptive metadata).

**Where confidence comes from:**
- LLM routes (Groq/Gemini) — the SAME prompt/JSON call now also asks for
  `confidence` (0-1) and a verbatim `evidence` quote per field
  (`extraction.PROVENANCE_FIELDS`). No second pass, no extra request.
  `evidence_verified` is computed in `_build_provenance()`: is the quoted
  snippet actually a substring of the extracted text? A model can hallucinate
  a quote as easily as a value; showing it as "evidence" without checking
  would be worse than not showing one.
- Regex has no self-assessment, so `regex_extract()` assigns a fixed score per
  KIND of match: explicit labelled match ("Invoice #:") = 0.9, `_guess_vendor`
  (a known weak positional heuristic) = 0.72, a value computed rather than
  printed (total = subtotal + tax with no printed total) = 0.55 — deliberately
  below the gate threshold.
- Source location is honest about what is knowable. Regex reports a real line
  number. The LLM text route gets ONE flattened string spanning every page with
  no boundary preserved, so it says "page 1" for a single-page document (every
  current sample) and "page not tracked (N-page document)" otherwise — never
  fabricated per-page precision.

**Stated, not glossed over:** model self-reported confidence skews high and is
not independently calibrated. Still a genuine signal, not a guarantee — the
gate's own reason text says so.

**The gate** — `config.CONFIDENCE_GATED_FIELDS = [vendor_name, invoice_number,
total]` (same fields `REQUIRED_FIELDS` already treats as central),
`config.CONFIDENCE_THRESHOLD = 0.65`. `rules.validate_confidence()` returns the
gated fields that ARE present but scored below threshold — a field that is
missing entirely stays `validate_required_fields()`'s business, so absence is
never double-counted as also "low confidence". New "Extraction confidence"
check in `decide()`, placed right after "Required fields present" (a reading-
quality problem is more fundamental than whether the numbers reconcile).
**Only ever holds, never rejects** — same as every other extraction-uncertainty
signal (unreadable scan, injection guard).

**Suggested resolution + problematic fields** — `build_audit()` now also
returns `suggested_resolution` (one deterministic sentence, static text keyed
by rule name in `rules._SUGGESTED_RESOLUTIONS`, looked up from the SAME
first-failing-check that already produces `reason` — never generated) and
`problematic_fields` (every field any failing check implicates,
`rules._RULE_FIELDS`, de-duplicated; `missing_fields`/`low_confidence` fill in
the two checks whose fields vary per run). Both None/empty on APPROVED.

**UI: "Reviewer brief".** A new panel (`Panels.tsx` → `ReviewerBrief`; vanilla
→ `reviewerBriefHTML()`) sits right before the Accept/Reject buttons on any
non-APPROVED run: why it was flagged (`audit.reason`), which field(s)
(`problematic_fields`, each with its confidence badge + quoted evidence +
"unverified" flag when applicable), and the suggested next step. Confidence
badges also appear inline on every field in "Extracted data"
(`ExtractedFields`/the vanilla fields table) — coloured by whether the RULE
ENGINE actually flagged that field (`audit.low_confidence_fields`), not by
re-deriving the threshold in the browser, so the UI's read of "low" can never
drift from what `decide()` used. PO information, and who-reviewed-when, were
already shown elsewhere (`PoMatchPanel`, `HumanRuling`) — not duplicated here.

**Found and fixed while building this:** the vanilla UI's confidence badge had
its CSS class names backwards — `provBadge()` emitted `prov-ok`/`prov-warn`/
`prov-bad` but the CSS selectors were `.prov-badge.ok` etc. (bare tone names).
Caught by extracting the function and running it against real data outside the
browser, not by eyeballing the diff — the badges would have silently rendered
with no colour at all.

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

FastAPI + SQLite + **Next.js 15 / React 19 / Tailwind v4 / TypeScript**.
`POST /api/runs/stream` streams stages over SSE, read with `fetch()` and a
`ReadableStream` reader. `pyjwt` for tokens; PBKDF2-HMAC-SHA256 password hashing
from the stdlib.

**The UI ships as a static export**, not a Node server. `npm run build` in
`frontend-next/` emits plain HTML/JS into `out/`, and `backend/main.py` mounts
that at `/`. Consequences worth knowing:

* The UI is **same-origin** with the API, so relative `/api/...` paths resolve
  with no CORS and no base URL. `next dev` on :3000 proxies `/api` to :8000 so
  the same relative paths work in development.
* Nothing Node runs at serve time. One process, one port, `start.ps1` unchanged.
* `FRONTEND_DIR` falls back to the original `frontend/` when `out/` has not been
  built, so a clone without npm still boots a working UI and the Python suite is
  unaffected.
* **Navigation is client-side state, not routes.** Real paths would need the
  static mount to resolve deep links — a backend change for no gain across four
  sections. Documented in `AppShell.tsx` so it does not read as an oversight.
* The HTML shell is served `Cache-Control: no-store`; hashed `_next/*` assets
  stay cacheable. Without this the browser pins itself to a build that no longer
  exists on disk — it happened twice, and looked like the app was broken.

---

## 6. Files

```
backend/
  main.py         675 lines. FastAPI app, 9-stage pipeline as an async generator,
                  auth endpoints, error handlers, upload validation,
                  /api/admin/reset-demo, no-store on the HTML shell.
  extraction.py   744 lines. PDF → text → fields. Groq/Gemini/regex/none,
                  SCHEMA_PROMPT, injection guard. The ONLY module that talks
                  to a model; both SDKs imported lazily.
  rules.py        655 lines. Validation, vendor tri-state, duplicates,
                  is_not_an_invoice(), decide() and build_audit().
                  The only place a verdict is produced.
  storage.py      SQLite, ledger, write_txn(), save_run_checked(),
                  record_human_review(), clear_run_history(), run_allocations
                  + its backfill, migrations via _ensure_columns().
  auth.py         324 lines. OAuth2 bearer tokens, scopes, user store,
                  production config enforcement.
  config.py       .env loader, APP_ENV, provider settings, tolerances, rate
                  limits, daily quotas, FX_RATES + FX_RATES_VERSION (pinned).
  matching.py     PO lookup, tolerance_for(), split-PO maths, currency,
                  multi-PO binding, split_across(), fx_convert().
  quota.py        142 lines. Daily per-provider budget, SQLite-backed.
  ratelimit.py    130 lines. Sliding-window per user/IP.
  schemas.py       65 lines. ExtractedInvoice, LineItem, StageLog, RunResult.
frontend-next/    ~5,700 lines across 27 source files. Next.js 15 + React 19 +
                  Tailwind v4 + TypeScript. Built to a STATIC EXPORT in out/.
  app/            layout.tsx (Inter via next/font), page.tsx (auth gate +
                  section switch), globals.css (the whole design system:
                  surfaces, type scale, semantic colours, motion).
  components/
    layout/       AppShell.tsx — sidebar, page chrome, mobile drawer.
    pages/        OverviewPage, ProcessPage, InvoicesPage, ReferencePage.
    invoice/      RunDetail, StageList (+PhaseStepper), PoMatchPanel,
                  Panels (verdict/extraction/reasons), AuditTrail, ReviewBar.
    ui/           index.tsx (primitives), Modal.tsx (focus trap),
                  Toast.tsx, icons.tsx (16px/1.5-stroke set).
    ResetDemoButton.tsx, charts.tsx (hand-drawn SVG, no chart library).
  lib/            api.ts (fetch + SSE reader), auth.tsx, useData.ts,
                  metrics.ts (dashboard figures), format.ts, types.ts.
frontend/         The ORIGINAL vanilla UI. Kept deliberately: main.py serves it
                  when frontend-next/out is missing, so a clone without npm
                  still boots and the Python suite is unaffected.
data/
  purchase_orders.json / approved_vendors.json / users.json   (TRACKED)
  app.db                                                      (NOT tracked)
sample_invoices/  10 PDFs, generate_invoices.py, manifest.json
scripts/          replay_samples.py — drives the 7 samples in manifest order
                  and checks each verdict. Used by reset-demo.ps1 -Replay.
reset-demo.ps1    Clears run history (and optionally replays the samples).
tests/            18 files, 446 tests. conftest.py provides auth_headers()
                  as a plain function, NOT a fixture -- import it.
```

### Test suite — 446 tests, 18 files

| File | n | Covers |
|---|---|---|
| `test_api_security.py` | 59 | authn, authz, rate limits, secrets, input, errors |
| `test_document_type.py` | 19 | the not-an-invoice check and its degraded-route gate |
| `test_reset_demo.py` | 11 | who may clear run history, and what survives it |
| `test_vendor_matching.py` | 40 | normalisation, substrings, ambiguity |
| `test_production_safety.py` | 39 | APP_ENV gates, demo creds, daily quota |
| `test_human_review.py` | 28 | accept/reject, ledger effect, eligibility |
| `test_security.py` | 27 | prompt injection, false-positive floor |
| `test_extraction_routing.py` | 23 | Groq/Gemini routing, failure fallbacks |
| `test_audit_trail.py` | 22 | trail structure, provenance, determinism |
| `test_arithmetic.py` | 22 | subtotal + tax == total |
| `test_invalid_amount.py` | 21 | zero / negative totals |
| `test_currency.py` | 29 | pinned-rate FX approve, same-number reject, held-else |
| `test_confidence.py` | 31 | provenance (regex heuristic + LLM self-report), the gate, suggested resolution/problematic fields, one end-to-end mocked-LLM run |
| `test_inferred_po.py` | 13 | distance cap, ambiguity guard |
| `test_po_edge_cases.py` | 12 | split-PO, idempotency, reversal, concurrency |
| `test_samples.py` | 10 | the 10 samples end to end, in manifest order |
| `test_multi_po.py` | 28 | multi-PO binding, the split, the ledger, the hold |
| `test_allocations.py` | 13 | the allocation ledger, its migration and idempotence |

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
POST /api/admin/reset-demo       [invoice:admin]  clears run history only
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
| PO-1006 | Wayne Facilities | $4,000.00 | open |
| PO-1007 | Wayne Facilities | $2,500.00 | open |
| PO-1008 | Oscorp Materials | $2,160.00 | open |
| PO-1009 | LexCorp Studios | $5,000.00 | open |

All eight vendors approved (V-001 … V-008). **Wayne Facilities is the only
vendor with two POs**, for sample 07's multi-PO story. **Oscorp Materials**
(PO-1008) and **LexCorp Studios** (PO-1009) exist for samples 08/09 — the FX
conversion and the same-number-collision reject — and are otherwise untouched
by anything else. Nothing else references any of these three vendors, so the
other samples are unaffected.

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
| 8 | `07_multi_po_wayne.pdf` | $6,500 across PO-1006 + PO-1007 | NEEDS_REVIEW ‡ |
| 9 | `08_fx_match_oscorp.pdf` | €2,000 converts to exactly $2,160 (PO-1008) | APPROVED § |
| 10 | `09_currency_number_collision_lexcorp.pdf` | €5,000 vs a $5,000 PO — same digits | REJECTED § |

**Run 2 → 3 → 4 in order** or the split-PO story doesn't work. **Run 1 before 7**
or the duplicate has nothing to collide with. **8, 9 and 10 are order-independent**
— own vendors, own POs, untouched by anything else.

† Sample 05 is route-dependent by design: no key → nothing to read → refuse to
guess → NEEDS_REVIEW; with the vision route → reads INV-9004 / PO-1005 /
$15,400.00 → APPROVED. `manifest.json` carries both, resolved against
`config.has_api_key()`.

‡ **Sample 07 is order-independent and is the cleanest thing to demo.** Nothing
is wrong with it: PO-1006 ($4,000) + PO-1007 ($2,500) authorise exactly the
$6,500 billed, the vendor is approved, the arithmetic is right. It is held purely
because the document never says which PO each line belongs to. The process shows
the split it worked out and refuses to act on it alone. Accept it as `reviewer`
and watch both POs go to $0.00 — one invoice, two ledgers moved correctly.

**Sample 04 legitimately fails two rules.** Its PDF states Subtotal $8,200 + Tax
$0.00 = Total $8,150, which does not add up. Both routes read it identically, so
the arithmetic check is correct — the fixture is inconsistent. Verdict unaffected.

§ **Samples 08 and 09 are the same shape, opposite outcome, deliberately paired.**
Both bill in EUR against a USD PO. 08's €2,000.00 converts to exactly $2,160.00
at the pinned rate (`config.FX_RATES["EUR"] = 1.08`) — a genuinely different
currency landing on a genuinely matching value; approved, with the rate and
table version named in the audit trail. 09 states €5,000.00 against a $5,000.00
PO — the identical digits, not a converted equivalent (no correct conversion
produces that) — rejected outright rather than held: at the pinned rate it is
actually $5,400.00, so paying face value would silently underpay by $400. The
`extraction.MONEY` regex was extended to recognise a currency CODE
("EUR 2,000.00"), not just a symbol, so the regex fallback can read either.

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
| 2 | `Tracked[T]` provenance, per-route confidence, **confidence gate** | ✅ done |
| 3 | `rules.yaml` versioned policy + typed loader | ◨ thresholds in `config.py`; YAML to do |
| 4 | Transactions; `run_allocations` table | ✅ done |
| 5 | `DecisionTrace` + reference snapshot; stop re-seeding | ◨ audit trail done; snapshot to do |
| 6 | Line-item decomposition, multi-PO consolidation, FX provider | ◨ multi-PO done; currency mismatch resolves against a pinned rate table (config.FX_RATES); line items + a broader/live FX provider to do |
| 7 | UI: confidence badges, evidence snippets, allocation view | ◨ allocation view + confidence badges + reviewer brief done; nothing queued |

**Sequencing trap (already navigated):** multi-PO consolidation is a **ledger**
feature, not a matching feature. Phase 4's `run_allocations` table landed first,
on its own and behaviour-neutral; multi-PO matching went on top of it. Doing it
the other way round would have over-consumed every PO an invoice touched.

**Most valuable thing left:** was Phase 2's confidence gate — done, at the
user's request; see § Confidence, provenance and the confidence gate. Nothing
in the phases still open (3, 5, the rest of 6/7) changes a verdict on any of
the ten samples, so none of it blocks the case study.

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
ten samples are one page, so it has never surfaced there. Fix is one constant.

⚠️ **2. Gemini vision returns intermittent 503.** Google-side unavailability, not
quota and not our code. Observed 0/5 on one probe and working minutes later. The
fallback is correct (route `none` → NEEDS_REVIEW, nothing fabricated), but the
scanned-sample demo is not reliably reproducible, and `manifest.json` resolves
that sample's badge on key *presence* rather than provider *availability* — so
the badge can contradict the run beside it. **Decide before recording.**

⚠️ **3. `data/app.db` accumulates runs, which breaks the samples.** Not a bug —
the samples are history-dependent by design, so a second pass makes 01 a
duplicate of itself and leaves PO-1001 with $5.72. It was reported twice as
"the happy path is rejected". Now recoverable without shell access: **Reset
demo data** on Overview (admin), or `.\reset-demo.ps1`. Reset before recording.

⚠️ **5. `test_extraction_routing.py` reads the real quota counter.** It is
described as fully mocked, and the providers are — but `extraction` consults
`quota.try_consume()` against `data/app.db` before reaching the fakes, and
`conftest.py` has no DB isolation. With the local vision budget spent, 4 of its
23 cases fail. Run against a clean database for the true result. A
`storage.DB_PATH` redirect plugin is the workaround used during this work.

⚠️ **4. `extraction._first()` strips a leading minus sign** off a captured
amount, so `Total Due: -500.00` extracts as +500. Accounting parentheses are
unaffected. The amount rule cannot catch a sign the extractor discarded.

### Design gaps (deliberate, queued)

- Business rules are **constants in `config.py`**, not versioned policy. Phase 3.
- Reference data **re-seeded from JSON on every startup**, so editing
  `purchase_orders.json` silently changes what historical runs mean. Phase 5.
- A multi-PO invoice's **split is proposed, not read**, so it always needs a
  human. Deriving it would need per-line-item PO references — line-item
  decomposition, Phase 6.
- Rate-limit counters are **per process** — several workers multiply the limit.
- `_guess_vendor` picks the vendor by **line position**.
- **Sorting, filtering and paging are client-side** in the invoice register.
  `GET /api/runs` returns everything and takes no query parameters. Correct at
  this volume; past a few thousand rows it moves server-side.
- **No UI for the admin override** (`POST /api/runs/{id}/status`). Only reviewer
  accept/reject is surfaced. No bulk actions either — there is no batch endpoint.

### Case-study deliverables still outstanding

1. Settle the sample-05 badge question (issue 2).
2. Reset the demo (§4) — one click now, not a manual file deletion.
3. ✅ **Published to GitHub** — public, secrets verified absent.
4. *(Optional)* Deploy to a host, and smoke-test the *deployed* instance — the DB
   path, the env and SSE buffering all change. `GEMINI_API_KEY`, `GROQ_API_KEY`
   and `AUTH_SECRET` must be **host env vars**, never a committed `.env`. The
   GitHub link may be sufficient as "a shareable link"; confirm with the user
   before building this.
5. **Record the 5-minute demo video.** ← the main thing left.

### If you are starting a fresh session, do this first

1. Read this file, then [README.md](README.md) (current and verified).
2. `git log --oneline -10` and `git status` — confirm nothing moved.
3. `.\venv\Scripts\python.exe -m pytest tests/ -q` — expect **446 passed**.
   No key or network needed. If it is not 446, find out what changed before
   building anything. Two known non-regressions: `test_samples` 05 depends on
   Gemini being reachable (503 and 429 both happen), and
   `test_extraction_routing` fails 4 cases when the local vision quota is spent
   (§9 issue 5) — run against a clean `data/app.db` for the true number.
4. `cd frontend-next && npm run build` if the UI was touched. The export in
   `out/` is what FastAPI serves; without a rebuild the browser keeps serving
   the previous one.
5. **Ask the user what they want next.** Do not start Phases 2–7, and do not fix
   the open issues in §9, without being asked (§3).

The single highest-value next task is **recording the demo video**. Reset the
demo first (§4), and settle the sample-05 badge question (§9 issue 2).

---

## 10. Decisions already made — don't relitigate

| Decision | Why |
|---|---|
| **PS-1** over PS-2/PS-3 | Only PS where inputs are real artefacts and the decision is verifiable. |
| Rules deterministic, LLM extraction-only | Auditability. The headline claim; it survived the audit. |
| Three verdicts, not two | Binary forces guessing on ambiguous invoices; the middle state is where automation hands back to a human. |
| Tolerance one-sided | Over-billing is a problem; under-billing is a normal partial. |
| Balance derived from run history, not a stored counter | No counter can drift from what was actually approved, and it makes idempotency and reversal structural. Reaffirmed when a `remaining_amount` column was proposed, and again when `run_allocations` landed — an allocation is an immutable fact about a run, not a balance; the status join still decides whether it counts. |
| Multi-PO invoices always held, never auto-approved | The document states no split, so the division is computed. Approving would commit money in amounts no document and no person specified — the inferred-PO objection applied to the division rather than the binding. |
| The proposed split is still computed, stored and shown | Refusing to divide at all would hand the reviewer arithmetic instead of a decision. They confirm figures; they do not work them out. |
| Excess beyond every balance lands on the last PO | The allocations must sum to the invoice total or the ledger describes money nobody billed. The overage stays visible as that PO being over-consumed, and the combined tolerance check reports it. |
| Cascade skips multi-PO runs | They are held on the unstated split, not a short balance. Freeing budget elsewhere says nothing about it, and releasing them would commit a split nobody confirmed. |
| Only APPROVED runs consume budget | A flagged invoice mustn't block the queue behind it. |
| Refuse to guess when unreadable | Empty fields → review, rather than fabricating. `vendor_check` is tri-state: not-on-list (reject) ≠ couldn't-read-a-name (review). |
| pypdfium2 over pytesseract | Self-contained wheel; no system binaries for a reviewer to install. |
| No rule engine (JSON-logic etc.) | One-sided tolerance and ledger-derived balances express badly in a DSL; a sign error in exactly that comparison has been a bug twice. YAML for policy, Python for predicates. |
| ~~FX conversion must not widen auto-approval~~ **REVERSED, at the user's explicit request** | Was: a verdict depending on a rate fetched at run time is not reproducible by an auditor. Now: conversion is allowed against a **pinned, versioned** rate table (`config.FX_RATES`) — the objection was about *when* the rate is fetched, not conversion itself, and a pinned table is exactly as reproducible as a pinned model. See § Currency mismatch and FX conversion. |
| Same raw number, different currency → REJECTED, not held | No correct conversion produces identical digits in a different currency, so it isn't an ordinary discrepancy for a human to reconcile — it reads as a currency-code error, and paying face value would silently mis-pay by the full FX difference. The one place a currency finding rejects rather than holds. |
| Pydantic `Field()` rejected for confidence | Class-level schema metadata; confidence is per-instance data. Built as `ExtractedInvoice.provenance: dict`, not a literal `Tracked[T]` generic wrapper — same principle (provenance travels beside the value, not baked into the type used for arithmetic), lighter-weight implementation. |
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
| **UI is a static export served by FastAPI**, not a Node server | Keeps the UI same-origin with the API (no CORS, no base URL), one process, one port. Nothing Node runs at serve time. |
| Client-side sections, not real routes | Deep links would need the static mount to resolve them — a backend change for no gain across four sections. |
| `frontend/` kept after the rewrite | It is the no-npm fallback. A clone with no Node still boots a working UI. |
| HTML shell served `no-store` | The shell names which hashed bundle to load, so a cached copy pins the browser to a build that no longer exists. Cost two debugging sessions before it was fixed. |
| Non-invoice detection uses **extraction output, not keywords** | Searching for the word "invoice" misses other languages and fires on any document that merely discusses invoicing — including this project's own brief. A model that reads a page and finds no field is the classifier. |
| Not-an-invoice **rejects**, unreadable **holds** | A hold means "a human must decide whether to pay this"; there is nothing to decide about a CV. But an empty result from a failed extractor is evidence about the extractor, so degraded routes never hard-reject. |
| Demo reset is an endpoint, not just a script | It was reported twice as a bug. Recovery should not require deleting a file on the server. Admin-scoped, deletes runs only. |
| ~~No per-field confidence shown~~ **REVERSED, at the user's explicit request** | Was: the pipeline did not produce one, and rendering an invented percentage would be fabricating evidence. Now: it is a genuine per-instance signal (model self-report, or a regex heuristic) computed by extraction and stored — see § Confidence, provenance and the confidence gate. |

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
14. **Frontend data hooks fired before sign-in**, so both fetches 401'd, the
    error was cached, and the 401 handler tried to sign out the user who had
    just signed in. Gated on the token existing.
15. **`byDay()` bucketed runs by UTC** while building its axis from local dates,
    so east of Greenwich every run fell outside the window and the volume chart
    read "no activity" with data present.
16. **`is_not_an_invoice()` treated `extracted=None` as "not an invoice"** —
    absence of evidence read as evidence of absence. `decide()` is called
    without it on several paths, so valid invoices would have been hard-rejected.
    Caught immediately by `test_security` and `test_extraction_routing`.
17. **`PageHeader` used negative margins inside an unpadded `<main>`**, pushing
    the page wider than the viewport and adding a horizontal scrollbar on
    tablet and mobile.
18. **Both frontends showed an invoice's own total/subtotal/tax with a
    hardcoded `$`**, regardless of the invoice's actual currency —
    `ExtractedFields`, `VerdictHeader`, the invoices register, and the vanilla
    UI's equivalents all used `money()` (bare `$`) instead of `amount(v,
    currency)`. Invisible while every sample was USD; surfaced immediately by
    the first non-USD sample. PO-side ledger figures (consumed/remaining/
    authorised) were left as `money()` — legitimately correct since every seed
    PO is USD, not a currency bug.
19. **`Variance` and `Tolerance applied` in the audit trail were labelled with
    the invoice's currency, not the PO's** — both are computed against
    `remaining_before`, which is always PO-currency. Invisible until FX
    conversion made a clean APPROVED run on a non-USD invoice possible.
20. **The vanilla UI's confidence badge had its CSS class names backwards** —
    `provBadge()` emitted `prov-ok`/`prov-warn`/`prov-bad` but the CSS
    selectors were `.prov-badge.ok` etc. (bare tone names, not `prov-`
    prefixed). Caught by extracting the function and running it against real
    data outside the browser, not by eyeballing the diff — the badges would
    have silently rendered with no colour at all.

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
