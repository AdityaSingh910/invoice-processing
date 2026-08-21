# CLAUDE.md — project handoff

Read this first. This file is the technical handoff for a new Claude Code
session on this repository. It describes what exists, what must not be
redone, what is currently uncommitted, and what comes next.

**Working directory:** `c:\Users\adity\OneDrive\Desktop\Invoice processing`
Windows 11. PowerShell is primary; a Bash tool is also available.

---

## 1. What this is

An AP (accounts payable) automation application, originally built for the
**Zamp AI Solutions Associate case study, PS-1 (Finance / AP)** and since
extended into a deployable, multi-user platform (the "deployment-prep"
phases, §2 below).

**Core flow:** sign in → upload a vendor invoice PDF → it runs through a
9-stage pipeline live in the browser → produces **APPROVED / NEEDS_REVIEW /
REJECTED** with a deterministic, structured audit trail → anything held goes
to a human accept/reject queue that several employees can work
collaboratively → every run and every review action is recorded in a
dashboard and an activity history.

**Major components:**
- **Backend** (`backend/`) — FastAPI, PostgreSQL, the 9-stage pipeline, the
  deterministic rule engine, OAuth2 auth, document storage, multi-user review
  collaboration, email trusted-source verification (§7a).
- **Frontend** (`frontend-next/`) — Next.js 15 / React 19 / Tailwind v4,
  served as a static export by FastAPI. **Has uncommitted redesign work in
  progress — see §11, read before touching any frontend file.**
- **Frontend fallback** (`frontend/`) — the original vanilla HTML/JS UI,
  kept as a no-build fallback if `frontend-next/out/` was never built.
- **`data/`** — seed POs, vendors, demo users (JSON, tracked in git,
  reloaded into Postgres on every startup) plus gitignored runtime state
  (`documents/`).
- **`tests/`** — 638 tests, 21 files, real (schema-isolated) PostgreSQL, both
  LLM providers mocked. See §10.

---

## 2. Phase status

Two separate, differently-lettered phase tracks exist in this project's
history — do not conflate them:

- **Case-study phases (0–7)** — the original PS-1 build (pipeline, rules,
  confidence gate, etc.). All the phases that matter for the case study are
  done; see `README.md` for the case-study-specific detail (sample
  invoices, demo video status, grading criteria). Not the subject of this
  handoff.
- **Deployment-prep phases (A–M)** — turning the case study into a
  deployable, collaborative, multi-user platform. **This is the active
  track.** Current state:

| Phase | Work | Status | Commit |
|---|---|---|---|
| A | Architecture audit | ✅ Complete | — |
| B | SQLite → PostgreSQL migration | ✅ Complete | `147c0ce` |
| C | Persistent invoice PDF/document storage | ✅ Complete | `4d72899` |
| D | Multi-user collaboration + activity history | ✅ Complete | `345033a` |
| E | Review workflow hardening | ✅ Complete | `66e6f79` |
| F | Email security & trusted-source verification | ✅ Complete | see `git log` |
| G | Email invoice ingestion & extraction | ⬜ **Next — not started** | — |
| H | KPIs + analytics | ⬜ Not started | — |
| I | Logs + filters + grouping + exports | ⬜ Not started | — |
| J | Client access / client portal | ⬜ Not started | — |
| K | Chatbot (read-only invoice/AP assistant) | ⬜ Not started | — |
| L | Multilingual support | ⬜ Not started | — |
| M | Final security + deployment hardening | ⬜ Not started | — |

**Do not start Phase G or any later phase without being explicitly asked.**
This project has been built one verified phase at a time, each requested
individually, each committed on its own before the next began. See §9 for
what Phase G is planned to cover — plan only, nothing implemented.

**Do not redo A–F.** They are complete, tested, and committed. If something
in A–F looks wrong, raise it — don't silently "fix" or rebuild it.

---

## 3. Architecture

### Core philosophy

**The AI reads, the rules decide.** Extraction (reading a PDF into
structured fields) is done by an LLM, because invoice formats vary
enormously. The *decision* — approve, hold, reject — is 100% deterministic
Python with no model involved, so it is reproducible and auditable. No
prompt contains the words approve, reject, or tolerance.

### Pipeline (9 stages, streamed live over SSE)

```
Sign in → authenticate → authorize (scope) → rate limit → daily AI budget
        → validate the file
        ↓
INGEST → EXTRACT_TEXT → EXTRACT_FIELDS → VALIDATE → VENDOR_CHECK
       → PO_MATCH → DUPLICATE_CHECK → TOLERANCE_CHECK → DECISION
        ↓
audit trail → (if NEEDS_REVIEW) human accept/reject, collaboratively (§6, §7)
```

Stages do **not** short-circuit — findings accumulate and only the final
`DECISION` stage judges, so a reviewer sees the whole picture.

`POST /api/runs/stream` (`backend/main.py`) is the live run view: multipart
PDF in, SSE stage events out, ending with a `final` event carrying the full
result (status, reasons, extracted fields, PO match, audit trail).

### Extraction routes

| Route | When | Provider |
|---|---|---|
| `groq (text)` | PDF has an embedded text layer | Groq (`GROQ_API_KEY`) |
| `gemini (vision)` | Scanned/image-only PDF | Gemini (`GEMINI_API_KEY`) |
| `regex` | No Groq key, or the Groq call failed | — |
| `none` | Nothing readable — empty fields, never guesses | — |

Groq → regex on failure, deliberately not Groq → Gemini (would burn the
scarce vision-only budget on a route that already has a local fallback).
Models are pinned by ID (`openai/gpt-oss-120b`, `gemini-3.7-flash`), not
aliased, so which model read a given invoice is always answerable.

### Decision hierarchy

- **REJECTED** — never auto-overridden: duplicates, vendor on file but not
  approved, document is not an invoice, invoice states the PO's own number
  under a different currency (a same-digits currency collision).
- **NEEDS_REVIEW** — recoverable: missing fields, low extraction confidence
  on a field central to the decision, unreadable scan, over tolerance, no PO
  match, currency mismatch with no pinned rate (or still over tolerance
  after conversion), bad arithmetic, invalid total, inferred PO, multi-PO
  invoice with no stated split, injection-shaped text in the document.
- **APPROVED** — everything passed, including a currency mismatch a pinned
  FX rate resolves within tolerance.

Reject wins over review when both fire.

### Tolerance is one-sided

```python
within = diff <= tol        # NOT abs(diff) <= tol
```

Billing *over* the remaining PO balance is a problem; billing *under* it is
a normal partial invoice. Tolerance = `max(1% of remaining, $50)`
(`config.PO_TOLERANCE_PERCENT` / `PO_TOLERANCE_DOLLARS`).

### PO matching and the allocation ledger (`backend/matching.py`, `run_allocations` table)

There is **no stored "consumed" counter**. A PO's remaining balance is
derived on every read by summing `run_allocations` rows for that PO, joined
to `runs.status = 'APPROVED'`:

```
remaining_before = PO amount − Σ(allocations to that PO from prior APPROVED runs)
```

Only APPROVED runs consume budget. This makes idempotency and reversal
structural: nothing is deducted, so nothing can be double-deducted, and
moving a run out of APPROVED refunds it in the same instant (no explicit
refund step exists or is needed).

`run_allocations` exists because a single `po_number` column on `runs`
cannot describe an invoice spanning several POs. `matching.match_po()` binds
every referenced PO; `matching.split_across()` divides the total (fill each
PO to its remaining balance, in the order the invoice named them, last PO
absorbs any excess). **A multi-PO invoice is always held for review**, even
when the combined balance covers it — the document never states the split,
so the division is calculated, not read, and is shown to the reviewer to
confirm rather than derive.

### Currency mismatch and FX

Three outcomes when invoice currency ≠ PO currency:
1. **A pinned, versioned rate (`config.FX_RATES`) resolves it within
   tolerance → APPROVED**, with the rate and table version in the audit
   trail (reproducible by an auditor, unlike a rate fetched live).
2. **Same raw number, different currency** (e.g. invoice states "1500 EUR"
   against a "1500 USD" PO) — no correct conversion produces identical
   digits in a different currency, so this reads as a currency-code error or
   copied figure → **REJECTED outright**, the one case a currency finding
   rejects rather than holds.
3. **No pinned rate, or still doesn't fit after conversion → NEEDS_REVIEW.**

### Confidence, provenance and the confidence gate

Every extracted field can carry `{confidence, source, evidence,
evidence_verified}` (LLM routes self-report it in the same JSON call that
reads the value; regex gets a fixed heuristic score per match kind).
`config.CONFIDENCE_GATED_FIELDS = [vendor_name, invoice_number, total]`
scored below `config.CONFIDENCE_THRESHOLD = 0.65` holds the run for review
(never rejects — an extraction-uncertainty signal, not an invoice defect).

### The audit trail

`rules.decide()` builds a structured `audit` dict **as it evaluates** — each
check is recorded next to the branch that sets the verdict, not by a second
pass that could disagree with the decision. Stored in `runs.audit_json`.
Contains: automated decision, deterministic reason, invoice identity,
extraction route/provider, matched PO(s) with source file/row, allocations,
currency/FX detail, per-field provenance, every rule passed/failed, a
suggested resolution, and the fields any failing check implicates.

### Human review — automated decision vs. human decision

**These are two separate, permanently distinct concepts, never merged:**

```
automated_decision   NEEDS_REVIEW      ← what rules.decide() concluded — written once, NEVER rewritten
human_decision        ACCEPTED          ← a person's ruling, recorded beside it
final_decision         HUMAN_APPROVED    ← automated_decision + human_decision, combined
status                 APPROVED          ← what the ledger reads; moves so accepted runs consume PO budget
```

`automated_decision` is immutable audit history: it answers "what did the
deterministic rules conclude," forever, regardless of what a human later
decided. `status` is the only column that moves for ledger purposes, and it
moves through `storage.set_run_status()` — the single choke point used by
human review, the PO-freed-budget cascade, and the admin override — so
reversal and re-evaluation stay correct everywhere.

Only a run whose `automated_decision` is `NEEDS_REVIEW` is eligible for
human review. A run may be ruled on **once** — `record_human_review()`
enforces this (see §6.4 for exactly how, post-Phase-E). Reversing a ruling is
an admin action through `POST /api/runs/{id}/status`, which requires
`invoice:admin` and leaves its own audit trail.

---

## 4. Database — PostgreSQL

**PostgreSQL is the only database. There is no SQLite fallback anywhere,
dev included.** `DATABASE_URL` (env, read at call time via
`config.database_url()`) is required in every environment. `data/app.db` /
`app.db.bak` are vestigial pre-migration files; no code reads or writes them.

### Access pattern (`backend/storage.py`)

- Raw parameterised SQL via `psycopg2`, no ORM.
- `storage.get_conn()` returns a pooled connection
  (`psycopg2.pool.ThreadedConnectionPool`) wrapped in `_PooledConnection`, so
  `.close()` returns it to the pool rather than closing the socket. Every
  call site follows `conn = get_conn(); ...; conn.close()`.
- `storage.write_txn()` is a context manager that turns off autocommit for a
  read-modify-write unit, commits on success, rolls back on exception.
- `storage.PG_SCHEMA` (default `"public"`) is set per-connection via
  `SET search_path`. Test fixtures monkeypatch this to a fresh, uniquely
  named schema per test for isolation (`tests/pg_schema.py`).
- `storage.init_db()` runs on every FastAPI startup: creates tables if
  missing, adds any missing columns (`_ensure_columns`, since
  `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table), then
  **reloads `purchase_orders` and `vendors` from `data/*.json`** — editing
  those JSON files takes effect on next restart. `runs` and everything
  derived from it are never touched by the seed reload.

### Tables

```
purchase_orders   po_number (PK), vendor, amount, currency, issued_date,
                  status, description, source_file, source_row
                  — reloaded from data/purchase_orders.json every startup

vendors           vendor_name (PK), vendor_id, status
                  — reloaded from data/approved_vendors.json every startup

runs              id (PK, SERIAL), filename, status, created_at, vendor_name,
                  invoice_number, total, po_number, extracted_json,
                  po_match_json, stages_json, reasons_json, audit_json,
                  automated_decision, human_decision, final_decision,
                  reviewed_by, reviewed_at, review_note
                  — one row per processed invoice; the run history IS the ledger

run_allocations   id (PK, SERIAL), run_id (FK → runs.id), po_number, amount,
                  seq
                  — immutable per-run charges; a PO's consumed/remaining
                  balance is a derived SUM over these joined to
                  runs.status='APPROVED', never a stored counter

documents         id (PK, SERIAL), run_id (FK → runs.id), original_filename,
                  mime_type, size_bytes, sha256, uploaded_by, uploaded_at,
                  source, storage_backend, storage_key
                  — metadata only; PDF bytes live behind documents.py's
                  DocumentStore, keyed by storage_key (Phase C)

invoice_activity  id (PK, SERIAL), run_id (FK → runs.id), event_type, actor,
                  created_at, note, metadata_json
                  — append-only "what people (and the system) did" log,
                  distinct from audit_json's "why the rules decided what
                  they decided" (Phase D)

review_claims     id (PK, SERIAL), run_id (FK → runs.id), claimed_by,
                  claimed_at, expires_at, released_at, release_reason
                  — a leased claim on reviewing one run; "who currently
                  holds it" is DERIVED at read time (most recent row with
                  released_at IS NULL and an unexpired expires_at), never a
                  runs.current_reviewer column (Phase D)

extraction_quota  day, provider, used — PK (day, provider)
                  — daily per-provider extraction budget counter (quota.py,
                  not storage.py)

trusted_email_senders  sender (PK), kind, vendor_name, status, note
                  — reloaded from data/trusted_email_senders.json every
                  startup; no writer (Phase F)

email_messages    id (PK, SERIAL), run_id (nullable FK → runs.id), sha256,
                  message_id, received_at, submitted_by, source,
                  from_address, from_domain, from_display_name,
                  envelope_from, subject, size_bytes, attachment_count,
                  has_pdf_attachment, spf_result, dkim_result, dmarc_result,
                  dmarc_aligned, signature_kind, signature_result,
                  trusted_sender, classification, status, reasons_json,
                  auth_json, released_by, released_at, release_note
                  — what could be PROVEN about an incoming message. Metadata
                  and authentication evidence only; the body and attachment
                  bytes are never stored (Phase F, §7a.7)

email_activity    id (PK, SERIAL), email_id (FK → email_messages.id),
                  event_type, actor, created_at, note, metadata_json
                  — append-only history for a message. Separate from
                  invoice_activity because that table's run_id is NOT NULL
                  and a quarantined message has no run (Phase F, §7a.7)
```

**Not database tables, despite looking like they should be:** users live in
`data/users.json`, read directly by `auth.py` — there is no `users` table.
There is no `run_stage_logs` table either — a run's stage log is the
`stages_json` column on `runs`.

Indexes: `run_allocations(po_number)`, `run_allocations(run_id)`,
`documents(run_id)`, `invoice_activity(run_id)`,
`invoice_activity(created_at)`, `review_claims(run_id)`,
`review_claims(run_id, released_at)`, `runs(invoice_number)`, `runs(status)`,
`email_messages(status)`, `email_messages(sha256)`, `email_messages(run_id)`,
`email_messages(received_at)`, `email_activity(email_id)`.

### Concurrency and locking

Every place where two concurrent requests could race the same row takes a
`SELECT ... FOR UPDATE` lock on that row, for the shortest transaction that
correctly protects the invariant. This project deliberately reuses **one**
locking pattern everywhere rather than inventing a different concurrency
mechanism per feature:

- **`purchase_orders` row** — locked in `save_run_checked()` while
  committing a new run, so two invoices racing the same PO cannot both read
  the same remaining balance and both approve past it.
  (`test_concurrent_invoices_cannot_overspend_a_po`: 8 threads racing a
  $10,000 PO with $2,000 invoices resolve to exactly 5 approved / 3 held.)
- **`extraction_quota` row** — same pattern in `quota.try_consume()`, for
  the same reason (a read-then-increment needs a row lock once the
  database-wide SQLite lock is gone).
- **`runs` row** — locked by `claim_review()` (Phase D), and, as of Phase E,
  also by `set_run_status()`, `record_human_review()` and
  `release_review_claim()` (§6, §7). This is what serialises two employees
  racing a claim, two concurrent human decisions on one run, and a status
  change racing a decision — all through the same row, same lock, same
  order (`runs`, then `review_claims` where both are touched), which rules
  out lock-ordering deadlocks between these paths.

Invoices against **different** POs, or reviews of **different** runs, never
block each other — the lock is always scoped to the specific contended row,
not the database.

---

## 5. Document storage (Phase C)

The uploaded PDF survives the run that processed it.

- **`documents` table** (above) holds **metadata only** — never the bytes.
- **`backend/documents.py`** is the content-storage abstraction:
  `DocumentStore` interface, with `LocalDocumentStore` (default; files under
  `config.DOCUMENT_STORAGE_DIR`, `./data/documents`, gitignored — writes
  atomically via temp-file + `os.replace`) and `S3DocumentStore`
  (S3-compatible bucket; `boto3` imported lazily inside `__init__` so a
  local-only install never needs the package).
- **The storage key is never the original filename.** Always
  `documents.new_storage_key()` — a server-generated UUID4 — validated
  against a fixed shape (`^[0-9a-f]{32}\.pdf$`) before ever touching a
  filesystem path or object key. `LocalDocumentStore` additionally confirms
  the resolved path still sits inside the storage root. The original
  filename is kept only as sanitised display metadata
  (`main.py`'s `_safe_filename()`, applied before it ever reaches storage).
- **Persisting a document can never fail the run it belongs to** —
  `main.py`'s `_persist_document()` catches and logs; by the time it runs the
  automated decision already exists and, in most call sites, is already
  committed.

**Endpoints** (both require `invoice:read` — a document is invoice data, not
a separately-permissioned resource):
- `GET /api/runs/{id}/document` — metadata only, never `storage_backend`/
  `storage_key`.
- `GET /api/runs/{id}/document/download` — the actual bytes;
  `?inline=1` for `Content-Disposition: inline` (embedded viewer) vs. the
  default `attachment`. Both endpoints log `DOCUMENT_VIEWED` /
  `DOCUMENT_DOWNLOADED` activity with the authenticated caller as actor.

`POST /api/admin/reset-demo` clears `documents` rows and their backing files
in the same transaction as the runs they belong to (rows), plus a
best-effort, non-fatal file cleanup pass after commit.

---

## 6. Multi-user collaboration (Phase D)

Several employees can work the same review queue at once; the database, not
the frontend, is the authority on who owns what and what happened.

### 6.1 Two separate concepts

- **`audit_json`** on `runs` — why the *deterministic rules* reached a
  verdict. Written once by `rules.decide()`, never appended to.
- **`invoice_activity`** — what *people* (and the system, acting on their
  behalf) did about it afterwards: claimed, released, commented, accepted,
  rejected, viewed/downloaded the document, had a claim expire. Append-only
  — a later event never overwrites an earlier one.

Events logged: `PROCESSING_COMPLETED`, `REVIEW_REQUIRED`, `REVIEW_CLAIMED`,
`REVIEW_RELEASED` (with `release_reason` ∈ `released`/`expired`/`completed`/
`resolved`), `ACCEPTED`, `REJECTED`, `COMMENT_ADDED`, `DOCUMENT_VIEWED`,
`DOCUMENT_DOWNLOADED`, `STATUS_OVERRIDDEN`, `AUTO_APPROVED`. `actor` is the
authenticated username, or `NULL` for a system-generated event (an
auto-approval cascade, an expired claim being closed out) — never a name
invented for either case.

### 6.2 The review claim — a derived lease

One employee at a time may claim a `NEEDS_REVIEW` run
(`POST /api/runs/{id}/review/claim`). There is **no `runs.current_reviewer`
column and no in-memory lock** — `review_claims` rows are the source of
truth, and "who currently holds it" is derived at read time
(`storage.get_active_claim()`): the most recent row for the run with
`released_at IS NULL` and an unexpired `expires_at`.

- **Lease-based, not a permanent lock.** `config.review_claim_lease_minutes()`
  (env `REVIEW_CLAIM_LEASE_MINUTES`, default 15). A claim past its lease
  reads as inactive immediately, even before anything marks it
  `released_at`/`expired` — the *next* `claim_review()` call does that
  cleanup lazily. **There is no background sweep job.**
- **Concurrency guarantee:** `SELECT ... FOR UPDATE` on the `runs` row (see
  §4). Two concurrent claim attempts on the same run cannot both read "no
  active claim" and both insert — whichever commits first wins; the second
  sees the first's row and is refused with `409` + `{"claimed_by": ...,
  "expires_at": ...}`. Proved under 10 real threads racing one claim
  (`test_simultaneous_claims_produce_exactly_one_winner`).
- **Claiming again with the same identity renews** the lease rather than
  conflicting (a retry/heartbeat, not a conflict).
- **A run leaving `NEEDS_REVIEW` for any reason** (human ruling, cascade
  re-evaluation, admin override) **auto-releases whatever claim was on it**
  — implemented once, in `storage.set_run_status()`.
- **Only the claim's own holder may release it**, unless the caller also has
  `invoice:admin` (same override authority that scope already carries for
  `/status`).

### 6.3 Endpoints (Phase D)

```
POST /api/runs/{id}/review/claim    [invoice:review]  claim a NEEDS_REVIEW run
POST /api/runs/{id}/review/release  [invoice:review]  own claim only, or invoice:admin
POST /api/runs/{id}/comment         [invoice:review]  append a note, no ruling
GET  /api/runs/{id}/activity        [invoice:read]    chronological history + current_claim
```

`GET /api/runs/{id}` also carries `current_claim` directly (one extra
indexed query), so opening one invoice never needs a second round trip.
Deliberately **not** added to `GET /api/runs` (`list_runs`) — that returns
up to 200 rows in one call, and a claim lookup per row would multiply that.
Reviewer identity is always the authenticated principal — the claim/release
endpoints don't even accept a request body.

### 6.4 `record_human_review()` respects an active claim

A run actively claimed by someone **other than** the submitting reviewer is
refused (`{"error": "claimed", ...}`) — same check `claim_review()` applies,
extended to the decision-submission step so the protection can't be
sidestepped by skipping the claim step. **Claiming is optional, not
mandatory** — every review submitted before Phase D existed had no claim
behind it, and that path still works unmodified.

*(As of Phase E, this check and the decision write are atomic — see §7.)*

---

## 7. Review workflow hardening (Phase E)

Phase D's claim mechanism (`claim_review()`) was already correctly atomic —
one `SELECT ... FOR UPDATE` transaction. **Phase E found and fixed a gap
specifically in the decision-recording path**, `storage.record_human_review()`,
which Phase D's claim mechanism did not cover.

### 7.1 The gap that existed

`record_human_review()`'s eligibility check (not already reviewed, not
claimed by someone else) and its write used to run across **three separate,
unlocked transactions**: a bare `SELECT` with no lock, then
`set_run_status()`'s own transaction, then a further `UPDATE`. Two
concurrent submissions on the same run — a double-clicked Accept, a retried
request, or a genuine ACCEPT-vs-REJECT race between two reviewers — could
both read `human_decision IS NULL` before either committed, and both would
then write: **two conflicting rulings both landing in `invoice_activity`**
(an `ACCEPTED` row and a `REJECTED` row on one run), with the run's final
`status` decided by whichever transaction happened to commit last rather
than either caller being refused.

### 7.2 The fix

`record_human_review()` now performs its whole check-then-act sequence
inside **one** `write_txn()`, under `SELECT ... FOR UPDATE` on the run row —
the same lock, same row, `claim_review()` and `set_run_status()` already
take. The claim-ownership check now reads `review_claims` inline on the same
cursor, inside the same transaction (not via a separate `get_active_claim()`
call, which would open a second connection and could see a different
snapshot than the write that follows).

A concurrent second request now necessarily **blocks** on the lock until the
first commits, then re-reads `human_decision` (now set) and is correctly
refused with `{"error": "this run has already been reviewed (...)"}` — same
error shape and HTTP mapping (`409`) as before; only the guarantee behind it
changed.

**`_apply_status_transition(cur, ...)`** was extracted as the one shared
implementation of "move a run's status, update `final_decision`, release any
protected claim" — both `set_run_status()` (which now takes the lock itself
before calling it) and `record_human_review()` (which takes the lock once,
up front, for its whole transaction) call this same function. Previously
`record_human_review()` called the public `set_run_status()` as a black box,
which is what caused the original multi-transaction gap.

**`release_review_claim()`** now also locks `runs` first (same order
everywhere: `runs`, then `review_claims`) — this fully serialises claim,
release, and decision-recording against each other, and fixed a real, minor
existing bug as a side effect: releasing a claim on a `run_id` that doesn't
exist used to read as `"no active claim on this run"` (409) rather than the
`"unknown run"` (404) every other endpoint gives for a missing run.

### 7.3 What this protects, concretely

- **Duplicate submission** (double-click Accept, network retry): second
  request refused with `"already been reviewed"`, never applied twice.
- **Concurrent ACCEPT vs REJECT**: exactly one lands; the loser is refused,
  never silently overwritten or both recorded.
- **Duplicate PO charging**: since only one decision can ever land, the PO
  ledger (`run_allocations` → `runs.status='APPROVED'`) can never be charged
  twice for one run — verified directly (see §7.4).
- **`automated_decision` vs. `human_decision` separation is unaffected** —
  Phase E changed *when* the write is allowed to happen, not what it writes;
  `automated_decision` was already immutable and still is.

### 7.4 Tests added (in `tests/test_review_collaboration.py`)

Real-thread concurrency (same `threading.Barrier` pattern the Phase D
10-thread claim-race test already used — not mocked, not a sleep-based
approximation):
- `test_concurrent_conflicting_decisions_only_one_wins` — 10 reviewers race
  ACCEPT/REJECT on one run; exactly one decision lands, the activity history
  carries exactly that one ruling.
- `test_concurrent_duplicate_accepts_only_one_lands` — 8 reviewers
  double-submit the identical ACCEPT; exactly one lands, and
  `consumed_amount_for_po()` confirms the PO was charged exactly once.
- `test_release_on_an_unknown_run_is_refused_not_a_500` /
  `test_release_on_an_unknown_run_is_404_over_http` — the fixed
  unknown-run-release bug.
- `test_review_endpoint_refuses_a_second_submission_not_a_500` — HTTP-level
  duplicate submission.

**No API contract changed** — every error string, HTTP status code, and
response shape from Phase D is identical; only what the endpoints guarantee
under concurrency changed.

---

## 7a. Email security & trusted-source verification (Phase F)

**Status: implemented, tested (110 tests), verified, committed.**

Everything in this section describes code that exists and passes tests. Where
something is *not* implemented it says so explicitly — nothing here is
aspirational.

### 7a.1 What Phase F is, and the boundary with Phase G

**There was no email ingestion pipeline before this phase, and there still
isn't one.** Before Phase F the only trace of email anywhere in the backend
was `config.DOCUMENT_SOURCES = ("MANUAL_UPLOAD", "EMAIL")` and two comments
saying nothing wrote that value yet. Phase F did **not** add mailbox
connectivity — no IMAP, no POP, no polling, no provider client. That is
Phase G.

What Phase F adds is the **verification layer and the seam Phase G plugs
into**: a message is *handed* to the application (an uploaded `.eml`), and
the application decides how much of its claimed origin it can prove. Whatever
transport eventually retrieves a message hands the same raw bytes to the same
`email_security.classify()` and gets the same verdict, because the verdict
depends only on the bytes and on configuration.

**Nothing in Phase F creates a run or processes an attachment.** An ADMITTED
message is one that is *allowed* to be processed; actually processing it is
the next phase.

### 7a.2 The one idea the whole design rests on

A message is a blob of bytes, and **every byte of it was chosen by whoever
sent it** — `From:`, `Received:`, `Received-SPF:`, and `Authentication-Results:`
alike. Exactly two things in that blob can be believed:

1. **A header stamped by a boundary we control and can name.**
   `config.email_trusted_authserv_ids()` (env `EMAIL_TRUSTED_AUTHSERV_IDS`)
   is an allowlist of authserv-ids. Any `Authentication-Results` header
   carrying a different authserv-id is **discarded** — and *recorded* as
   discarded in the evidence, so an auditor can see that someone tried it.
   **Empty is the default and is safe, not broken:** nothing is believed, so
   every message without a verifiable signature reads UNVERIFIED and is
   quarantined rather than trusted. There is no "trust whatever the header
   says" fallback anywhere.
2. **A cryptographic signature verified here.** DKIM is real public-key
   cryptography over the message's own bytes, so who relayed it is irrelevant.

`Received-SPF:` is **never** believed — it carries no authserv-id, so it
cannot be attributed to our boundary rather than to the sender. It is kept as
evidence only.

### 7a.3 What is and is not verifiable — read before extending

| Mechanism | Status | Detail |
|---|---|---|
| **DKIM** | **Genuinely verified** | Full RFC 6376 in `email_security.verify_dkim()`: simple/relaxed canonicalisation (both header and body, all four combinations tested), `bh=` body hash, `l=` partial-body length, bottom-up signed-header selection (§5.4.2), `x=` signature expiry, `rsa-sha256` and `ed25519-sha256`. `rsa-sha1` is deliberately refused (RFC 8301). Uses `cryptography`, which pdfplumber already required. |
| **SPF** | **Relayed or unavailable — never computed** | SPF authorises the *connecting IP*, which a stored message cannot establish; `Received:` headers are just more sender-chosen text. Reported from a trusted `Authentication-Results` header, or `unavailable`. Never guessed. |
| **DMARC alignment** | **Computed locally** | Strict and relaxed, both recorded. This is the check that catches a spoofed From riding on a real signature from elsewhere, and it needs no network. |
| **DMARC policy (`p=`)** | **Needs DNS** | `unavailable` with the default resolver. "Publishes no DMARC record" and "we could not look" stay distinct. |
| **S/MIME / PGP** | **Detected only, never verified** | See §7a.6. |

**DKIM public keys and DMARC policies come from a pluggable
`DnsTxtResolver`** — the same shape as Phase C's `DocumentStore`, for the same
reason:

- `NullDnsTxtResolver` — **the default.** Resolves nothing, so DKIM reports
  `unavailable` (never `fail` — a signature we could not fetch a key for has
  not failed anything).
- `StaticDnsTxtResolver` — a fixed table. Two real uses: pinning a known
  vendor's key the way `config.FX_RATES` pins an exchange rate (reproducible
  and auditable a year later, unlike a live lookup), and driving the tests
  with a real generated keypair and no network.
- `DnspythonTxtResolver` — live DNS. `dnspython` is imported lazily in the
  constructor and is **not** in `requirements.txt` (it is listed there,
  commented out, exactly like `boto3`), so an install that never sets
  `EMAIL_DNS_RESOLVER=dnspython` never needs the package.

### 7a.4 Three states, not two

Every mechanism reports **`pass` / `fail` / `unavailable`**, and the third is
load-bearing. "We checked and it failed" and "we could not check" are
different facts, and collapsing them would either flag honest senders as
hostile or wave unverified ones through. The RFC 8601 result word is *always*
kept beside the normalised state (`spf_result` = `fail`, `spf_detail` =
`softfail`), because a record that lost that distinction could not tell them
apart later.

RFC 8601 `temperror` and `permerror` map to **`unavailable`, not `fail`** —
they mean the evaluation could not be completed (DNS timed out, record
malformed), not that the sender failed. Treating a DNS outage as an
authentication failure would quarantine a vendor because someone else's
nameserver was down.

### 7a.5 Classification and the quarantine gate

`email_security.classify()` is deterministic — no model, and the audit record
is built *as it evaluates*, next to the branch that sets the verdict, exactly
as `rules.decide()` does.

| Classification | Status | When |
|---|---|---|
| **VERIFIED** | `ADMITTED` | An aligned, passing result from believable evidence, **and** the sender is on the trusted-sender list |
| **FAILED** | `QUARANTINED` | Something was checked and did not pass: a signature that did not verify, a revoked key, a trusted boundary reporting a failure, a structurally spoofed `From` |
| **SUSPICIOUS** | `QUARANTINED` | Signals that disagree with each other, or an authenticated sender nobody allowlisted |
| **UNVERIFIED** | `QUARANTINED` | Nothing could be checked. **Not an accusation.** The default state of a deployment with no trusted boundary and no resolver |

**Quarantine is a hold, exactly as `NEEDS_REVIEW` is a hold** — same posture
the confidence gate and injection guard already take. An UNVERIFIED message
is described as a gap in what could be checked, never as hostile; a test
asserts the reason text contains none of "malicious", "hostile", "spoof",
"attack", "fraud".

**Authentication is not authorisation, and the reverse.** An authenticated
sender who is not on the allowlist is SUSPICIOUS, not VERIFIED. Being on the
allowlist authenticates nobody — an allowlisted domain in `From` costs a
spoofer nothing. Both directions are tested.

**Explicitly not proof of legitimacy.** A pass proves the claimed origin, not
that the invoice is legitimate; a compromised-but-authenticated mailbox
passes every check here. `rules.decide()` still runs unchanged on the content.
The stored record carries its own `limitations` list saying so, so the caveat
travels with the verdict rather than living only in documentation.

### 7a.6 Digital signatures — a deliberately different question

`backend/email_signature.py` exists as a separate module specifically so that
DKIM and a user-level signature can never be conflated:

- **DKIM** says a *domain* asserts a message passed through its
  infrastructure.
- **S/MIME** says a *person*, holding a certificate from a CA you chose to
  trust, asserts they composed it.

**What it does today: DETECTS, never verifies.** Detection reads MIME
structure only (`multipart/signed` + protocol parameter, `application/pkcs7-mime`,
or a signature part found anywhere in the tree) and is reliable. Verification
is **not implemented, and not because it was skipped**: S/MIME verifies
against a certificate chain terminating in a trust anchor, and this
deployment has no certificate store, no configured roots and no revocation
source (CRL/OCSP). Verifying while accepting any root would let a self-signed
certificate naming the CFO pass — a "valid" that means nothing. Same argument
for PGP and a keyring.

So: `SignatureVerifier` is the interface a real one implements, and
`UnavailableSignatureVerifier` reports `not_present` or `unavailable` and
**has no code path that returns a pass, by construction** — asserted against
every message shape in the test file at once. `EMAIL_SIGNATURE_VERIFIER` set
to anything other than `none` **raises** rather than silently downgrading to
detection, because a deployment that asked for verification and quietly got
detection is exactly the false assurance this module exists to avoid.

A DKIM-passing message with no S/MIME reports `signature_result = not_present`
— tested directly.

### 7a.7 Storage — evidence, not email

Two new tables (plus `trusted_email_senders`, seeded from
`data/trusted_email_senders.json` on every startup exactly like
`purchase_orders`/`vendors`, and with no writer — it is procurement reference
data, not runtime state).

```
email_messages    id, run_id (nullable FK -> runs), sha256, message_id,
                  received_at, submitted_by, source, from_address, from_domain,
                  from_display_name, envelope_from, subject, size_bytes,
                  attachment_count, has_pdf_attachment, spf_result,
                  dkim_result, dmarc_result, dmarc_aligned, signature_kind,
                  signature_result, trusted_sender, classification, status,
                  reasons_json, auth_json, released_by, released_at,
                  release_note

email_activity    id, email_id (FK -> email_messages), event_type, actor,
                  created_at, note, metadata_json
```

Indexes: `email_messages(status)`, `(sha256)`, `(run_id)`, `(received_at)`,
`email_activity(email_id)`.

**The message body is never stored, and neither are attachment bytes.**
`auth_json` holds the authentication headers, signature parameters and
alignment arithmetic — what an auditor needs to re-derive the verdict — and
nothing that needs that also needs the invoice text. Attachments contribute
metadata only (sanitised filename, content type, size, sha256). `sha256` over
the raw message ties a record to a message without keeping the message. A
test asserts a distinctive body string does not appear anywhere in the stored
record.

**`classification` vs `status` is the same split as `runs.automated_decision`
vs `runs.status`:** the classification is what the deterministic evaluator
concluded and is never rewritten; `status` moves when a person rules on it.

**Why `email_activity` is not `invoice_activity`:** `invoice_activity.run_id`
is `NOT NULL` and foreign-keyed to `runs`. A quarantined message has no run
and, if discarded, never will — so it cannot be represented there without
dropping that constraint, which is a Phase D invariant this phase has no
business weakening. Same design, same columns, same append-only rule,
different subject. Once Phase G turns an admitted message into a run, that
run's history continues in `invoice_activity`; the two are **joined** by
`email_messages.run_id`, not merged.

**`link_email_to_run()`** is the Phase G seam — nothing in Phase F calls it.
It refuses to link a run to a message that is `QUARANTINED` or `DISCARDED`,
which is the gate: a held message cannot reach the pipeline by the back door.

**`clear_run_history()` / reset-demo** nulls `email_messages.run_id` and
**keeps the security record**. A finding about a sender stays true whether or
not the invoice it carried is still on file, and the endpoint's stated
narrowness ("it deletes runs only") is preserved.

### 7a.8 Concurrency

`set_email_status()` performs its whole check-then-act inside **one**
`write_txn()` under `SELECT ... FOR UPDATE` on the message row — built the way
Phase E rebuilt `record_human_review()`, for the same reason. A message may be
ruled on **once**; a second concurrent request blocks, re-reads the status it
may no longer change, and is refused. Proved under 10 real threads racing
RELEASE against DISCARD on one message: exactly one lands, and the history
carries exactly one ruling.

### 7a.9 Endpoints

```
POST /api/email/messages                  [invoice:process]  verify + record a raw message
GET  /api/email/messages                  [invoice:read]     list (?status_filter=)
GET  /api/email/messages/{id}             [invoice:read]     verdict + full evidence + activity
POST /api/email/messages/{id}/release     [invoice:review]   release a quarantined message
POST /api/email/messages/{id}/discard     [invoice:review]   discard one (terminal)
GET  /api/email/trusted-senders           [invoice:read]     allowlist + verification setup
```

**No new scope was created** — Phase F reuses the existing ones, per the
"integrate, don't duplicate" rule: submitting is ingestion (`invoice:process`,
and it carries the same per-user rate limit as invoice processing so it cannot
be used to bypass it), reading a record is reading invoice data
(`invoice:read`, the same call the document endpoints make), and releasing is
a hold/release ruling (`invoice:review`, the same authority that accepts a
`NEEDS_REVIEW` invoice). The actor is always the authenticated principal.

A **byte-identical resubmission returns the existing record** with
`duplicate: true` rather than writing a second row — safe for a retry or a
double-click, and the replay is logged as `MESSAGE_RESUBMITTED` rather than
hidden behind a duplicate. This is sound only because classification is
deterministic, which is itself tested.

### 7a.10 Known limitations (all stated, none worked around)

1. **No mailbox connectivity.** Messages are submitted, not fetched. Phase G.
2. **SPF is never computed locally** — architecturally impossible from a
   stored message. Relayed from a trusted boundary or `unavailable`.
3. **User-level signature verification is not implemented** — detection only,
   status `unavailable`. Needs a trust anchor and revocation source (§7a.6).
4. **Relaxed DMARC alignment uses a heuristic public-suffix list**, not the
   full PSL (`email_security._MULTI_LABEL_SUFFIXES`). For an unknown
   multi-label suffix it takes one label too few, which could make two
   unrelated domains under that suffix look organizationally aligned. Strict
   alignment is an exact match with no heuristic involved; both answers are
   recorded separately in the audit record. Swapping in a real PSL lookup is a
   self-contained change to `organizational_domain()`.
5. **With no `EMAIL_TRUSTED_AUTHSERV_IDS` and no resolver configured — the
   out-of-the-box default — every unsigned message is UNVERIFIED and
   quarantined.** That is correct and safe, but it means the feature does
   nothing useful until a deployment names its boundary or enables DNS.
6. **No frontend.** These endpoints have no UI; Phase F is backend-and-tests
   only, the same restriction Phases D and E worked under (§11).

## 8. Authentication, authorization

- **OAuth 2.0 resource-server pattern.** `Authorization: Bearer <JWT>`,
  validated for signature, expiry, issuer. `POST /api/auth/token` is the
  password grant (rate-limited per IP). `pyjwt`; PBKDF2-HMAC-SHA256 password
  hashing from the stdlib. Users live in `data/users.json` — no `users`
  table.
- **Scopes** (`backend/auth.py`): `invoice:read`, `invoice:process`,
  `invoice:review`, `invoice:admin`. Demo roles: `viewer` (read only),
  `analyst` (+process), `reviewer` (+review), `admin` (+override any status).
- **Rate limiting** — per user and per IP, sliding window
  (`backend/ratelimit.py`), default 20 processing requests/min/user,
  per-process (not shared across workers — a known scale limit if this ever
  runs with more than one uvicorn worker).
- **Daily AI budget** (`backend/quota.py`) — a slower circuit breaker on top
  of rate limiting; PostgreSQL-backed counter, fails open (cost guard, not a
  security control).
- **Production safety** — `APP_ENV=production` refuses to start with:
  missing `AUTH_SECRET`, demo credentials present, empty user store, or
  wildcard CORS. Checked in `auth.enforce_production_config()` at startup.
- **Input validation** — uploads read in capped chunks (not buffered then
  measured), PDFs validated by magic bytes (`%PDF-`), filenames reduced to a
  safe basename (`main.py`'s `_safe_filename()`).

---

## 9. Roadmap — Phase F and beyond

**Nothing in this section is implemented. This is a plan only.**

### Phase F — Email security & trusted-source verification (DONE)

**Implemented, tested and verified — see [§7a](#7a-email-security--trusted-source-verification-phase-f)
for what it actually does, and §7a.10 for what it deliberately does not.**
This roadmap entry is kept only as a marker; §7a is the authority. Note that
the original plan listed "SPF verification" as ordinary scope — that turned
out to be impossible to compute from a stored message, and §7a.3 records the
reason rather than the plan pretending otherwise.

### Phase G — Email invoice ingestion & extraction (NEXT, not started)

Actual ingestion, once Phase F's trust signal exists to gate it:
connecting to an email provider, detecting incoming invoice emails,
retrieving attachments, storing them (via the existing `DocumentStore`
abstraction from Phase C — `config.DOCUMENT_SOURCES` already recognises
`"EMAIL"` as a source value, unused until this phase), and feeding verified
invoices through the existing pipeline (§3) unchanged.

### H–M

KPIs/analytics, logs/filters/grouping/exports, a client-facing portal, a
read-only AP chatbot, multilingual support, and a final security/deployment
hardening pass — all unstarted, all deferred until asked for individually.

---

## 10. Testing

**638 tests, 21 files.** Both Groq and Gemini mocked at the HTTP transport
boundary — the suite needs no API key, no network, no quota, only a reachable
PostgreSQL (`DATABASE_URL`). `test_samples.py` is the deliberate exception:
it honours a live key and exercises the real routes end-to-end.

Phase F's DKIM tests also need no network: they **generate a real RSA keypair
and perform a genuine RFC 6376 signing pass**, then hand the verifier the
matching public key through a `StaticDnsTxtResolver`. When those tests say a
signature verified, an actual signature actually verified.

```powershell
.\venv\Scripts\python.exe -m pytest tests\ -q
```

| File | Tests | Covers |
|---|---|---|
| `test_email_security.py` | 110 | Phase F: real DKIM verification (all four canonicalisations, tampered signature/body, revoked key), DMARC alignment, discarded/forged Authentication-Results, spoofed From, conflicting signals, unavailable-vs-failed, S/MIME + PGP detection, malformed/hostile headers, quarantine gate, 10-thread ruling race, authorization, backwards compatibility |
| `test_api_security.py` | 59 | authn, authz, rate limits, secrets, input, errors |
| `test_review_collaboration.py` | 48 | Phase D (claiming, 10-thread claim race, stale-claim recovery, activity, HTTP auth) + Phase E (decision-atomicity races, unknown-run-release, HTTP duplicate submission) |
| `test_vendor_matching.py` | 40 | normalisation, substrings, ambiguity |
| `test_production_safety.py` | 39 | APP_ENV gates, demo creds, daily quota |
| `test_documents.py` | 33 | Phase C: persistence, metadata, download, authorization, storage-key path safety |
| `test_confidence.py` | 31 | provenance, the confidence gate, suggested resolution |
| `test_currency.py` | 29 | pinned-rate FX approve, same-number reject, held-else |
| `test_human_review.py` | 28 | accept/reject, ledger effect, eligibility |
| `test_multi_po.py` | 28 | multi-PO binding, the split, the ledger, the hold |
| `test_security.py` | 27 | prompt injection, false-positive floor |
| `test_extraction_routing.py` | 23 | Groq/Gemini routing, failure fallbacks |
| `test_audit_trail.py` | 22 | trail structure, provenance, determinism |
| `test_arithmetic.py` | 22 | subtotal + tax == total |
| `test_invalid_amount.py` | 21 | zero / negative totals |
| `test_document_type.py` | 19 | the not-an-invoice check |
| `test_allocations.py` | 13 | the allocation ledger, migration, idempotence |
| `test_inferred_po.py` | 13 | distance cap, ambiguity guard |
| `test_po_edge_cases.py` | 12 | split-PO, idempotency, reversal, PO-lock concurrency |
| `test_reset_demo.py` | 11 | who may clear run history, what survives it |
| `test_samples.py` | 10 | the 10 samples end to end, in manifest order |

(Counts verified via `pytest --collect-only -q` on the current tree — not
copied from an old table.)

**Concurrency tests use real threads against real PostgreSQL** — a
`threading.Barrier` so every thread starts simultaneously, then asserts the
outcome (exactly one winner, the balance never over budget), not the
mechanism. Not mocked, not sleep-based.

**Verified state at the end of Phase F** (runs on 2026-08-21, after the work
described in §7a). `tests/test_email_security.py` alone: **110 passed.**
Full-suite results, and how to read them:

- **With `GEMINI_API_KEY`/`GROQ_API_KEY` unset (the deterministic baseline):
  634 passed, 4 failed** — the 4 being exactly the pre-existing
  `test_extraction_routing.py` cases described below. Re-running that file
  alone: **23/23 passed.**
- **With live keys set: 633 passed, 5 failed** — the same 4, plus
  `test_samples.py::test_sample_invoice[05_scanned_no_text.pdf]`. That fifth
  one **disappears entirely when the keys are unset**, which is the proof of
  what causes it.

**That fifth failure is the live Gemini free-tier quota running out, not a
regression.** The assertion output names the cause directly: `Vision
extraction failed - rate limit / quota exhausted (429)`. `test_samples.py` is
the one file that deliberately honours a live key and calls the real API
(see the top of this section), the scanned sample is the only one needing the
vision route, and repeatedly running the full suite drains a free tier of 20
requests per day. The pipeline behaved correctly under it — an unreadable
scan was held for review rather than guessed at, which is the designed
response to exactly this outage.

Two independent confirmations that Phase F cannot be responsible:

1. **Phase F is purely additive.** `git diff --stat` over the modified
   backend files reports **732 insertions and 0 deletions**; `extraction.py`,
   `rules.py`, `matching.py` and `quota.py` were not touched at all.
2. The failure appeared between two runs whose only intervening change was
   edits to `CLAUDE.md` and `README.md`.

**If you see it, wait for the daily quota to reset (or unset `GEMINI_API_KEY`,
which routes that sample differently) before concluding anything broke.** The
same underlying flakiness is already recorded in README's "Known problems"
as the reason the scanned-sample demo is not reliably reproducible.

Those 106 were additionally checked against passing vacuously, by mutation:
making `collect_authentication_results()` trust every header broke exactly
the two anti-spoofing tests, and making the DKIM signature check always
succeed broke exactly the two tampering tests. Both mutations were reverted
and the suite re-verified green.

**Verified state at the end of Phase E:** full-suite run: **524 passed, 4
failed** — all 4 failures in `test_extraction_routing.py`
(`test_scanned_pdf_routes_to_gemini_vision`,
`test_routing_follows_the_text_layer_not_the_file_name`,
`test_both_providers_produce_the_same_invoice_structure`,
`test_gemini_vision_failure_degrades_to_route_none`). This is a **pre-existing,
documented condition**, not a Phase E regression: `test_extraction_routing.py`
has no `db`/schema fixture and runs against the real `public` Postgres
schema, so it can fail if the real daily vision-extraction quota is already
spent by other activity against that schema. Confirmed by re-running that
file alone immediately afterward: **23/23 passed.** If you see this same
failure pattern, re-run `test_extraction_routing.py` alone (or right after
`.\reset-demo.ps1`) to get the true result before concluding something broke.

**Test isolation:** every `db(tmp_path, monkeypatch)`-style fixture
repoints `storage.PG_SCHEMA` at a fresh, uniquely-named schema per test
(`tests/pg_schema.py`), created and dropped automatically. Exceptions:
`test_samples.py` uses one module-scoped schema (the ten samples build on
each other in order); `test_reset_demo.py` and `test_extraction_routing.py`
have no schema fixture at all and run against whatever `storage.PG_SCHEMA`
currently is (the real `public` schema) — pre-existing, documented, not
something to "fix" without being asked.

---

## 11. Frontend state — ⚠️ read before touching any frontend file

**There is a substantial, intentional frontend redesign sitting uncommitted
in the working tree.** It predates Phases C, D and E, and none of those
phases touched, committed, or discarded any of it — each was scoped
backend-and-tests-only. **Do not revert, discard, reformat, or commit any of
it as part of an unrelated backend change.** Current `git status` (verify
with a fresh `git status` before assuming this is still accurate):

```
modified:   frontend-next/app/globals.css
modified:   frontend-next/app/page.tsx
modified:   frontend-next/components/charts.tsx
modified:   frontend-next/components/invoice/Panels.tsx
modified:   frontend-next/components/invoice/PoMatchPanel.tsx
deleted:    frontend-next/components/invoice/RunDetail.tsx
modified:   frontend-next/components/invoice/StageList.tsx
modified:   frontend-next/components/layout/AppShell.tsx
modified:   frontend-next/components/pages/InvoicesPage.tsx
modified:   frontend-next/components/pages/OverviewPage.tsx
modified:   frontend-next/components/pages/ProcessPage.tsx
modified:   frontend-next/components/pages/ReferencePage.tsx
modified:   frontend-next/components/ui/index.tsx

untracked:  frontend-next/components/invoice/DocumentPreview.tsx
untracked:  frontend-next/components/invoice/ReviewWorkspace.tsx
untracked:  claudee.md   (stray file at repo root — not part of the app; leave as-is unless asked)
```

This is a redesign toward a light-first enterprise finance interface with an
explicit dark-mode toggle (`:root[data-theme="dark"]`, never
`prefers-color-scheme`) — `RunDetail.tsx` was split into `DocumentPreview.tsx`
+ `ReviewWorkspace.tsx`. **If asked to commit backend work, stage backend/
test/doc files explicitly by name (`git add backend/x.py tests/y.py
CLAUDE.md`), never `git add -A` or `git add .`,** or this frontend work will
be swept into an unrelated commit. This exact discipline was followed for
the Phase E commit (`66e6f79`) — only `backend/storage.py`,
`tests/test_review_collaboration.py`, `CLAUDE.md`, and `README.md` were
staged.

If the frontend needs work, that is explicitly out of scope for backend
phase work unless asked — see the Phase D/E briefs, which both stated the
restriction directly.

---

## 12. Running it

**Requires PostgreSQL** — `DATABASE_URL` in `.env`. `docker-compose up -d`
for a local instance matching `.env.example`, or point at whatever instance
is already configured.

```powershell
.\start.ps1                 # installs deps, generates samples, starts server, opens browser
.\venv\Scripts\python.exe -m pytest tests\ -q      # 638 tests, no key/network needed
.\reset-demo.ps1             # clear run history (samples are order-dependent)
.\reset-demo.ps1 -Replay     # clear, then drive all 10 samples through the API
```

| Username | Password | Can |
|---|---|---|
| `viewer` | `demo-viewer` | read |
| `analyst` | `demo-analyst` | + process invoices |
| `reviewer` | `demo-reviewer` | + accept/reject held invoices, claim reviews |
| `admin` | `demo-admin` | + override any run's status |

**Known operational gotchas:**
- **`start.ps1` launched from a tool call does not survive** — the process
  tree is cleaned up when the call ends. Start it from a terminal the user
  owns.
- **Set `AUTH_SECRET` in `.env`** before a demo — without it, a fresh signing
  key is generated per process and every restart invalidates existing
  tokens.
- **Sample invoices are order-dependent** (`sample_invoices/manifest.json`)
  — run `.\reset-demo.ps1` before relying on their documented verdicts.
- **`cd frontend-next && npm run build`** after any frontend change — FastAPI
  serves the static export in `out/`; without a rebuild the browser keeps
  serving the old one (the HTML shell is served `no-store` specifically to
  avoid this class of problem, but the export itself still has to be rebuilt).

---

## 13. Git / handoff state

**Latest completed phase: F (email security & trusted-source verification),
§7a — implemented, tested, and committed as its own commit (the most recent
one; `git log --oneline -1` names it).**
**Phase G has NOT been implemented — do not start it without being
explicitly asked.**

Phase F was staged **by name** — `backend/config.py`,
`backend/email_security.py`, `backend/email_signature.py`, `backend/main.py`,
`backend/storage.py`, `data/trusted_email_senders.json`,
`tests/test_email_security.py`, `requirements.txt`, `.env.example`,
`CLAUDE.md`, `README.md` — never `git add -A`, so the unrelated frontend
redesign (§11) stayed in the working tree untouched. Verified after the fact:
the frontend diff is byte-identical to what it was before the phase began.
**Do the same for Phase G.**

Recent commits (`git log --oneline -6`):
```
66e6f79 Make the review decision path atomic, closing a concurrency gap Phase D left open
345033a Add multi-user review collaboration and activity history (Phase D)
4d72899 Add persistent invoice PDF storage behind a swappable local/S3 backend
147c0ce Migrate persistence from SQLite to PostgreSQL
cba2f01 Bring README and CLAUDE.md up to date with the frontend redesign
2a8f5c7 Add an explicit dark-mode toggle to the sidebar
```

Branch `main`, **5 commits ahead of `origin/main`, not yet pushed** (push
only if explicitly asked). Working tree has the uncommitted frontend work
described in §11 **plus all of Phase F** — verify with `git status` rather
than trusting this file if time has passed.

**[README.md](README.md)** is kept in sync with the code and is the other
primary reference — when it and this file disagree on a factual claim about
the code, verify against the code directly rather than trusting either.

### Before doing anything in a new session

1. Read this file, then `README.md`.
2. `git status` and `git log --oneline -10` — confirm nothing has moved
   since §11/§13 above were written.
3. Confirm `DATABASE_URL` is set and PostgreSQL is reachable.
4. `.\venv\Scripts\python.exe -m pytest tests\ -q` — expect 638 passed (or
   634 + the 4 known `test_extraction_routing.py` cases, see §10).
5. Ask what to work on next. Do not start Phase G or later without being
   asked (§2, §9).
