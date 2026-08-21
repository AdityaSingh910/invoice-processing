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
| UI | ✅ **Next.js 15 + React 19 + Tailwind v4**, 4 sections, redesigned as a light-first enterprise finance interface with an explicit (not OS-linked) dark-mode toggle |
| Extraction | ✅ **Groq** for text PDFs, **Gemini Vision** for scans |
| Automated tests | ✅ **480 passing** deterministically, 19 files, both providers mocked |
| Audit trail | ✅ Structured, deterministic, emitted by the rule engine itself |
| Human review | ✅ Accept/reject, recorded beside the automated decision |
| API security | ✅ OAuth2 bearer tokens, scopes, rate limits, input validation |
| Production safety | ✅ `APP_ENV=production` refuses demo creds / missing secret |
| Daily AI budget | ✅ Per-provider circuit breaker, PostgreSQL-backed |
| Database | ✅ **Migrated SQLite → PostgreSQL** (2026-08-21) — `DATABASE_URL`, same schema shape, row-level locking replaces SQLite's whole-file lock |
| Document storage | ✅ **Phase C** (2026-08-21) — uploaded PDFs persist past the run; metadata in Postgres, bytes behind a local/S3 `DocumentStore` abstraction |
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

**Git:** 50 commits on `main` (this session's Phase C commit included),
**ahead of `origin/main` by 3 — not yet pushed** (verified via `git status`;
the previous session's Postgres-migration commit was already unpushed when
this session started, and that is still accurate, not a regression). Push
only if the user asks. **Working tree still has uncommitted changes** —
the frontend redesign + dark-mode toggle + `DocumentPreview.tsx` +
`ReviewWorkspace.tsx`, exactly as it was at the start of this session; Phase C
did not touch, commit, or discard any of it. Recent (before this session's
commit):

```
147c0ce Migrate persistence from SQLite to PostgreSQL
cba2f01 Bring README and CLAUDE.md up to date with the frontend redesign
2a8f5c7 Add an explicit dark-mode toggle to the sidebar
859dca9 Redesign the frontend as a light-first enterprise finance interface
2a1d56c Let "Open review queue" actually open a filtered queue
0e75cb0 Show confidence/provenance in the UI and add a reviewer brief
0c519a7 Add per-field confidence/provenance and wire it into the decision (Phase 2)
9c0efd0 Resolve currency mismatch against a pinned FX rate, reject a disguised one
81a086d Charge each purchase order its own share of a multi-PO invoice
dea9163 Record which PO each run charged, so one invoice can span several
fbe479f Bring README and CLAUDE.md up to date with the current build
824d45b Recognise documents that are not invoices, and reject them saying so
8da60b7 Let an admin clear run history from the UI, and explain duplicate rejections
634bdc7 Add a one-command reset so the samples keep telling their intended story
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

## 3a. Deployment-prep initiative — a SEPARATE phase table, do not conflate with §8

Started 2026-08-21, on explicit request: turn this from a case-study demo into
a deployable, collaborative, multi-user AP platform. Its own phase lettering
(A–L) is deliberately distinct from the case-study's Phase 2–7 numbering above
— they are two different bodies of work, tracked separately, and "Phase B" in
this context never means Phase 2 above.

| Phase | Work | State |
|---|---|---|
| A | Understand the current architecture before changing anything | ✅ done |
| B | SQLite → PostgreSQL migration | ✅ **done and verified** — see §Stack. 447/447 tests pass against real Postgres; the live server was started against it, all 10 samples replayed through the real HTTP pipeline with correct verdicts, the PO ledger derivation checked against documented figures, a human review + cascade exercised end to end (multi-PO accept → both POs to $0.00), and auth boundaries (401/403/200) checked against the live server, not just TestClient. |
| C | Persistent invoice PDF storage | ✅ **done and verified** (2026-08-21) — see § Document storage (Phase C). New `documents` table (metadata only) + `backend/documents.py` (`DocumentStore` abstraction: `LocalDocumentStore` default, `S3DocumentStore` lazy-imports boto3). 33 new tests plus the full 480-test suite pass; the live server was started against the real Postgres instance and a real upload/download/hash round-trip was verified over HTTP (not just TestClient) — see the verification note below the table. |
| D | Collaborative multi-user activity/history | ⬜ not started |
| E | Review claim/locking system | ⬜ not started |
| F | Analytics/KPIs backend | ⬜ not started |
| G | Filtering/grouping/export | ⬜ not started |
| H | Client-facing authorization/data APIs | ⬜ not started |
| I | Read-only Invoice/AP Assistant (chatbot) | ⬜ not started |
| J | Email ingestion abstraction | ⬜ not started |
| K | Multilingual backend support | ⬜ not started |
| L | Deployment/security review | ⬜ not started |

**Stopped after Phase C on explicit instruction** ("STOP after Phase C. Do NOT
proceed to Phase D until I explicitly tell you to continue"). Phase B was the
same pattern one phase earlier. Do not start Phase D or later without being
asked, for the same reason §3 above applies to the case-study phases: this is
deliberately incremental, one verified phase at a time.

**Phase C verification note.** Beyond the automated suite: the live server
was started against the real Postgres instance (`uvicorn` on a scratch port,
not through `start.ps1` — see the gotcha in §4 about tool-launched servers not
surviving), a real invoice was uploaded as `analyst`, and `GET
.../document/download` was fetched and compared byte-for-byte
(`sha256sum`) against the original file on disk — identical. Unauthorized
access (no token) returned 401 on both the metadata and download endpoints. A
crafted filename of `../../../../etc/passwd.pdf` was uploaded and confirmed
stored as `passwd.pdf` (the existing `_safe_filename()` sanitizer, reused
unchanged, already neutralizes this — Phase C did not need its own copy of
that logic). Invalid file type returned 415; a byte-capped oversized upload
returned 413; neither created a run or a document row. A found-and-fixed
issue during this verification, not shipped: the first full-suite run leaked
43 real PDF files into `data/documents/` because most test files' fixtures
isolate the Postgres schema but had no reason to know document *content* also
needed isolating — fixed with one `autouse` fixture in `tests/conftest.py`
(`_isolate_document_storage`) that redirects `config.DOCUMENT_STORAGE_DIR` to
a per-test `tmp_path` for every test in the suite, confirmed by re-running the
full suite and observing zero new files on disk afterward.

Environment needed for local development now: PostgreSQL reachable via
`DATABASE_URL` (see §4, §Stack) — unchanged since Phase B. Phase C's default
document-storage backend (`local`, writing under `data/documents/`) needs
nothing additional installed or configured; `DOCUMENT_STORE_BACKEND=s3` is
available for later but requires `boto3` (not installed by default) and
`DOCUMENT_S3_BUCKET`.

---

## 4. Running it

**Requires PostgreSQL first** — `DATABASE_URL` in `.env`, pointed at a
reachable instance. Two ways to get one:

```powershell
docker-compose up -d          # local instance matching .env.example, if Docker is available
```

or install PostgreSQL directly (what this machine actually has, since Docker
was not available when this was set up): `winget install
PostgreSQL.PostgreSQL.16`, then create a dedicated app role/database rather
than using the `postgres` superuser directly — see §Stack for exactly what
this project's own local instance looks like.

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

**The database is now PostgreSQL, not a SQLite file.** `data/app.db` /
`data/app.db.bak` are vestigial — no code reads or writes them any more, and
`.\reset-demo.ps1` no longer needs to stop the server first (Postgres has no
file lock to fight, unlike SQLite). `DATABASE_URL` (in `.env`, gitignored)
points at a local Postgres instance; see §Architecture > Stack for how it got
there and `scripts/migrate_sqlite_to_postgres.py` if an old `data/app.db`
still has run history worth carrying over.

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

### Document storage (Phase C)

**Built at explicit request**, as the first step of the deployment-prep
initiative's own phase table (§3a), not the case-study phases in §8.

The uploaded PDF now survives the run that processed it. The database
(`storage.py`'s new `documents` table) holds **metadata only** — original
filename, MIME type, size, a SHA-256 hash, `uploaded_by`, `uploaded_at`,
`source`, and an opaque `storage_key` — never the PDF bytes. The bytes live
behind `backend/documents.py`'s `DocumentStore` interface, so nothing else in
the application (main.py's pipeline, storage.py's row) knows or cares which
backend is active:

- `LocalDocumentStore` (default, `DOCUMENT_STORE_BACKEND=local`) — files under
  `config.DOCUMENT_STORAGE_DIR` (`data/documents/`, gitignored). Writes
  atomically (temp file + `os.replace`), so a reader can never observe a
  partially-written document.
- `S3DocumentStore` (`DOCUMENT_STORE_BACKEND=s3`) — an S3-compatible bucket,
  for a deployment with no shared local disk between instances (several
  workers, ephemeral containers). `boto3` is imported **lazily**, inside the
  constructor, so a local-only install never needs the package — it is
  commented out in `requirements.txt` for that reason, same principle as the
  provider SDKs in `extraction.py` being optional.

**The storage key is never the original filename, sanitised or not.** It is
always `new_storage_key()` — a UUID4 the server generates, never anything the
caller sent. `LocalDocumentStore._path()` additionally refuses any key that
does not match the fixed shape (`^[0-9a-f]{32}\.pdf$`) and re-checks the
resolved path sits inside the storage root before touching disk — belt and
braces on top of a key that HTTP can never actually submit malformed in the
first place, because a corrupted or hand-edited database row should still be
refused rather than trusted. The original filename is kept purely as display
metadata, and it is already the sanitised name `main.py`'s existing
`_safe_filename()` computed at upload time (bug #12's fix, reused unchanged)
— the raw client-supplied name never reaches storage at all.

**Two new endpoints, both `invoice:read`** — a document is invoice data, not
a separately-permissioned resource:

- `GET /api/runs/{id}/document` — metadata. Deliberately never returns
  `storage_backend` or `storage_key`: where the file physically lives is
  nobody's business outside this process.
- `GET /api/runs/{id}/document/download` — the real bytes. `?inline=1` sets
  `Content-Disposition: inline` for an embedded viewer instead of the default
  `attachment`. Authorization is checked by the `Security(...)` dependency
  before the handler body runs at all, so an unauthorised caller learns
  nothing about whether a document even exists for that run — the 401/403 is
  identical whether or not `run_id` is valid.

**Persisting a document is never allowed to fail the run it belongs to.**
`_persist_document()` in `main.py` wraps the save (content write +
`storage.save_document()`) in a try/except that only logs — by the time it
runs, the automated decision is already made and, on the success path,
already committed to `runs`. A storage-layer problem (a full disk, an
unreachable bucket) must not turn a completed, correctly-decided run into a
pipeline error the operator has to re-run. Same fail-safe posture as the
daily quota breaker (§ API security), applied to a different resource. An
unreadable-document run (`_abort_unreadable`) persists its document too — a
reviewer routing it for manual handling still needs the original file.

**`source` is `MANUAL_UPLOAD` today.** `config.DOCUMENT_SOURCES =
("MANUAL_UPLOAD", "EMAIL")` recognises `EMAIL` now so the schema and the
`DocumentStore` abstraction do not need to change shape when Phase J's
ingestion path exists — nothing in Phase C writes that value yet, and
`storage.save_document()` rejects anything outside this tuple rather than
trusting the caller.

**`POST /api/admin/reset-demo`** (and `.\reset-demo.ps1`) now also clears
document rows and their backing files, in the same write transaction as the
runs they belong to (rows) plus a best-effort, non-fatal cleanup pass after
commit (files) — otherwise a reset would leave the samples' PDFs
accumulating on disk forever with no database row left to reference them,
the same accumulation problem §9 issue 3 already describes for `runs` itself.

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
  existing safe fallback. Counter lives in Postgres so it survives a restart.
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

FastAPI + **PostgreSQL** + **Next.js 15 / React 19 / Tailwind v4 / TypeScript**.
`POST /api/runs/stream` streams stages over SSE, read with `fetch()` and a
`ReadableStream` reader. `pyjwt` for tokens; PBKDF2-HMAC-SHA256 password hashing
from the stdlib.

**Migrated from SQLite to PostgreSQL** (2026-08-21), to prepare for real
deployment and concurrent multi-user access. `psycopg2-binary`, no ORM — same
philosophy as before (raw parameterised SQL, `storage.py` as the one place
that touches it). `DATABASE_URL` (env var, read at call time like every other
secret in this project) is required in every environment; there is no SQLite
fallback anywhere, dev included. `docker-compose up -d` gives a matching local
instance; the actual dev machine this was built on had no Docker, so the
first local instance was a `winget install PostgreSQL.PostgreSQL.16` service
instead — either works, `DATABASE_URL` is all that matters.

What changed and what didn't: table names, column names, and every business
rule are byte-for-byte the same — this was a dialect and connection-management
migration, not a schema redesign (`?`→`%s`, `AUTOINCREMENT`→`SERIAL`,
`PRAGMA table_info`→`information_schema.columns`, `RealDictCursor` in place of
`sqlite3.Row`). The one thing that's genuinely different, not just translated,
is locking: SQLite's `BEGIN IMMEDIATE` took a lock over the *whole database*
for every ledger write; Postgres uses `SELECT ... FOR UPDATE` on just the
specific `purchase_orders` row(s) an invoice charges, inside `save_run_checked`.
Same safety guarantee (two invoices racing the same PO still serialise
correctly — proved by `test_concurrent_invoices_cannot_overspend_a_po`, 8
threads racing a $10,000 PO, still resolves to exactly 5 approved / 3 held),
strictly better concurrency (invoices against *different* POs no longer block
each other at all, which one SQLite file could never offer). `quota.py`'s
`try_consume()` got the same treatment, for the same reason — its own
read-then-increment needed a row lock once the database-wide lock could no
longer be relied on to serialise it for free.

Connection pooling: `storage.get_conn()` returns a `_PooledConnection` (thin
proxy around a `psycopg2.pool.ThreadedConnectionPool` member) whose `.close()`
returns the connection to the pool instead of tearing down the socket —
psycopg2's C-level connection type refuses `conn.close = ...` directly
(`AttributeError: attribute 'close' is read-only`), which is the whole reason
the proxy exists. Every one of storage.py's ~30 `conn = get_conn(); ...;
conn.close()` call sites needed zero changes.

Test isolation: every `db(tmp_path, monkeypatch)` fixture that used to
monkeypatch `storage.DB_PATH` to a fresh SQLite file now monkeypatches
`storage.PG_SCHEMA` to a fresh, uniquely-named Postgres schema
(`tests/pg_schema.py`), created and dropped per test against one shared
database. Same isolation guarantee, same fixture shape, twelve files' worth of
one-line-per-fixture changes rather than a rewrite.

`scripts/migrate_sqlite_to_postgres.py` is a one-time, idempotent import for
carrying `runs`/`run_allocations` history out of an old `data/app.db` — POs
and vendors are never migrated this way, since they reload from `data/*.json`
on every startup regardless of which database engine is under them.

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

### Visual design system and dark mode

**The frontend was redesigned as a light-first enterprise finance interface**
— warm-neutral white/off-white surfaces distinguished by elevation rather than
borders, a single interactive accent, exactly three semantic colours
(approved/held/rejected) that mean something and never decorate, radii capped
at 10px (larger corners read as consumer software, not a finance tool), IBM
Plex Sans for UI text and IBM Plex Mono with tabular numerals for every ledger
figure. Every colour in the app flows through CSS custom properties in
`globals.css`; there are exactly two literal Tailwind colour classes anywhere
in `frontend-next` (`bg-black`, both modal/drawer backdrop scrims) — verified
by grep, not assumed.

**This landed in two separate commits deliberately.** The redesign itself
(`globals.css`, `layout.tsx`'s font setup, `AppShell.tsx`'s nav restructure)
was already sitting uncommitted in the working tree from an earlier session
before this one started — it was committed on its own first, then the
dark-mode toggle (a distinct, later request) went on top as its own commit.
Splitting a mixed working tree like that is done by reverting just the new
edits with the editor, committing what remains, then reapplying — there is no
interactive `git add -p` available in this environment.

**Dark mode is an explicit toggle, never `prefers-color-scheme`.** `html {
color-scheme: light }` is fixed; a `:root[data-theme="dark"]` block in
`globals.css` overrides every token with a restrained dark palette (same
one-accent, three-semantic-colour discipline as light — no neon, no glow).
Because the whole app already read through those custom properties, the
override cascades through nav, tables, badges and charts with zero
component-level `dark:` variants needed. `lib/theme.tsx` provides
`ThemeProvider`/`useTheme`, toggled from a sun/moon button in the sidebar's
account row (shared by the desktop rail and the mobile drawer). Persisted to
`localStorage` (`ip-theme`) and applied via a `next/script` `beforeInteractive`
bootstrap in `layout.tsx`, so a returning dark-mode user never sees a flash of
the light theme.

**A wasted-afternoon lesson from getting here:** the user's own screenshots of
the running app showed a fully dark, boxy, "hacker tool" UI and asked for a
"complete visual overhaul." The served CSS, fetched directly with `curl`,
said otherwise (`--canvas:#f8f8f6`, light). A Playwright screenshot with zero
browser extensions confirmed the real app was already close to the target —
what the user saw was almost certainly a force-dark browser extension or
Chrome's "Auto Dark Mode for Web Contents" repainting the page client-side,
which a page's own CSS cannot prevent once the browser decides to do it. **If
a user's screenshot of this app looks wrong in a way the code doesn't explain,
get an extension-free render (`playwright` in the venv, headless, no profile)
before trusting the screenshot** — it is cheap and it settles the question
completely instead of guessing.

---

## 6. Files and Architecture

### Backend (`backend/` — Python + FastAPI)

The backend is a single-process FastAPI app that:
1. Streams the 9-stage pipeline over SSE to the browser
2. Enforces OAuth2 auth, scopes, rate limits
3. Holds the deterministic decision engine (no model output in verdicts)
4. Manages a PostgreSQL ledger that derives balances from run history

**Core modules:**

| Module | Role |
|---|---|
| `main.py` | FastAPI app, async pipeline generator, SSE streaming, auth endpoints, error handlers. `POST /api/runs/stream` is the live run view. |
| `extraction.py` | PDF → structured fields. Routes by document type (text vs scanned). Groq (text) → Gemini (vision) → regex → empty (none). **The ONLY module calling a model.** Captures confidence + evidence per field. Both provider SDKs imported lazily. |
| `rules.py` | Deterministic decision engine: `decide(extracted, po_match, ...) → (verdict, reasons, audit_trail)`. Emits audit as it evaluates, never a second pass. Handles vendor tri-state, is_not_an_invoice(), duplicates, confidence gate. No model, no approximation. |
| `matching.py` | PO lookup (exact + fuzzy). Tolerance (one-sided). Multi-PO binding + `split_across()` ledger math. Currency detection + `fx_convert()` at pinned rates. All deterministic. |
| `storage.py` | PostgreSQL schema, seed data load, ledger queries, write transactions (`SELECT ... FOR UPDATE` on the specific PO row(s) for race safety), human review recording, run clearing, `run_allocations` migration, document metadata (`save_document`, `get_document_for_run`). Balances derived per run by summing APPROVED allocations. Pooled connections via `psycopg2.pool`. |
| `documents.py` | Document **content** storage abstraction (Phase C): `DocumentStore` interface, `LocalDocumentStore` (default, local disk), `S3DocumentStore` (boto3 imported lazily). Never holds metadata — that's `storage.py`'s `documents` table. Storage keys are always server-generated (`new_storage_key()`), never the original filename. |
| `auth.py` | OAuth2 resource-server: JWT validation (pyjwt), scopes, password grant, production mode enforcement (no demo creds, no missing secret). |
| `config.py` | .env loader, `APP_ENV` switch, provider model IDs, business thresholds (PO_TOLERANCE_PERCENT, CONFIDENCE_THRESHOLD, DAILY_QUOTA_VISION, FX_RATES + version), document-storage settings (DOCUMENT_STORE_BACKEND, DOCUMENT_STORAGE_DIR, DOCUMENT_S3_*). |
| `quota.py` | Per-provider daily extraction budget, PostgreSQL-backed counter (`extraction_quota` table). Fails open (cost guard, not security). |
| `ratelimit.py` | Sliding-window per user/IP (per-process, not shared across workers). |
| `schemas.py` | Pydantic dataclasses: `ExtractedInvoice` (fields + provenance dict), `LineItem`, `StageLog`, `RunResult`. |

**Database schema** (PostgreSQL, `DATABASE_URL`) — corrected here against the
actual code in `storage.py`/`quota.py`, not assumed; the previous version of
this table had drifted from reality in several places (wrong PK, wrong column
names, two tables that do not exist) before this pass:

```
purchase_orders:      po_number (PK), vendor, amount, currency, issued_date,
                      status, description, source_file, source_row

vendors:               vendor_name (PK), vendor_id, status
                      (this table is named `vendors`, not `approved_vendors`)

runs:                  id (PK, SERIAL), filename, status, created_at,
                      vendor_name, invoice_number, total, po_number,
                      extracted_json, po_match_json, stages_json,
                      reasons_json, audit_json, automated_decision,
                      human_decision, final_decision, reviewed_by,
                      reviewed_at, review_note

run_allocations:       id (PK, SERIAL), run_id (FK -> runs.id), po_number,
                      amount, seq
                      (immutable facts; balance derived at read time by
                      joining to runs.status='APPROVED')

extraction_quota:      day, provider, used (PK is (day, provider))
                      (this table is named `extraction_quota`, not
                      `daily_quota`, and lives in quota.py not storage.py)

documents:             id (PK, SERIAL), run_id (FK -> runs.id),
                      original_filename, mime_type, size_bytes, sha256,
                      uploaded_by, uploaded_at, source, storage_backend,
                      storage_key
                      (Phase C. Metadata only -- the PDF bytes live behind
                      documents.py's DocumentStore, keyed by storage_key,
                      never in this table and never named `path`)
```

**Not database tables, despite looking like they should be:** users live in
`data/users.json`, read directly by `auth.py` on every call (`load_users()`);
there is no `users` table and never has been. There is also no
`run_stage_logs` table — a run's stage-by-stage log is the `stages_json`
column on `runs` itself, one JSON array per run.

---

### Frontend (`frontend-next/` — Next.js 15 + React 19 + Tailwind v4 + TypeScript)

**Architecture:** built as a **static export** (`npm run build` → `frontend-next/out/`), served as plain HTML/JS by FastAPI. No Node process at runtime. `next dev` on :3000 proxies `/api` to :8000 for development.

**Design system** is **light-first enterprise finance interface**:
- Warm-neutral surfaces (white/off-white) distinguished by elevation, not borders
- One accent colour for interaction
- Three semantic tones (approved/held/rejected) that carry meaning
- Radii capped at 10px (larger corners read as consumer not finance)
- IBM Plex Sans for UI text, IBM Plex Mono (tabular numerals) for all ledger figures
- Every colour flows through CSS custom properties in `globals.css`
- Dark mode is an explicit toggle (`:root[data-theme="dark"]`), persisted to localStorage, never `prefers-color-scheme`

**Layout and pages:**

| File | Purpose |
|---|---|
| `app/layout.tsx` | Root layout. IBM Plex fonts (next/font). Theme bootstrap script runs before interactive, so dark-mode users never see light flash. ThemeProvider. |
| `app/page.tsx` | Single client-rendered page. Auth gate (show login if no token). If authenticated, render AppShell + section switcher (Process / Invoices / Reference / Overview). Client-side state, no real routes — deep links would need backend support for no gain. |
| `app/globals.css` | ~450 lines. Complete design system: 30+ CSS custom properties (colour tokens, type scale, spacing, shadows). Light palette as bare `:root`, dark palette in `@media (prefers-color-scheme: dark)` + `[data-theme="dark"]` guards so both light/dark win. Utility classes: `.panel`, `.rise`, `.tnum` (tabular numerals). |

**Page components** (`components/pages/`):

| Component | Purpose |
|---|---|
| `OverviewPage` | Dashboard: 9 KPI cards (count/amount/rate for approved/review/rejected, PO consumption, daily AI budget). Volume over time (hand-drawn SVG, no chart library). Reason distribution pie. Reset demo button (admin only). |
| `ProcessPage` | Upload + live run view. Before-run state: file drop zone, sample-invoice picker, 10 MB limit (client + server). After run: phase stepper, document preview, extracted data, audit trail, decision panel inline. File held in-memory so user sees the document they processed. |
| `InvoicesPage` | Run register. Fetches `GET /api/runs` once, then filters/sorts/pages client-side (no params). Filter by status + EXCEPTIONS (unreviewed NEEDS_REVIEW). Sort by created_at/total/vendor/status. Click a row to open ReviewWorkspace overlay. Pre-filter supported (e.g. Overview "Open review queue" lands here with filter set). |
| `ReferencePage` | Read-only: POs + approved vendors (both lists, no edit UI). |

**Invoice components** (`components/invoice/`):

| Component | Purpose |
|---|---|
| `StageList` | Render the 9-stage log with completion %, execution time, outcomes. `PhaseStepper`: progress indicator with labels. |
| `PoMatchPanel` | Three-way match table (invoice amount vs PO balance vs tolerance). `PoBudget` component: the balance bar (consumed / this run / remaining, overflow hatched red). |
| `Panels` | Verdict banner + extraction summary + extracted fields table (confidence badges) + reasons list. `ReviewerBrief`: combined from audit.reason + audit.problematic_fields + audit.suggested_resolution, never generated. Confidence badges inline on every field, coloured by whether the RULE ENGINE flagged it. |
| `AuditTrail` | Render audit.* as structured record: decision, reason, PO match + source, values compared, variance, tolerance, rules pass/fail list. |
| `ReviewBar` | Accept/Reject buttons + confirm dialogs. Populated from ReviewerBrief. Only shown on NEEDS_REVIEW. Posts to `/api/runs/{id}/review`. |
| `DocumentPreview` | Render the original PDF. Priority: in-memory File (if this is the run just processed in this tab) → fetched from `/api/sample-invoices/{name}` (if it's a sample) → empty state (historical non-sample runs not persisted). Uses browser native PDF viewer (`<object>`), not a library. Caches sample index in-memory per session. |
| `ReviewWorkspace` | Two-pane layout component. Document at left (sticky on desktop), decision evidence at right. Exports `ReviewWorkspaceBody` (the layout, used inline on ProcessPage) and `ReviewWorkspace` (full-screen overlay opened from InvoicesPage). Overlay: `role=dialog`, `aria-modal`, Escape closes, `body.overflow: hidden`. Supports Previous/Next nav when opened from a list. |

**UI primitives** (`components/ui/`):

- `Button`, `Badge`, `StatusBadge`, `Panel`, `PanelHeader`, `Modal` (focus trap), `SearchInput`, `Select`, `Segmented`, `Tooltip`, `Callout`, `EmptyState`, `ErrorState`, `SkeletonRows`, `Spinner`
- All respond to `--tone` and `--state` CSS custom properties
- Icons: 16px stroke-width=1.5 SVG set (50+), incl. sun/moon for theme toggle. No icon library.
- `Toast.tsx`: floating notifications (context + hook), sticky-top. `Modal.tsx`: dialog container with focus trap.

**Layout components** (`components/layout/`):

- `AppShell.tsx`: page chrome, responsive sidebar (desktop rail) / mobile drawer. Dark-mode toggle (sun/moon button) in account row. Exports `PageHeader` / `PageBody`.

**Other:**

- `ResetDemoButton.tsx`: calls `POST /api/admin/reset-demo`, shows confirm dialog, updates runs on success.
- `charts.tsx`: hand-drawn SVG charts (volume-by-day, reason distribution pie). No chart library.

**Utility libraries** (`lib/`):

| File | Purpose |
|---|---|
| `api.ts` | Fetch wrapper with auth header + error handling. `streamRun()` reads `POST /api/runs/stream` SSE response with ReadableStream reader. `apiFetch` / `apiJson` for regular requests. |
| `auth.tsx` | `AuthContext` + `useAuth` hook. Login form. Token in localStorage. |
| `theme.tsx` | `ThemeProvider` + `useTheme` hook. Reads/writes localStorage (`ip-theme`), syncs to `html[data-theme]`. |
| `useData.ts` | Hook: fetch data once on mount, cache in state, return `{data, loading, error}`. |
| `metrics.ts` | Compute dashboard KPIs from runs list. |
| `format.ts` | Format utilities: `money(n)`, `amount(n, currency)`, `when(date)`, `percent(n)`, `confidence(score)`. `STAGE_ORDER` array. |
| `types.ts` | TypeScript interfaces: `RunRecord`, `Extracted`, `PoMatch`, `Audit`, `Reason`, `Stage`, `Verdict`, `SampleInvoice`, etc. |

---

### Other directories

**`frontend/`** — The original vanilla frontend (HTML + vanilla JS, no build, no Node). Kept deliberately as a fallback: if `frontend-next/out/` does not exist, `main.py` serves this instead. A clone without npm still boots a working UI.

**`data/`** — Seed data, reloaded into Postgres on every startup:
- `purchase_orders.json`, `approved_vendors.json`, `users.json` — tracked in git, reloaded on startup
- `app.db` / `app.db.bak` — **vestigial**, the old SQLite runtime database from before the Postgres migration. Not tracked, not read by any code any more. Safe to delete; kept only because `scripts/migrate_sqlite_to_postgres.py` can still import history out of one if it exists.
- `documents/` — **Phase C**, gitignored. Uploaded invoice PDFs under the local `DocumentStore` backend, named by their server-generated storage key (never the original filename). Runtime state, not seed data; cleared along with run history by `reset-demo.ps1` / `POST /api/admin/reset-demo`.

**`sample_invoices/`** — Test fixtures:
- 10 PDFs, order-dependent by design
- `generate_invoices.py` — regenerate with `python generate_invoices.py`
- `manifest.json` — scenario descriptions, expected verdicts, route-dependent expectations (sample 05 has two by Gemini availability)

**`scripts/`** — Operational:
- `replay_samples.py` — drives all samples through `/api/runs/stream`, verifies verdicts. Used by `reset-demo.ps1 -Replay`.
- `migrate_sqlite_to_postgres.py` — one-time, idempotent import of `runs`/`run_allocations` history from an old `data/app.db` into the live Postgres database. Does not touch POs/vendors (those always reload from `data/*.json`).

**`docker-compose.yml`** — a local PostgreSQL instance matching `.env.example`'s `DATABASE_URL`, for a clone that would rather not install Postgres system-wide. `docker-compose up -d` and go. Not used in production; a real deployment points `DATABASE_URL` at a managed instance.

**`reset-demo.ps1`** — clears run history (and optionally replays samples), by calling `storage.clear_run_history()` directly — the same function `POST /api/admin/reset-demo` calls, so the script and the endpoint can never disagree about what "reset" means. Callable from terminal or from UI (admin). No longer needs to stop the server first (that was a SQLite file-lock workaround; Postgres has no equivalent). Since Phase C, also clears document rows and their backing files, in the same transaction as the runs they belong to.

**`tests/`** — 19 files, 480 tests:
- Both Groq and Gemini mocked at HTTP transport boundary, so suite needs no key, no network, no quota
- `conftest.py` provides `auth_headers(role)` as a plain function (not a pytest fixture — callers import and call it). Also defines an **autouse** fixture, `_isolate_document_storage` (Phase C) — every test in the suite gets `config.DOCUMENT_STORAGE_DIR` redirected to a per-test `tmp_path`, the same isolation guarantee `pg_schema.py` gives the database, applied automatically to every test file without each one needing to know document content exists. Found the hard way: before this fixture existed, a full suite run wrote 43 real PDF files under the actual `data/documents/` with no surviving database row to reference them, because every other test file's `db` fixture only knew to isolate the Postgres schema.
- `pg_schema.py` — shared helper for per-test Postgres isolation: a fresh, uniquely-named schema per test (`fresh_schema()`), dropped on teardown (`drop_schema()`). Every `db(...)` fixture across the suite calls this instead of monkeypatching a SQLite file path.
- Real, isolated database state per test. Exceptions: `test_samples.py` honours a live key and exercises real routes (module-scoped schema, since the ten samples build on each other); **`test_reset_demo.py` and `test_extraction_routing.py` have no `db`/schema fixture at all and run directly against whatever `storage.PG_SCHEMA` currently is** — i.e. the real application schema (`public`), exactly as they ran directly against the real `data/app.db` before the migration (`test_extraction_routing.py` never imports `storage` itself, but `extraction.extract_invoice()` calls `quota.try_consume()` internally, which does). Both discovered, not introduced, while migrating; documented here rather than silently fixed, per §3. Practical effect: `test_extraction_routing.py` can fail if the real vision quota is already spent, and running the full suite clears real run history as a side effect of `test_reset_demo.py` actually exercising the reset endpoint against a live schema. One further Phase C wrinkle in the same vein: because the autouse document-storage fixture applies to `test_reset_demo.py` too, a reset it triggers against the real `public` schema deletes real `documents` rows but looks for their backing files in that test's own redirected (and irrelevant) tmp_path — so any real files that predated the test run are not found and not deleted. Harmless (an orphan file under `data/documents/`, not a correctness or security issue) and not worth solving with more machinery for a corner this narrow; noted here rather than silently left unexplained.

### Test suite — 480 tests, 19 files

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
| `test_documents.py` | 33 | Phase C: persistence, metadata, download, authorization, storage-key path safety, reset-demo cleanup |

Notes for whoever changes them:

* Both providers are **mocked at the transport boundary**, so the suite needs no
  key, no network and no quota. `test_samples.py` is the exception — it honours
  a live key and exercises the real routes, printing which mode ran.
* `test_samples.py` cases share one DB and run in manifest order. An early
  failure cascades — inherent, since later cases are *about* the state earlier
  ones left.
* The concurrency test uses real threads and a `Barrier`, exercising actual
  Postgres row locking (`SELECT ... FOR UPDATE` on the contended PO). Losing
  threads block and retry rather than raising outright, so the retry loop
  inherited from the SQLite version is now mostly a no-op — kept because it is
  still correct, and because a self-hosted instance under real contention can
  still occasionally raise (a timeout, a deadlock across multiple rows). The
  test asserts the outcome (never over budget, exactly five of eight
  approved), not the mechanism.

### API endpoints

```
GET  /api/health                 public
POST /api/auth/token             OAuth2 password grant  (rate limited per IP)
GET  /api/auth/me                authenticated
POST /api/runs/stream            [invoice:process]  + rate limit + daily budget
GET  /api/runs                   [invoice:read]
GET  /api/runs/{id}              [invoice:read]   includes the audit trail
GET  /api/runs/{id}/document     [invoice:read]   metadata only, no storage path
GET  /api/runs/{id}/document/download [invoice:read]  the PDF; ?inline=1 for inline
POST /api/runs/{id}/review       [invoice:review]
POST /api/runs/{id}/status       [invoice:admin]  cascades to held invoices
POST /api/admin/reset-demo       [invoice:admin]  clears run history + documents
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

⚠️ **3. The `public` Postgres schema accumulates runs, which breaks the
samples.** Not a bug (and not new — this was true of `data/app.db` too) — the
samples are history-dependent by design, so a second pass makes 01 a duplicate
of itself and leaves PO-1001 with $5.72. It was reported twice as "the happy
path is rejected". Now recoverable without shell access: **Reset demo data**
on Overview (admin), or `.\reset-demo.ps1`. Reset before recording. Also
happens as a side effect of running the full test suite — see the
`test_reset_demo.py` note under §Test suite.

⚠️ **5. `test_extraction_routing.py` reads the real quota counter.** It is
described as fully mocked, and the providers are — but `extraction` consults
`quota.try_consume()` against the real `public` Postgres schema before
reaching the fakes: this file has no `db`/schema fixture at all (see §Test
suite), so `storage.PG_SCHEMA` is never redirected. With the local vision
budget spent, some of its cases fail. Run right after a `reset-demo` for the
true result, or point `DATABASE_URL` at a scratch database while testing.

⚠️ **4. `extraction._first()` strips a leading minus sign** off a captured
amount, so `Total Due: -500.00` extracts as +500. Accounting parentheses are
unaffected. The amount rule cannot catch a sign the extractor discarded.

⚠️ **6. A `reset-demo` triggered from inside the test suite can leave orphan
document files on real disk.** `test_reset_demo.py` runs against the real
`public` Postgres schema by design (see issue 5's sibling note in §Test
suite) — but the autouse `_isolate_document_storage` fixture (Phase C, in
`conftest.py`) also redirects `config.DOCUMENT_STORAGE_DIR` for that same
test to a throwaway `tmp_path`. So when that test's reset call deletes real
`documents` rows from `public`, it looks for their backing files in the
wrong (test-local, irrelevant) directory and never finds them. The database
stays correct — no dangling row, no security exposure — but a file that
predated the test run can be left behind under the real `data/documents/`.
Narrow (only triggers when real files already exist AND a test process calls
the real reset endpoint) and harmless enough that it is documented rather
than fixed with more machinery, per §3.

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
3. Confirm `DATABASE_URL` is set (in `.env`) and Postgres is reachable — the
   app and the test suite both require it now; there is no SQLite fallback.
   `docker-compose up -d` if using the provided compose file, or point it at
   whatever local/managed instance is already running.
4. `.\venv\Scripts\python.exe -m pytest tests/ -q` — expect **480 passed**.
   No key or network needed. If it is not 480, find out what changed before
   building anything. Known non-regressions: `test_samples` 05 depends on
   Gemini being reachable (503 and 429 both happen), and
   `test_extraction_routing` can fail when the local vision quota is spent
   (§9 issue 5) — run right after a `reset-demo` for the true number.
5. `cd frontend-next && npm run build` if the UI was touched. The export in
   `out/` is what FastAPI serves; without a rebuild the browser keeps serving
   the previous one.
6. **Ask the user what they want next.** Do not start Phases 2–7, and do not fix
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
| **PostgreSQL, not SQLite, in every environment** | Requested explicitly, to prepare for real deployment and concurrent multi-user access — the same reasoning as every other pinning decision in this file (auditability needs a reproducible, restartable, shareable store; a single-writer file on one process's disk was never going to survive more than one uvicorn worker). No SQLite fallback anywhere, dev included, so "works on my machine" cannot mean "works against the wrong database." |
| Row-level locking (`SELECT ... FOR UPDATE`) instead of a database-wide lock for the ledger | SQLite's `BEGIN IMMEDIATE` had no finer granularity to reach for; Postgres does, and the actual invariant that needs defending is per-PO, not database-wide. Not a redesign of the guarantee — `test_concurrent_invoices_cannot_overspend_a_po` still passes unmodified in what it asserts — just a more precise implementation of the same one. |
| `psycopg2-binary`, no ORM | Same philosophy as the SQLite version: raw parameterised SQL, `storage.py` as the one module that touches the database. An ORM would have been a second architectural decision riding along on a driver swap, which was not what was asked. |
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
| Demo reset is an endpoint, not just a script | It was reported twice as a bug. Recovery should not require deleting a file on the server. Admin-scoped, deletes runs (and, since Phase C, the documents that belong to them) only. |
| ~~No per-field confidence shown~~ **REVERSED, at the user's explicit request** | Was: the pipeline did not produce one, and rendering an invented percentage would be fabricating evidence. Now: it is a genuine per-instance signal (model self-report, or a regex heuristic) computed by extraction and stored — see § Confidence, provenance and the confidence gate. |
| Dark mode is an explicit toggle, never `prefers-color-scheme` | A finance product should look the same in a demo as it did in design review; a form this dense reads as a "hacker" tool the instant the OS decides to darken it. Built as `:root[data-theme="dark"]`, opt-in only, persisted to localStorage — see § Visual design system and dark mode. |
| Document metadata and content are two different tables/abstractions, not one | `storage.py`'s `documents` table is queryable, joinable, backupable with the rest of the ledger; `documents.py`'s `DocumentStore` can be swapped (local disk → S3) without touching a single SQL statement. Mirrors why PO balances are derived rather than stored: separating "what is true" from "where the bytes live" keeps each concern replaceable on its own. |
| Storage key is always server-generated, never the original filename | The filename is attacker-controlled input; sanitising it is not the same guarantee as never using it as a path component at all. A UUID key removes the entire traversal/collision class rather than mitigating it. |
| `boto3` imported lazily inside `S3DocumentStore.__init__`, not at module top | A local-only install (`DOCUMENT_STORE_BACKEND=local`, the default) must never need a dependency it doesn't use — same principle as `extraction.py`'s provider SDKs being optional. |
| Document persistence can never fail the run it belongs to | By the time it runs the automated decision already exists; a storage hiccup is real but must not turn a correctly-decided invoice into a pipeline error the operator has to re-run. Same fail-safe posture as the daily quota breaker, logged not raised. |
| `EMAIL` recognised as a document source now, even though nothing produces it yet | So the schema and the `DocumentStore` abstraction do not need to change shape when Phase J's ingestion path exists. `storage.save_document()` still rejects any value outside `config.DOCUMENT_SOURCES` — recognising a future source is not the same as trusting an unvalidated one today. |

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

## 11. Frontend component architecture patterns (discovered)

**Layout composition over inheritance:** `ReviewWorkspace` exports both `ReviewWorkspaceBody` (the two-pane layout logic, reused inline on ProcessPage) and the full-screen overlay (ReviewWorkspace). The body is positioning-agnostic — the caller decides whether it's inline or in a modal. This avoids duplicating the layout logic and lets ProcessPage show it inline with its own page chrome, while InvoicesPage wraps it in a full-screen overlay. Same pattern should apply to new two-pane or multi-panel layouts.

**Document preview smart routing:** `DocumentPreview` handles three sources of truth: in-memory File (highest priority — it's current), fetched from API (for samples), empty state (for historical non-samples). It deduplicates sample names with an in-memory cache so repeated mounts don't refetch. When building similar "preview or fetch or empty" components, this priority order prevents stale data and keeps API calls minimal.

**Token-driven UI theming:** Every colour, spacing, type size flows through CSS custom properties in `globals.css`. Component-level styles read those tokens (e.g., `color: var(--text)`, `background: var(--surface)`). Dark mode is a second set of token values, never component-level `dark:` variants. This keeps the CSS lightweight and lets the entire app darkify consistently when the theme toggle fires. Adding components: always use tokens, never hardcode colours.

**Client-side filtering/sorting as default:** `InvoicesPage` fetches the full run list once, then filters/sorts/pages in the browser. This is the right default at ~1000 rows; no API params needed. The pattern scales to ~10k before moving to server-side (at that point, add query params to `GET /api/runs`, but the component shape stays the same). Filtering is boolean (status OR exceptions), sorting is by key + direction, paging is offset-based.

**Async state in hooks, not Redux:** Every page uses `useState` + `useEffect` to manage async fetches, filters, open modals. No Redux or Zustand. Keeps dependencies clear (a page's own filters only affect its own render) and state is co-located with the component that uses it. If the app grows and shared state across pages becomes essential, migrate to context + useContext, then consider Redux.

**Form validation client + server both:** ProcessPage validates file type/size client-side for fast UX (readable message instantly if over 10MB), but the server validates independently and is the real gate. This avoids a round-trip and gives snappy feedback without trusting the client.

**Confidence badges by rule engine, not re-derived in UI:** `ExtractedFields` colours confidence badges based on whether the RULE ENGINE flagged the field (`audit.low_confidence_fields`), not by re-calculating the threshold in the browser. This ensures the UI's read of "low" can never drift from what `decide()` used. The badge colour is deterministic and always matches the reason the run was held.

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

**Database schema:** Use named columns in INSERT statements (`INSERT INTO table (col1, col2) VALUES (?, ?)`) not positional, so queries survive schema growth. Migrations are additive via `_ensure_columns()` — idempotent and safe to re-run. Never store binary blobs (PDFs); reconstruct from in-memory File or fetch samples from API.

**API contracts with frontend:** `GET /api/runs` returns the full list in one response (client does pagination). `GET /api/runs/{id}` includes the full audit trail. `POST /api/runs/stream` streams stages as SSE ending with `final` event. Relative `/api/...` paths (same-origin). Bearer token in `Authorization` header for protected endpoints.

**Audit trail structure:** Stored in `runs.audit_json`, includes `automated_decision`, `reason`, `invoice`, `po_match`, `rules`, `extraction`, `problematic_fields`, `low_confidence_fields`, `suggested_resolution`. Frontend renders reason + problematic fields + suggestion in the reviewer brief. Full trail shown in Audit Trail panel.

**Frontend component patterns:** Layout composition (ReviewWorkspaceBody reused inline and in overlay). Document preview smart routing (File > fetch > empty state). Token-driven theming (no hardcoded colours, all via CSS custom properties). Client-side filtering/sorting default (scales to ~10k rows). Confidence badges coloured by rule engine, not re-derived in UI.

**Uncommitted changes in working tree:** frontend redesign + dark-mode toggle + DocumentPreview + ReviewWorkspace. Not committed because still being finalized. Commit message convention: end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.  
