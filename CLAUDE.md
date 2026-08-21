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
  collaboration, email trusted-source verification (§7a), email invoice
  ingestion (§7b), and the derived-at-read-time KPI/analytics layer (§7c).
- **Frontend** (`frontend-next/`) — Next.js 15 / React 19 / Tailwind v4,
  served as a static export by FastAPI. **Has uncommitted redesign work in
  progress — see §11, read before touching any frontend file.**
- **Frontend fallback** (`frontend/`) — the original vanilla HTML/JS UI,
  kept as a no-build fallback if `frontend-next/out/` was never built.
- **`data/`** — seed POs, vendors, demo users (JSON, tracked in git,
  reloaded into Postgres on every startup) plus gitignored runtime state
  (`documents/`).
- **`tests/`** — 852 tests, 23 files, real (schema-isolated) PostgreSQL, both
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
| F | Email security & trusted-source verification | ✅ Complete | `d351869` |
| G | Email invoice ingestion & extraction | ✅ Complete | `8dfc286` |
| H | KPIs + analytics | ✅ Complete | (this phase) |
| I | Logs + filters + grouping + exports | ⬜ **Next — not started** | — |
| J | Client access / client portal | ⬜ Not started | — |
| K | Chatbot (read-only invoice/AP assistant) | ⬜ Not started | — |
| L | Multilingual support | ⬜ Not started | — |
| M | Final security + deployment hardening | ⬜ Not started | — |

**Do not start Phase I or any later phase without being explicitly asked.**
This project has been built one verified phase at a time, each requested
individually, each committed on its own before the next began. See §9 for
what H–M are planned to cover — plan only, nothing implemented.

**Do not redo A–H.** They are complete, tested, and committed. If something
in A–H looks wrong, raise it — don't silently "fix" or rebuild it.

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

email_attachments id (PK, SERIAL), email_id (FK → email_messages.id), seq,
                  filename, content_type, size_bytes, sha256,
                  is_invoice_candidate, status, skip_reason,
                  run_id (nullable FK → runs.id), run_status, error,
                  created_at, processed_at, storage_backend, storage_key
                  — one row per attachment, so ONE email can produce SEVERAL
                  invoice runs. A quarantined candidate's PDF is held in the
                  Phase C DocumentStore via storage_key (Phase G, §7b.6)

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
`email_messages(received_at)`, `email_activity(email_id)`,
**`UNIQUE email_messages(provider, provider_message_id)`** (Phase G's
idempotency mechanism, §7b.5), `email_messages(ingest_status)`,
`email_attachments(email_id)`, `email_attachments(run_id)`,
**`UNIQUE email_attachments(email_id, sha256)`**.

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

## 7b. Email invoice ingestion & extraction (Phase G)

**Status: implemented, tested (95 tests), verified, committed.**

Phase F answers *what can be proven about a message handed to us*. Phase G is
the part that **goes and gets one**, and connects it to the pipeline that
already existed. Keep the two straight: F is verification, G is ingestion.

### 7b.1 The flow, and why the order is the whole point

```
IMAP provider          fetch raw bytes + a stable message id
      v
idempotency check      one indexed lookup; a message seen before stops here
      v
parse headers/MIME     stdlib, cheap; NO attachment is opened
      v
TRIAGE                 cheap, deterministic, NO MODEL   <-- stops most mail
      v                (LOW / IRRELEVANT stop here, recorded and kept)
PHASE F verification    cryptography, microseconds
      v                (QUARANTINED stops here; Phase F's own hold)
attachment validation   magic bytes, size, dedupe -- first time bytes are read
      v
run_pipeline()          EXPENSIVE: OCR / LLM / PO match / decision / review
```

**Triage exists so the last stage is never reached by a newsletter.** A message
with no PDF and nothing invoice-shaped in the subject costs one header parse
and two dictionary lookups: no LLM, no OCR, no extraction quota, and not even a
signature verification. This is enforced structurally (`ingest_message()`
returns before the processing call) and **asserted directly**: the test suite
replaces `extraction.extract_invoice` with a spy and fails if a filtered
message ever reaches it.

### 7b.2 Two axes, deliberately not one score

`backend/email_triage.py` classifies every sender on two **independent** axes:

| | |
|---|---|
| `sender_type` | `CORPORATE` / `PERSONAL` / `UNKNOWN` — what kind of address |
| `trust_status` | `TRUSTED` / `UNTRUSTED` / `UNKNOWN` — whether we buy from them |

```
invoice@acme-office.example   CORPORATE + TRUSTED     (allowlisted)
supplier@gmail.com            PERSONAL  + UNKNOWN     (not "untrusted")
billing@never-heard-of.test   CORPORATE + UNKNOWN     (a company, just not one we know)
```

**A corporate sender is not automatically trusted** — a company domain costs an
attacker nothing to register. **An unknown sender is not automatically
hostile** — every genuine vendor was unknown once. **A personal address is not
automatically refused** — a small supplier really does invoice from Gmail, and
`PERSONAL + TRUSTED` is representable and tested.

`trust_status` is read from **Phase F's existing `trusted_email_senders`
allowlist**, which already links each entry to a `vendor_name`. There is no
second vendor list. `sender_type` comes from `data/email_domain_policy.json`
(corporate + free-mail domains), extendable through `EMAIL_CORPORATE_DOMAINS` /
`EMAIL_PERSONAL_DOMAINS`. Neither is hard-coded in business logic.

### 7b.3 Relevance, and the refusal to over-filter

`HIGH` / `POSSIBLE` proceed; `LOW` / `IRRELEVANT` stop. The asymmetry is
deliberate: a false "irrelevant" costs a missed invoice somebody has to chase,
a false "possible" costs one LLM call — so **every doubt resolves upward**.

A stopped message is **recorded, kept, and readable afterwards** with the
reasons that stopped it, and its attachments are listed. Nothing is deleted.
The claim triage makes is only ever *"not worth an LLM call without a person
asking"*, never *"not an invoice"*.

### 7b.4 The provider abstraction

`backend/email_provider.py` is the only module in the codebase that knows a
mailbox exists. Everything downstream takes raw RFC 5322 bytes and an id.

- **`ImapEmailProvider`** — a real, working client on the stdlib's `imaplib`.
  **No new dependency.** Always TLS (`IMAP4_SSL`), no plaintext option.
  Authenticates with **SASL XOAUTH2 when `EMAIL_IMAP_OAUTH_TOKEN` is set**, and
  falls back to a password only when it is not — a scoped short-lived token
  beats a long-lived mailbox password in an environment variable. Gmail and
  Microsoft 365 both accept this.
- **`NullEmailProvider`** — the default. Ingestion off, nothing polled, no
  outbound connection. Not a placeholder: it is the correct behaviour for an
  install that does not want email.

There is **no mock provider in `backend/`** — test doubles live in the test
file, where no production configuration can select them.

### 7b.5 Idempotency — the database, not Python

**`UNIQUE (provider, provider_message_id)`** on `email_messages`, partial so
Phase F rows with a NULL provider do not collide.

Not a check-then-insert: two pollers, or two uvicorn workers, would both pass
that before either wrote. The database refuses the second `INSERT` however the
race is timed, and the loser reads its own answer back and reports a duplicate.
Proved under 8 real threads racing one message — exactly one record, at most
one run.

`provider_message_id` prefers the RFC 5322 `Message-ID` over the IMAP UID,
because a UID is unique only within one folder on one server: a message moved
between folders, or a mailbox restored from backup, would otherwise reprocess.

A second unique index, `(email_id, sha256)` on `email_attachments`, means the
same PDF attached twice is stored and processed once, and a redelivery cannot
append a second copy of every attachment.

`mark_handled()` (the IMAP `\Seen` flag) is **an optimisation, never
correctness** — and it is set only *after* the outcome is committed, so a crash
mid-processing leaves the message to be re-offered and refused by the
constraint.

### 7b.6 One email is not one invoice

`email_attachments` is one row per attachment with its own status and its own
nullable `run_id` — the same shape of problem `runs.po_number` had before
`run_allocations`, solved the same way. So:

- an email with three invoices produces three runs;
- an attachment that failed can be retried without reprocessing the ones that
  succeeded (a `PROCESSED` row is skipped before its bytes are even read);
- a deliberately skipped attachment records **why**, so "we ignored your
  logo.png" is answerable later;
- one corrupt PDF does not stop the good invoice beside it in the same email.

`email_messages.run_id` still holds the *first* run, so Phase F's single-run
link keeps working unchanged.

### 7b.7 Phase F cannot be bypassed

`process_message_attachments()` re-reads the **stored** security status and
refuses anything that is not `ADMITTED` or `RELEASED`. It reads the database
row, not a value the caller passed, so the gate holds across restarts, across
processes, and regardless of how the function was reached. There is no argument
that skips it. Both the quarantined and discarded cases are tested by calling
the function directly and asserting the extraction spy stayed at zero.

**Quarantine reuses Phase F entirely** — same statuses, same
`/release` and `/discard` endpoints, same `invoice:review` scope. No second
quarantine system.

**A quarantined message's PDFs are preserved**, in the existing Phase C
`DocumentStore`, keyed by a server-generated key exactly as invoice documents
are — so releasing a message later actually has something to process, instead
of making the vendor resend. The **message body is still never stored**; only
the attachment, which is the invoice itself. Once an attachment becomes a run,
the run's own document row owns a copy and the holding copy is deleted.

### 7b.8 One pipeline, two doors

`_run_invoice_pipeline()` consumes **`main.run_pipeline`** — the same async
generator the browser drives — and reads the `final` frame it already emits.
Every stage, the audit trail, the confidence gate, PO matching, the allocation
ledger, document persistence and review routing are the ones a manual upload
gets, because they *are* the ones a manual upload gets. `source="EMAIL"` is the
value `config.DOCUMENT_SOURCES` has recognised since Phase C and that nothing
wrote until now.

A test asserts both doors produce runs with identical stage lists and identical
audit-trail keys.

### 7b.9 Failure handling

Every failure is recorded and visible; nothing is silently dropped.

| Failure | Behaviour |
|---|---|
| Provider unreachable / auth failure | `poll_once` returns `ok: False` with the reason; the endpoint answers **502**. Never an empty poll, which would look like "no new mail" |
| Malformed message / MIME | Recorded with `ingest_status=FAILED`; the batch continues |
| Oversized message | Recorded as `FAILED` with the size and the limit |
| Corrupt PDF | That **attachment** fails; other attachments in the same email still process |
| Attachment unreadable on release | `FAILED` with a distinct reason — our problem, not the sender's |
| Database unavailable while recording | Reported, message left unmarked so the next poll re-offers it |
| Pipeline crash | Caught per attachment, logged, recorded; loop continues |

### 7b.10 Endpoints (no new scope)

```
GET  /api/email/ingestion                   [invoice:admin]    config + counts
POST /api/email/ingestion/poll              [invoice:process]  run one pass now
POST /api/email/messages/{id}/process       [invoice:process]  process an admitted/released message
GET  /api/email/messages/{id}/attachments   [invoice:read]     what arrived, what it became
```

`/api/email/ingestion` is admin-scoped because it describes the mailbox
connection, and it reports only whether a credential is **present** — never the
password, never the token. A test greps the response body for both.

**Release does not auto-process.** Phase F's `/release` means exactly what it
meant before; `/process` is the explicit, separately-audited follow-up. That
keeps the security model unchanged rather than quietly widening it.

### 7b.11 Deployment

The poller is an in-process asyncio task started at FastAPI startup when
`EMAIL_INGEST_ENABLED=1` **and** a provider is configured, and cancelled on
shutdown. It runs each pass in a worker thread (`asyncio.to_thread`), so
blocking socket I/O and an LLM call never stall the event loop and every HTTP
request with it. **No manually-run local script is involved.** Running several
uvicorn workers is safe — see §7b.5.

### 7b.12 Known limitations

1. **IMAP is the only implemented provider.** The abstraction is real and a
   second provider is a new class, but Gmail API and Microsoft Graph are *not*
   implemented and are not claimed.
2. **Polling, not webhooks.** IMAP has no webhook; `IDLE` is not implemented
   either, so the worst-case latency is one `EMAIL_POLL_SECONDS` interval.
3. **OAuth tokens are consumed, not obtained.** `EMAIL_IMAP_OAUTH_TOKEN` is
   read from the environment; there is no refresh-token flow, so a short-lived
   token must be refreshed by whatever issues it.
4. **PDF only.** The extraction pipeline reads PDFs, so other formats are
   recorded and skipped with a reason rather than half-processed.
5. **No frontend.** These endpoints have no UI, the same restriction Phases
   D, E and F worked under (§11).
6. **The relevance filter is a heuristic**, deliberately biased toward
   processing. It can pass junk through to an LLM call; it is built not to stop
   a real invoice, and stopped messages are kept and re-runnable either way.
7. **`test_email_ingestion.py` needs no network**, but its happy path signs
   messages with a real generated key — an unsigned message is correctly
   quarantined by Phase F and never reaches the pipeline.

**Two code comments are now out of date and were deliberately left alone**
(they are comments only — no behaviour depends on them, and changing them
would have put unrelated edits in the Phase G commit). Both predate the
lettered phase tracks and refer to a "Phase J" that no longer means anything:

- `backend/config.py` (~line 291) — "when ingestion (Phase J) adds a second
  producer"
- `backend/main.py` (~line 547) — "for when Phase J's ingestion path exists,
  but nothing writes it yet"

Phase G *is* that ingestion path, and it **does** now write
`source="EMAIL"`. Worth correcting the next time those files are touched for
another reason.

## 7c. KPIs & analytics (Phase H)

**Status: implemented, tested (119 tests), verified.**

Phases A–G built the machine. Phase H answers **how well it is actually
working** — from the rows the application already keeps, and from nothing else.

### 7c.1 The one rule this phase is built on

**Everything is a query. Nothing is a stored number.**

`backend/analytics.py` writes nothing. There is no `analytics` table, no
`kpi_daily` rollup, no `runs.is_automated` flag, no `total_approved` counter.
Every figure is aggregated at read time from `runs`, `invoice_activity`,
`review_claims`, `run_allocations`, `email_messages` and `email_attachments`.

This is the third time this project has made that choice, and for the third
time it is the same reason:

| | Stored counter | Derived at read time |
|---|---|---|
| PO balance (§3) | rejected twice | ✅ `SUM(run_allocations)` joined to APPROVED |
| Review claim holder (§6.2) | rejected | ✅ most recent unreleased, unexpired row |
| **Every KPI (this phase)** | **rejected** | ✅ **aggregated per request** |

A counter is authoritative, so the moment one code path forgets to bump it the
number is wrong and nobody notices. A derived figure cannot drift from the rows
it is derived from, because it *is* the rows.

**The only schema change Phase H makes is four indexes** (§7c.14), which is the
sanctioned way to make a derived figure cheap. A test asserts the schema gained
no table.

### 7c.2 Where the numbers come from — two mechanisms, chosen per metric

1. **SQL aggregation** for anything expressible in real columns: counts, rates,
   date buckets, latencies, per-vendor and per-PO groupings. `_run_counts()` is
   one query with twelve `FILTER`ed aggregates rather than twelve queries — the
   alternative is a dozen numbers that were each true at a slightly different
   instant.

2. **One guarded Python pass** (`_scan_run_json`) for the metrics that live
   inside the JSON columns: per-stage timings (`stages_json`), which rules
   failed (`audit_json`), the extraction route, and invoice value by currency.

   **Why not SQL for those too:** `stages_json` and `audit_json` are TEXT, not
   JSONB, and Postgres has no total `try_cast` to jsonb. One malformed blob
   would abort the whole aggregate query and take a working dashboard down.
   The Python pass skips that row, counts it in `data_quality.malformed_json`,
   and reports every other run normally. It is also ONE query serving four
   breakdowns, so the row-scan is paid once per request rather than per metric.
   Tested directly, including valid-JSON-of-the-wrong-shape and a `True` that
   must not be accepted as a millisecond count.

   **The cost is stated, not hidden:** this pass reads the JSON of every run in
   the window. At this volume that is the right trade; at a much larger one the
   answer is a JSONB column with a GIN index, which is a self-contained change
   to that one function.

### 7c.3 The KPI definitions — read these before quoting a number

Each KPI ships its **numerator, denominator and definition string** in the API
response, so a rate can always be checked against the counts under it and no
client has to hard-code what a metric means.

**A rate with an empty denominator is `null`.** Never `0.0`, never `100%`.
"No invoices were processed" and "0% were automated" are different statements
and only one of them is true on a quiet day. This is asserted throughout the
test file, and a mutation making `_rate` return `0.0` breaks exactly four tests.

| KPI | Numerator | Denominator |
|---|---|---|
| **Automation rate** | `automated_decision` in {APPROVED, REJECTED} | all runs in window |
| **Processing success rate** | runs whose extraction produced a usable route | all runs in window |
| **Task success ratio** | resolved **and** not overridden | all runs in window |
| **Human review rate** | `automated_decision` = NEEDS_REVIEW | all runs in window |
| **Review completion rate** | held runs carrying a `human_decision` | held runs |

**AUTOMATION RATE — how much work the rules disposed of unaided.**
A REJECTED run *counts as automated*: correctly stopping a duplicate is the
process working, not failing. Computed from `automated_decision`, which is
immutable (§3), so a later human ruling cannot retroactively change how
automated the process was at the time — tested directly.

**PROCESSING SUCCESS RATE — a machinery metric, not a business one.**
Whether the pipeline could read the document at all. It says *nothing* about
whether the invoice was approved: a correctly rejected duplicate is a
processing **success**, and an unreadable scan held for a human is a processing
**failure** even though the hold was the right response to it. Detected from
the `extraction_method` the pipeline wrote (`"none"` on main.py's
`_abort_unreadable` path), never inferred from fields being absent.

The two are kept apart on purpose. A "success rate" that collapses "the machine
worked" into "the invoice was approved" is the single most misleading number an
AP dashboard can show, and it is the default one to build by accident.

**TASK SUCCESS RATIO — did the work finish, by the route it was meant to?**

```
resolved     the run waits on nobody: its automated decision was terminal,
             OR a person has ruled on it
overridden   an administrator changed its status outside the review path
             (a STATUS_OVERRIDDEN event in invoice_activity)

numerator = resolved − overridden
```

Genuinely distinct from automation rate, and the distinction is the point: a
held invoice a reviewer then accepted is **not automated** but **is** a task
success — the process invited a person, a person came, the work finished. An
administrator reaching past the process to correct a decision is not, even
though the run ends terminal either way. A test asserts the two metrics
diverge, so neither is a redundant restatement of the other.

> **THIS MEASURES OPERATIONAL SUCCESS, NOT CORRECTNESS.** The database holds no
> independent record of what the right answer was — no ground-truth label, no
> downstream payment confirmation — so nothing in this phase claims a decision
> was correct. It claims the work finished. The definition string says exactly
> that, travels with every response, and a test asserts the words are in it.

**REVIEW EFFECTIVENESS** is reported the same way. A hold a reviewer ACCEPTED
means the reviewer judged the invoice fine after all; a hold they REJECTED
means they judged the concern real. Neither is reported as the hold having been
"right" — the definition strings say `NOT evidence the hold was wrong` /
`NOT evidence the hold was right`, and a test greps for those words and for the
absence of any correctness claim. The full
`automated_decision` × `human_decision` × `status` × `final_decision`
transition matrix is returned alongside, as rows rather than a nested object,
so a combination nobody anticipated shows up instead of being dropped.

### 7c.4 Time — everything is UTC, and the response says so

Every timestamp this application writes is
`datetime.now(timezone.utc).isoformat()`: ISO-8601, UTC, explicit `+00:00`,
stored as TEXT. Because **every** writer uses that one call the strings compare
correctly with `>=` / `<`, which is already how `get_active_claim` tests a lease
expiry (§6.2). Date windows are therefore half-open ISO string ranges
`[start, end)` — index-servable with no cast.

- Ranges: `today` · `7d` · `30d` · `month` · `all` · `custom` (`from`/`to`).
- `custom` **includes the day named by `to`** (`from == to` is one full day).
- Day buckets are **UTC calendar days**, matching how `quota.py` already
  reckons the extraction budget. Responses carry `"timezone": "UTC"` so a
  dashboard labels its axis honestly. The bucket key is
  `substring(created_at from 1 for 10)` — the stored value's first ten
  characters *are* its UTC date, so there is no cast and no conversion to get
  wrong.
- An unrecognised range **raises** rather than falling back to a default: a
  typo in `range` silently becoming "all time" would answer a question nobody
  asked.
- An unbounded (`all`) window contributes no SQL and no parameters — never a
  sentinel epoch date, which would quietly exclude anything older than it. For
  trends, `all` starts the series at the first run rather than scanning to the
  beginning of time.

Boundary handling is tested directly, including a run written at exactly UTC
midnight (it belongs to the day that *starts* then) and one a microsecond
before it.

### 7c.5 Endpoints — no new scope

```
GET /api/analytics/overview     [invoice:read]   headline KPIs, decision mix, value, backlog
GET /api/analytics/trends       [invoice:read]   one row per UTC day
GET /api/analytics/processing   [invoice:read]   run + per-stage timing, routes, quota
GET /api/analytics/reviews      [invoice:read]   review funnel, latency, effectiveness (aggregate)
GET /api/analytics/vendors      [invoice:read]   per-vendor + every PO's budget position
GET /api/analytics/email        [invoice:read]   the ingestion funnel
GET /api/analytics/users        [invoice:read]   YOUR OWN row — or everyone's, with invoice:admin
```

All share `?range=` / `?from=` / `?to=` through one FastAPI dependency
(`main.analytics_window`), so every route rejects a bad range identically.
`range` and `from` are taken through aliases because one shadows a builtin and
the other is a Python keyword.

**Aggregate analytics need `invoice:read`** — the same scope that already reads
a run, its audit trail and its stored document. A dashboard saying 12 invoices
were held is derived from rows that caller can already fetch one at a time, so
demanding more would be theatre.

**`/api/analytics/users` is the exception, and it is authorised differently
because it is the only endpoint about PEOPLE rather than invoices.** It returns
the caller's own row unless they hold `invoice:admin`, in which case it returns
the whole team — the same "your own, unless you are an administrator" shape
`/review/release` already uses (§6.2). The response carries
`scope: "self" | "all"` so a client never infers it from the row count, and the
decision is made from the authenticated principal, **never** from a query
parameter (a `?user=` filter would be an authorization check the caller
performs on themselves).

> **A stated limitation, not a workaround.** The existing model (§8) has four
> scopes and **no `manager` role**. The honest choice was between
> `invoice:review` — which every peer reviewer holds, and which would therefore
> expose each of them to all the others — and `invoice:admin`, the only scope
> that denotes authority *over* the review process rather than participation in
> it. `invoice:admin` it is. A fifth scope was deliberately not invented:
> Phases F and G both added endpoints without adding scopes, and an
> `analytics:people` scope needs a role to carry it, which means editing every
> deployment's user store for a reporting screen.

Both directions are tested, including that a **reviewer** is not an
administrator for this purpose.

### 7c.6 What analytics deliberately do not disclose

- **No invoice contents.** No line items, no `extracted_json`, no `raw_text`,
  no raw `audit_json` blob, no `provenance`. Tested by grepping every
  endpoint's response body.
- **No document location.** No `storage_key`, no `storage_backend` — the same
  restriction the Phase C document endpoints already observe (§5).
- **No email contents.** The email funnel reports counts and statuses only: no
  sender address, no domain, no subject. Phase F never stored the body and this
  phase does not start. Tested by seeding a message with a distinctive
  address and subject and asserting neither appears.
- **No database internals.** `data_quality.malformed_json` is keyed by what the
  value means (`stages`, `audit`, `extracted`), not by the column it lives in.
- **No injectable identifier.** Window bounds are always bind parameters. The
  COLUMN a window filters on is interpolated (SQL cannot bind an identifier),
  and every call site passes a hard-coded literal — enforced by a regex check
  in `Window.clause()` and `email()`'s `by()` helper, so a future edit that
  threads a request value in there fails loudly instead of becoming an
  injection point. Tested with hostile column names.
- **Read-only.** Nothing under `/api/analytics` writes. Asserted by
  snapshotting every decision-bearing column plus the activity and allocation
  row counts, calling all seven services, and requiring the snapshot identical.
  Every endpoint also 405s on POST.

### 7c.7 Money is never summed across currencies

`runs.total` is in the invoice's own currency, so `overview` reports
**`value_by_currency`** — a bucket per currency — and there is deliberately no
combined `value_processed` field anywhere for a reader to misinterpret. Adding
1,000 EUR to 1,000 USD produces a number that is not an amount of anything.
Tested with a mixed-currency window, including an assertion that no combined
total exists.

*(The pre-existing `frontend-next/lib/metrics.ts` `totals()` — the Overview
page's own client-side summary — does sum `r.total` across runs regardless of
currency. That is a pre-existing approximation on a different screen, left
alone; the Analytics screen does not repeat it.)*

### 7c.8 PO figures reuse the ledger — and a test proves they agree

`storage.consumed_amounts_by_po()` was added **immediately beside `_consumed`**
so the two expressions of the ledger rule (sum `run_allocations`, joined to
`runs.status = 'APPROVED'`) cannot be edited apart by someone who found only
one of them. The set-based version exists because an analytics table needs all
POs at once and calling `_consumed` per PO is a query per row.

Adjacency is a convention, so it is backed by a test:
`test_set_based_ledger_matches_the_per_po_ledger` asserts the two agree for
**every** PO on a database containing a multi-PO invoice, a duplicate rejection
and a reversal. Downgrading the set-based query to ignore `status='APPROVED'`
breaks exactly that test and one other — verified by mutation.

**PO consumption is deliberately NOT windowed.** A remaining balance "as of the
last 30 days" is meaningless to anyone about to approve an invoice against that
PO. The balances are all-time (the ledger's own); the invoice *counts* beside
them are windowed and named `*_in_range`. Both halves are tested, as is the
property that a reversal refunds the analytics view in the same instant —
because nothing was ever deducted.

### 7c.9 One window means two different things — deliberately

Every endpoint windows on **when the invoice arrived** (`runs.created_at`),
because they all ask about a cohort of invoices: "of the work that entered last
week, how much was automated".

**`users()` is the single exception: it windows on `runs.reviewed_at`**, because
it asks about work a *person did*, and "your workload this week" plainly means
the decisions you made this week — not the decisions you made about invoices
that happened to arrive this week. Windowing it on `created_at` reports a
reviewer who spent today clearing a month-old backlog as having done nothing,
which is both wrong and the case a review queue produces most often. It also
keeps those counts consistent with the per-person activity counts beside them,
which are windowed on when the event happened.

**This was found by looking at the rendered dashboard, not by a test** — the
Today range showed three reviewers with zero reviews each despite visible
activity. It now has one
(`test_reviewer_workload_is_windowed_by_when_the_work_was_done`).

### 7c.10 Latency is measured two ways, and says which it could not measure

- **`time_to_decision`** — `runs.created_at` → `runs.reviewed_at`. Available
  for every reviewed run. Answers "how long did the vendor wait".
- **`handling_time`** — first `review_claims.claimed_at` → `runs.reviewed_at`.
  Answers "how long did the reviewer take once they picked it up", and is
  available **only for runs that were claimed**. Claiming is optional (§6.4),
  so the count it cannot measure is reported as `unclaimed_reviews` rather than
  averaged in as zero. Tested both ways, plus the null-`reviewed_at` case.

Both use `percentile_cont` in SQL for the median. Timing statistics always
carry a `samples` count, so a caller can tell an average of zero (every
measurement really was zero) from no average at all (nothing was measured).

The backlog block reports what is open **right now** — `claimed_now` is derived
exactly the way `get_active_claim` derives it (most recent unreleased row per
run, lease unexpired), and is deliberately *not* windowed: who is holding a
claim is a fact about this moment, not about the reporting range.

### 7c.11 Hold reasons group by rule name, not by reason sentence

`audit_json.rules_failed` is a list of **rule names** — a fixed, hand-written
vocabulary from `rules.py` ("PO remaining check", "Duplicate check", "Vendor
approved", …). The reason *sentence* embeds the invoice's own amounts and
numbers, so grouping by it produces a list of individual invoices rather than a
list of causes. Tested with two invoices of different amounts failing the same
rule: they group to one row, and neither amount leaks into the key.

A run failing three rules contributes to three rows, so these counts sum to
more than the run count. The API says so and the dashboard prints it, because a
table of them looks like it should sum to the total.

### 7c.12 Extraction usage — counts, never invented cost

`extraction_quota` records **requests per provider per day** and nothing else:
no token counts, no price table, no provider invoice. So the payload reports
used/limit/remaining/utilisation, sets **`cost_available: false`**, and carries
a note saying a spend figure cannot be derived from this application's data.
Tested, including a grep asserting no `cost`/`spend`/`price` field exists.

`extraction_quota` is created lazily by `quota.py` on first use, so a database
that has never run an extraction genuinely has no such table. That reads as
"nothing has been extracted yet", not as a failure.

### 7c.13 Frontend

A new **Analytics** section (its own "Reporting" nav group), integrated into the
existing redesign rather than replacing any of it — it reuses `Panel`,
`PanelHeader`, `DataTable`, `Meter`, `Segmented`, `Tooltip`, `EmptyState`,
`Badge`, `VolumeChart`, `LegendItem` and the existing colour tokens. Two chart
primitives were added to the existing `charts.tsx` (still no charting
dependency): `RateTrend` and `SplitBar`.

**Every figure on that screen is computed by the server.** The page formats and
arranges; it calculates no KPI, so the browser cannot show a number that
disagrees with what an auditor would get from the database. (Contrast
`lib/metrics.ts`'s `totals()`, which is the *Overview* page's own client-side
summary of runs it already holds — a different job, kept separate.)

**Three display states, not two** (`lib/metrics.ts`'s `kpiState`):

| State | When | Rendered as |
|---|---|---|
| `ok` | denominator ≥ 5 | the figure |
| `insufficient` | a real rate over fewer than 5 runs | the figure, plus "Only 2 invoices — too few to read as a rate" |
| `unavailable` | denominator is 0 | `—` plus "No invoices in this period" |

The middle state is the one that matters: "100% automated" over two invoices is
arithmetically true and operationally meaningless, and rendering it identically
to 100% over two thousand is misleading whether or not any single number is
wrong. Verified visually on the `Today` range.

`RateTrend` **refuses to draw a null as zero**: a day with no invoices had no
automation rate, so the line breaks into segments and the readout says "no
invoices that day". Coercing null to 0 would draw a cliff to the floor and back
— a statement the data does not make.

Files touched: **new** `components/pages/AnalyticsPage.tsx`; **Phase-H-only
edits** to `lib/types.ts`, `lib/useData.ts`, `lib/metrics.ts`,
`components/ui/icons.tsx`; **Phase-H edits inside files the redesign had
already modified** — `components/charts.tsx` (a pure append),
`components/layout/AppShell.tsx` (the nav row and `Section`/`NavId` unions) and
`app/page.tsx` (routing the section). See §13 for what that means for
committing.

The dashboard was verified end to end against a seeded throwaway Postgres
schema holding 90 runs, 41 held, 31 ruled on across three reviewers — empty
states, the insufficient-sample guard, the self-scope reviewer view and the
over-budget PO indicator all render correctly. **The developer's own `public`
schema was not touched**, and was confirmed unchanged afterwards.

### 7c.14 Schema changes — four indexes, nothing else

Added in `init_db()` beside the existing index block:

```
idx_runs_created_at      runs(created_at)        every analytics query filters on it;
                                                 nothing indexed it before
idx_runs_vendor_name     runs(vendor_name)       per-vendor grouping
idx_runs_reviewed_by     runs(reviewed_by)       per-reviewer grouping
idx_activity_actor       invoice_activity(actor) per-person activity counts
```

No table, no column, no counter — asserted by
`test_the_new_indexes_exist_and_the_schema_gained_no_table`.
`EXPLAIN (ANALYZE, BUFFERS)` over 5,000 runs confirms `idx_runs_created_at` is
used (Bitmap Index Scan; the 30-day window query executes in **0.3 ms**), and
every endpoint returns in **under 11 ms** at that volume.

### 7c.15 Known limitations

1. **No ground truth, therefore no correctness metric.** Nothing here can say a
   decision was *right* — only what was decided and whether the work finished.
   Stated in the payload, not only in this file (§7c.3).
2. **No monetary cost.** Request counts only (§7c.12).
3. **The JSON pass reads every run in the window** (§7c.2). Fine at this
   volume; the remedy at a larger one is a JSONB column, and it is a
   self-contained change to one function.
4. **No `manager` role exists**, so team-wide reviewer figures require
   `invoice:admin` (§7c.5). This is the existing model's limit, reported rather
   than bypassed.
5. **Trends are daily buckets only** — no weekly or monthly rollup, and a range
   wider than 400 buckets is refused rather than truncated.
6. **No CSV/export**, and no per-run drill-down from a chart. Both are Phase I
   (logs, filters, grouping, exports), deliberately not started.
7. **`by_route` counts the extraction method the run recorded**, which for runs
   written before that field existed may be absent; those are reported as
   `(unrecorded)` rather than guessed at.
8. **Vendor and PO breakdowns group by the stored `vendor_name` string**, not
   by a normalised vendor identity — so two spellings of one vendor are two
   rows. `storage.normalize_vendor_name()` exists and could be applied here,
   but doing so would make the analytics grouping disagree with what the
   Invoices table displays, which is a change worth making deliberately rather
   than as a side effect of this phase.

---

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

## 9. Roadmap — Phase I and beyond

**Phases F, G and H are done and are recorded here as markers only** (§7a, §7b
and §7c are the authority on what they actually do). **Everything from I onward
is a plan and nothing in it is implemented.**

### Phase F — Email security & trusted-source verification (DONE)

**Implemented, tested and verified — see [§7a](#7a-email-security--trusted-source-verification-phase-f)
for what it actually does, and §7a.10 for what it deliberately does not.**
This roadmap entry is kept only as a marker; §7a is the authority. Note that
the original plan listed "SPF verification" as ordinary scope — that turned
out to be impossible to compute from a stored message, and §7a.3 records the
reason rather than the plan pretending otherwise.

### Phase G — Email invoice ingestion & extraction (DONE)

**Implemented, tested and verified — see [§7b](#7b-email-invoice-ingestion--extraction-phase-g)
for what it does, and §7b.12 for what it deliberately does not.** This entry is
a marker only; §7b is the authority.

### Phase H — KPIs & analytics (DONE)

**Implemented, tested and verified — see [§7c](#7c-kpis--analytics-phase-h) for
what it does, and §7c.15 for what it deliberately does not.** This entry is a
marker only; §7c is the authority.

Two things the original brief asked for came out differently, and are recorded
here rather than quietly dropped:

- The brief described success-rate KPIs partly in terms of **"how often the
  hold was the right call"** and a task-success ratio over outcomes that were
  **"correct-looking"**. Neither is derivable: this database holds no
  ground-truth label and no downstream payment confirmation, so nothing can
  establish that a decision was *right*. The metrics were defined against what
  the rows can actually prove — what was decided, and whether the work finished
  by the route it was meant to — and every response says so in its own
  definition string (§7c.3). The alternative would have been a
  confident-sounding number measuring nothing.
- The brief listed **manager-visible** analytics as one of the role tiers. No
  `manager` role exists in the scope model (§8), so team-wide per-person
  figures were placed behind `invoice:admin` and the limitation is stated
  (§7c.5) rather than worked around by inventing a fifth scope.

**The trap the brief named was avoided:** no counter column and no summary
table were added. The only schema change is four indexes (§7c.14), and a test
asserts the schema gained no table.

### Phase I — Logs, filtering, grouping & exports (NEXT, not started)

**Nothing is implemented. This is the brief, recorded so the next session
starts from the right place.**

**Purpose:** make the history already on file searchable, groupable and
extractable, for the people who have to answer a specific question about a
specific invoice rather than read a dashboard.

**Planned scope:**
- **Logs** — a queryable view over `invoice_activity` and `email_activity`
  (both already append-only, both already carry actor, event type and
  timestamp), and over the per-run stage log in `runs.stages_json`.
- **Filtering** — by date range, decision, status, vendor, PO, reviewer,
  source (`MANUAL_UPLOAD` / `EMAIL`), and rule that failed.
- **Grouping** — the same axes Phase H aggregates on, but returning the ROWS
  behind a figure rather than the figure. This is the natural drill-down that
  §7c.15 lists as deliberately absent from the analytics screen.
- **Exports** — CSV at minimum. Note that an export leaves the application and
  the authorization boundary with it, so exports must carry the same scope
  rules the read endpoints do, and a per-person export must respect the
  `users()` restriction in §7c.5 rather than becoming a way around it.

**What already exists to build on:**

| Need | Where it already is |
|---|---|
| what people did, append-only | `invoice_activity`, `email_activity` |
| per-run stage log | `runs.stages_json` (name, status, detail, ms) |
| why a verdict was reached | `runs.audit_json` (`rules`, `rules_failed`, `reason`) |
| validated time windows | `analytics.resolve_window()` — reuse it, do not write a second date parser |
| safe JSON reads | `analytics._loads()` — the guarded parse (§7c.2) |
| date-range indexes | `idx_runs_created_at`, `idx_activity_created_at`, `idx_activity_actor` |

**The traps to avoid:**
- **Do not write a second time-window parser.** `analytics.resolve_window()` is
  already validated, already half-open, already UTC, and already tested against
  boundary cases. A filter panel that parses dates its own way will disagree
  with the dashboard beside it.
- **Do not let an export widen authorization.** The most likely accidental leak
  in this phase is a CSV of reviewer activity available to anyone holding
  `invoice:read`.
- **Do not paginate by OFFSET over a growing table** without an index that
  supports the sort; `list_runs()` currently caps at 200 rows for exactly this
  reason (§6.3).

### J–M

A client-facing portal, a read-only AP chatbot, multilingual support, and a
final security/deployment hardening pass — all unstarted, all deferred until
asked for individually.

---

## 10. Testing

**852 tests, 23 files.** Both Groq and Gemini mocked at the HTTP transport
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
| `test_analytics.py` | 119 | Phase H: every KPI against known rows, the null-not-zero rule, task-success vs automation-rate divergence, per-stage timing and bottleneck ordering, both review latencies, date windows and UTC boundaries, trends with gaps, malformed/wrong-shaped JSON, the ledger-agreement anti-drift test, the email funnel, per-person authorization from both sides, read-only-ness, and no-leak greps |
| `test_email_ingestion.py` | 95 | Phase G: sender/relevance triage, the no-LLM-for-junk guarantee (an extraction spy), provider failure, idempotency under 8 threads, attachment validation & path traversal, multi-invoice emails, the Phase F gate, quarantine→release→process, authorization, backwards compatibility |
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

**Verified state at the end of Phase H** (2026-08-21).
`tests/test_analytics.py` alone: **119 passed.**

| Run | Result |
|---|---|
| **Baseline**, before any Phase H change, tree at `8dfc286` | 733 tests — **728 passed, 5 failed** |
| **After Phase H** | 852 tests — **848 passed, 4 failed** |

The 4 remaining failures are exactly the pre-existing
`test_extraction_routing.py` cases described below, and that file still passes
**23/23 when run alone**. The 5th baseline failure
(`test_samples.py::test_sample_invoice[05_scanned_no_text.pdf]`) is the
live-Gemini free-tier quota condition documented further down; it passed on the
post-Phase-H run, which is the flakiness that entry already predicts rather
than anything Phase H did.

848 − 728 = 120 = the 119 tests Phase H added, plus that one recovered sample.
**No Phase A–G test changed behaviour.**

Those 119 were checked against passing vacuously by mutation — four
mutations, each breaking exactly the tests that should break, all reverted
and re-verified green:

| Mutation | Broke | Correct? |
|---|---|---|
| `_rate()` returns `0.0` instead of `None` for an empty denominator | 4 tests (the empty-data / quiet-day assertions) | ✅ |
| `users()` ignores `see_everyone` and always returns everyone | 4 tests (both authorization directions, service and HTTP) | ✅ |
| `consumed_amounts_by_po()` drops `status='APPROVED'` | 2 tests (ledger agreement, reversal refund) | ✅ |
| automation rate counts held runs as automated | 7 tests (automation, review-rate complement, task success) | ✅ |

**One real design flaw was found by looking at the rendered dashboard rather
than by a test**, and fixed: reviewer workload was windowed on when the
INVOICE arrived, so a reviewer who spent today clearing a month-old backlog
read as having done nothing. It now windows on `reviewed_at` (§7c.9) and has
a test.

**Concurrency tests use real threads against real PostgreSQL** — a
`threading.Barrier` so every thread starts simultaneously, then asserts the
outcome (exactly one winner, the balance never over budget), not the
mechanism. Not mocked, not sleep-based.

**Verified state at the end of Phase G** (2026-08-21). `tests/test_email_ingestion.py`
alone: **95 passed.** Full suite with `GEMINI_API_KEY`/`GROQ_API_KEY` unset:
**729 passed, 4 failed** — the 4 being exactly the pre-existing
`test_extraction_routing.py` cases below, which pass **23/23** when that file
runs alone. No Phase A–F regression.

Those 95 were checked against passing vacuously by mutation: disabling the
relevance filter broke exactly the 4 cheap-filter tests, removing the Phase F
gate broke exactly the 2 bypass tests, and downgrading the idempotency index
from UNIQUE broke exactly the 8-thread race. All reverted and re-verified.

Two real bugs were found by these tests during development and fixed: an
already-processed attachment was being re-read on a second pass and marked
FAILED (overwriting a good result), and `ingest_message` returned without a
`duplicate` key on one branch.

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
in the working tree, and as of Phase H there is now Phase H frontend work
sitting in the same tree alongside it.** The redesign predates Phases C–G,
none of which touched it (each was scoped backend-and-tests-only). **Do not
revert, discard, reformat, or commit the redesign as part of an unrelated
change.** Verify with a fresh `git status` before assuming any of the below is
still accurate.

### 11.1 The pre-existing redesign (NOT Phase H — do not commit)

A redesign toward a light-first enterprise finance interface with an explicit
dark-mode toggle (`:root[data-theme="dark"]`, never `prefers-color-scheme`);
`RunDetail.tsx` was split into `DocumentPreview.tsx` + `ReviewWorkspace.tsx`.

```
modified:   frontend-next/app/globals.css
modified:   frontend-next/app/page.tsx                        ← also has Phase H edits
modified:   frontend-next/components/charts.tsx               ← also has Phase H edits
modified:   frontend-next/components/invoice/Panels.tsx
modified:   frontend-next/components/invoice/PoMatchPanel.tsx
deleted:    frontend-next/components/invoice/RunDetail.tsx
modified:   frontend-next/components/invoice/StageList.tsx
modified:   frontend-next/components/layout/AppShell.tsx      ← also has Phase H edits
modified:   frontend-next/components/pages/InvoicesPage.tsx
modified:   frontend-next/components/pages/OverviewPage.tsx
modified:   frontend-next/components/pages/ProcessPage.tsx
modified:   frontend-next/components/pages/ReferencePage.tsx
modified:   frontend-next/components/ui/index.tsx

untracked:  frontend-next/components/invoice/DocumentPreview.tsx
untracked:  frontend-next/components/invoice/ReviewWorkspace.tsx
untracked:  claudee.md   (stray file at repo root — not part of the app; leave as-is unless asked)
```

### 11.2 Phase H frontend work (§7c.13), and how it overlaps

Phase H was the first phase asked to do frontend work, so it is the first time
the two are interleaved. Three categories:

| Category | Files | Safe to commit alone? |
|---|---|---|
| **New, Phase H only** | `components/pages/AnalyticsPage.tsx` | ✅ yes |
| **Modified, Phase H only** — these were *untouched* by the redesign | `lib/types.ts`, `lib/useData.ts`, `lib/metrics.ts`, `components/ui/icons.tsx` | ✅ yes |
| **Modified by BOTH** | `components/charts.tsx`, `components/layout/AppShell.tsx`, `app/page.tsx` | ⚠️ see below |

For the three shared files:

- **`charts.tsx`** — Phase H's change is a **pure append** (`RateTrend`,
  `SplitBar`) and is a separate hunk from the redesign's edits. Hunk-splittable.
- **`AppShell.tsx`** — Phase H added `"analytics"` to the `Section` and `NavId`
  unions, an `IconAnalytics` import, and a "Reporting" nav group. These land
  **inside the same diff hunks** as the redesign's nav rework. **Not
  hunk-splittable.**
- **`app/page.tsx`** — Phase H added the `AnalyticsPage` import and the
  `{section === "analytics" && ...}` branch, likewise **inside** redesign
  hunks. **Not hunk-splittable.**

**So a Phase H commit that both builds and excludes the redesign is not
achievable by staging alone.** Committing the backend-plus-unentangled-frontend
set leaves a tree where `AnalyticsPage.tsx` exists but nothing routes to it and
its two chart imports are unresolved. This is a real decision, not an
oversight — see §13 for what was actually done about it.

**If asked to commit backend-only work, stage files explicitly by name
(`git add backend/x.py tests/y.py CLAUDE.md`), never `git add -A` or
`git add .`** — that discipline was followed for the Phase E (`66e6f79`), F
(`d351869`) and G (`8dfc286`) commits, and the frontend diff was verified
byte-identical afterwards each time.

---

## 12. Running it

**Requires PostgreSQL** — `DATABASE_URL` in `.env`. `docker-compose up -d`
for a local instance matching `.env.example`, or point at whatever instance
is already configured.

```powershell
.\start.ps1                 # installs deps, generates samples, starts server, opens browser
.\venv\Scripts\python.exe -m pytest tests\ -q      # 852 tests, no key/network needed
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
- **Email ingestion is OFF by default and stays off** unless both
  `EMAIL_INGEST_ENABLED=1` and `EMAIL_PROVIDER=imap` are set (§7b.11). Nothing
  polls a mailbox and no outbound connection is made otherwise, so the demo
  and the test suite are unaffected by it.
- **Email endpoints exist but have no UI** (§7b.12) — exercise them with the
  API directly, or through `POST /api/email/messages` with a `.eml` file.

---

## 13. Git / handoff state

**Latest completed phase: H (KPIs & analytics), §7c — implemented, tested, and
committed. Its BACKEND is committed; its FRONTEND is not (see below).**
**Phase I (logs, filtering, grouping, exports) has NOT been implemented — do
not start it without being explicitly asked. Its brief is in §9.**

### 13.1 What Phase H committed, and what it deliberately did not

Phases E, F and G were each staged **by name** — never `git add -A` — so the
unrelated frontend redesign (§11) stayed in the working tree untouched, and
that was verified after the fact each time. Phase H followed the same rule, but
hit a case the earlier phases never could: **it was the first phase asked to do
frontend work**, so its own changes are interleaved with the redesign's in
three files (§11.2).

**Committed** (staged by name):

```
backend/analytics.py            new -- the whole KPI/query layer
backend/storage.py              consumed_amounts_by_po() + four indexes
backend/main.py                 the seven /api/analytics endpoints
tests/test_analytics.py         new -- 119 tests
CLAUDE.md
README.md
```

**Deliberately NOT committed — still in the working tree:**

```
frontend-next/components/pages/AnalyticsPage.tsx    (new, Phase H)
frontend-next/lib/types.ts                          (Phase H only)
frontend-next/lib/useData.ts                        (Phase H only)
frontend-next/lib/metrics.ts                        (Phase H only)
frontend-next/components/ui/icons.tsx               (Phase H only)
frontend-next/components/charts.tsx                 (Phase H + redesign)
frontend-next/components/layout/AppShell.tsx        (Phase H + redesign)
frontend-next/app/page.tsx                          (Phase H + redesign)
...plus the entire pre-existing redesign (§11.1)
```

**Why.** The Phase H edits to `AppShell.tsx` and `app/page.tsx` land inside the
same diff hunks as the redesign's own changes, so they cannot be staged apart.
Committing them would have swept part of the redesign into an unrelated commit
— the one thing §11 forbids. Committing only the *separable* frontend files
would have produced a tree where `AnalyticsPage.tsx` exists but nothing routes
to it and two of its imports are unresolved. So the backend went in clean and
the whole frontend stayed out, on the repository owner's explicit instruction.

**Consequence to know about before doing anything:** at the committed revision
the analytics **API is complete and fully tested**, but the analytics **screen
does not exist**. The dashboard only appears with the working tree applied. It
was built, and it was verified end to end (§7c.13) — it is uncommitted, not
unfinished.

**The right way to finish this** is one frontend commit covering the redesign
and the Phase H UI together, once the repository owner has reviewed the
redesign. Splitting them further is not worth the risk to work that was never
committed in the first place.

### 13.2 Verification that the redesign was not disturbed

The redesign's own hunks were not edited, reformatted, reverted or staged. The
three shared files gained Phase H additions **on top of** the redesign, never
in place of it: `charts.tsx` is a pure append, and the `AppShell.tsx` /
`page.tsx` changes add a union member, an import and a render branch. Confirm
with `git diff -- frontend-next/` before trusting this paragraph.

### 13.3 Commits

```
<Phase H commit>  Answer how well the process is actually working, from the rows already on file (Phase H)
8dfc286 Go and fetch the invoices, instead of waiting to be handed one (Phase G)
d351869 Verify what an incoming email can actually prove about its own origin (Phase F)
66e6f79 Make the review decision path atomic, closing a concurrency gap Phase D left open
345033a Add multi-user review collaboration and activity history (Phase D)
4d72899 Add persistent invoice PDF storage behind a swappable local/S3 backend
147c0ce Migrate persistence from SQLite to PostgreSQL
cba2f01 Bring README and CLAUDE.md up to date with the frontend redesign
```

Branch `main`, **8 commits ahead of `origin/main`, not yet pushed** (push only
if explicitly asked).

**[README.md](README.md)** is kept in sync with the code and is the other
primary reference — when it and this file disagree on a factual claim about
the code, verify against the code directly rather than trusting either.

### Before doing anything in a new session

1. Read this file, then `README.md`.
2. `git status` and `git log --oneline -10` — confirm nothing has moved since
   §11/§13 above were written. Expect a working tree holding the redesign
   **and** the Phase H frontend (§13.1).
3. Confirm `DATABASE_URL` is set and PostgreSQL is reachable.
4. `.\venv\Scripts\python.exe -m pytest tests\ -q` — expect **848 passed, 4
   failed**, the 4 being the known `test_extraction_routing.py` cases, which
   pass 23/23 when that file runs alone (§10). A 5th failure in
   `test_samples.py`'s scanned sample means the live Gemini free-tier quota is
   spent, not that anything broke.
5. `cd frontend-next && npm run build` if you touch any frontend file —
   FastAPI serves the static export in `out/`.
6. Ask what to work on next. Do not start Phase I or later without being asked
   (§2, §9).
