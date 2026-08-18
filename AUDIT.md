# Architecture Audit — Invoice Processing

Date: 2026-08-18 · Auditor: build author (self-audit) · Method: line-by-line code
reading of `backend/`, plus targeted greps. No runtime verification was possible
(see Status).

---

## Status before anything else: the app does not currently run

The build is mid-refactor and **broken at `HEAD`**. `main.py:62` calls
`extraction.extract_text_and_ocr_flag()` and `main.py:77` calls
`extraction.extract_fields()`. Neither function exists any more — `extraction.py`
was rewritten to expose `extract_invoice()` instead, and `schemas.py` lost the
`has_text_layer` / `ocr_attempted` / `ocr_succeeded` fields that `main.py:78-83`
still assign.

Confirmed empirically: the pipeline emits the `INGEST` stage, then dies with
`AttributeError: module 'extraction' has no attribute 'extract_text_and_ocr_flag'`.

The last known-good state was the run before the "upload any PDF" work started
(all 7 samples passing). Everything below describes the code as it stands, and
flags where a finding belongs to the half-finished layer versus the original design.

There are also **no tests** anywhere in the repo, and the project is **not a git
repository** — so there is no way to diff against, or roll back to, the working state.

---

## 1. Where does the LLM make a business decision?

**Headline: no LLM call emits a verdict.** `APPROVED` / `NEEDS_REVIEW` /
`REJECTED` is computed only in `rules.decide()`, and every comparison feeding it
(`matching.match_po`, `rules.duplicate_check`, `rules.validate_required_fields`)
is deterministic Python. There is no prompt anywhere containing the words approve,
reject, or tolerance. That separation is real and it holds.

**But the framing "the LLM only extracts" is too generous**, because the LLM picks
the *inputs the verdict is arithmetic over*, and those inputs arrive downstream as
bare floats with no marker of how contestable they were. Every item below is a
judgment call rendered as fact:

| # | Location | The judgment | What it silently controls |
|---|---|---|---|
| 1 | `extraction.py:97` (`SCHEMA_PROMPT`) — *"total is the final amount payable including tax"* | Which of several candidate numbers on the page **is** the total | The entire tolerance check. `matching.py:57` computes `diff = total - remaining_before` off this one number |
| 2 | `SCHEMA_PROMPT` — *"po_references: any purchase order identifiers referenced anywhere"* | What counts as a PO reference | Which PO is matched (`matching.py:15-21`), therefore which balance is drawn down |
| 3 | `SCHEMA_PROMPT` — *"vendor_name is the company ISSUING the invoice, not the customer"* | Which party on the page is the payee | Approved-vendor check, PO inference fallback, **and** duplicate detection — all three key off vendor |
| 4 | `SCHEMA_PROMPT` — *"currency: inferred from symbols or text. Default USD only if there is no signal"* | Currency inference | Nothing. See finding 5.6 — currency is extracted and then never read |

Two more that are **not** the LLM, and are arguably worse because they are code
pretending to be arithmetic:

| # | Location | The judgment |
|---|---|---|
| 5 | `extraction.py:319-320` | **The code fabricates a total.** If no total pattern matched but subtotal and tax did, it sets `total = subtotal + tax`. That number may appear nowhere on the document, and downstream it is indistinguishable from a total that was actually read off the page |
| 6 | `extraction.py:238-260` (`_guess_vendor`) | Picks the vendor by **line position** — first line in the top 14 that isn't filtered by a skip-regex. Pure positional heuristic, presented downstream as a definite vendor name |

**Assessment.** The decision *layer* is clean. The decision *inputs* are not, and
because extraction output is a flat bag of `Optional[float]`, the rules layer cannot
distinguish "$3,000.00 read from a line labelled Total Due" from "$3,000.00 that I
assembled from two other numbers" or "vendor name taken from whichever line looked
least like an address." The rules trust all of it equally and absolutely.

---

## 2. Are tolerances and rules in config, or embedded in code?

**Embedded in code, without exception.** `config.py` exists but holds only
*operational* settings — upload cap, page caps, model name, API-key presence. It
contains zero business policy.

Every business rule is a literal in a function body or a SQL string:

| Rule | Location | Form |
|---|---|---|
| Tolerance = max(2% , $25) | `matching.py:6` | `return max(amount * 0.02, 25.00)` — two magic numbers in a one-line function |
| Tolerance applies to **remaining balance**, not PO face value | `matching.py:55` | Implicit in which variable is passed |
| Tolerance is **one-sided** (over = fail, under = partial) | `matching.py:60-61` | `within = diff <= tol` — the single most consequential design decision in the build, expressed as one absent `abs()` |
| Required fields | `rules.py:5` | `REQUIRED_FIELDS = [...]` module constant |
| Severity hierarchy (reject beats review beats approve) | `rules.py:132-137` | `if/elif` control flow |
| Which finding maps to which severity | `rules.py` throughout | Inline `"fail"` / `"warn"` / `"ok"` string literals at each `add()` call |
| Duplicate identity = invoice # + total ±$0.01 + vendor | `storage.py:133-137` | Embedded in a SQL `WHERE` clause |
| Only `APPROVED` runs consume PO budget | `storage.py:120` | Embedded in a SQL `WHERE status='APPROVED'` |
| "Has a text layer" = ≥25 characters | `extraction.py:62` | `len(text) >= 25` |
| Vendor matching is bidirectional substring | `storage.py:104-106` | `if vn in name_norm or name_norm in vn` |

Reference **data** (POs, vendors) *is* externalised to `data/*.json`. Reference
**policy** is not. So an AP controller can add a vendor without touching code, but
cannot change the tolerance from 2% to 3% — or answer "what was the tolerance in
July?" — without reading Python.

---

## 3. Do extracted fields carry confidence and provenance?

**No. They are bare values.** `ExtractedInvoice` (`schemas.py:15-27`) is a flat
dataclass of `Optional[str]` / `Optional[float]`. There is no confidence, no page
number, no character offset, no bounding box, no source snippet, and no per-field
method.

The single provenance signal is `extraction_method` — **one string for the entire
document** (`"llm (text)"`, `"llm (vision)"`, `"regex"`, `"none"`). That is far too
coarse, because within one document the fields have wildly different reliability:

- In the regex path, `invoice_number` comes from a tight anchored pattern while
  `vendor_name` comes from a positional guess (`_guess_vendor`). Both surface as
  plain strings tagged `"regex"`.
- A `total` synthesized by `subtotal + tax` (finding 1.5) carries the same
  `"regex"` tag as one read directly off the page.
- A vision-extracted total from a blurry scan carries the same weight as one from
  clean embedded text.

Downstream consequences:
- `rules.validate_required_fields` tests `if not extracted.get(f)` — presence only.
  A garbage-but-present value passes.
- The UI shows every extracted field in one uniform table with no reliability
  signal, so a human reviewer has nothing to focus their attention on.
- Nothing can point a reviewer at *where on the page* a number came from, which is
  the first thing an AP clerk actually wants when a number looks wrong.

---

## 4. Is there a structured trace, or just log lines?

**Better than log lines, short of a reconstructable trace.** Four artefacts are
persisted per run as JSON blobs in the `runs` table (`storage.py:141-165`):
`stages_json`, `reasons_json`, `extracted_json` (including `raw_text`), and
`po_match_json`.

That is genuinely useful — the dashboard replays historical runs from it without
re-executing anything, and the source text is retained.

**What blocks true reconstruction:**

1. **The trace is prose, not data.** A stage `detail` is
   `"Diff $2500.00 vs tolerance $100.00 (OUTSIDE tolerance)"`. To answer "what
   tolerance applied?" programmatically you would have to parse an English
   sentence. Same for `reasons[].text`. The numbers exist structurally in
   `po_match_json`, but the *reasoning* does not.

2. **Reference data mutates under history — this is the serious one.**
   `storage.init_db()` runs `DELETE FROM purchase_orders` and re-inserts from
   `data/purchase_orders.json` **on every server start** (`storage.py:56-68`).
   Edit that JSON and every historical run's reasoning now refers to a PO whose
   amount has silently changed. The run says "$5,000 authorised"; the PO table
   says something else; nothing records which was true at decision time.

3. **No version pinning of any kind.** The run does not record the rules version,
   the tolerance formula in force, the code revision, or even
   `config.EXTRACTION_MODEL`. A run extracted by one model is indistinguishable
   from one extracted by another.

4. **The derivation set is not captured.** `po_match.remaining_before` is stored,
   but not *which prior run IDs* were summed to produce it. Since
   `consumed_amount_for_po` recomputes live from current run statuses, changing an
   old run's status would change what a newer run's stored number *should* have
   been, with no way to detect the drift.

5. **No decision/idempotency key.** Re-uploading the same PDF creates a fresh run
   with a new ID; correlation is by duplicate-detection heuristic only.

---

## 5. Can a low-confidence extraction reach auto-approve?

**Yes — five distinct paths.** In each, every rule reads a value that is present
and well-formed, so every check passes and the run lands on `APPROVED` with no
human ever seeing it.

**5.1 — Synthesized total (severity: high).**
`extraction.py:319` sets `total = subtotal + tax` when no total was found. Required
fields pass (total is non-null), tolerance compares that invented figure to the PO,
and if it happens to land within tolerance the invoice is approved. No reason line,
no severity flag, nothing distinguishes it. This is the cleanest example of the
architecture's core weakness: a value's *origin* is discarded before the rules see it.

**5.2 — Inferred PO match does not block approval (severity: high).**
When no PO reference is extracted, `matching.py:23-32` falls back to guessing:
`best = min(pos, key=lambda p: abs(p["amount"] - total))` — the nearest-amount PO
for that vendor, **with no distance cap**. `rules.py:91-96` then adds a reason at
level `"warn"` — but critically **does not set `review = True`**. Severity `"warn"`
is cosmetic; only `review`/`reject` change the verdict. So an invoice that never
named a PO gets silently bound to one the process picked, and can auto-approve.
This is a live bug, not just a design gap.

**5.3 — Bidirectional substring vendor matching (severity: high).**
`storage.py:104-106` matches if either name contains the other. An extracted
`"Acme Corp"` (a different, unapproved legal entity) matches approved
`"Acme Office Supplies"`. Vendor check passes; PO inference and duplicate detection
then both key off the wrong vendor.

**5.4 — Positional vendor guess (severity: medium).**
`_guess_vendor` returns the first non-filtered line. On layouts where the customer's
name sits above the vendor's — common on remittance and self-billing formats — it
returns the wrong company, which then feeds 5.3.

**5.5 — No internal arithmetic consistency check (severity: medium).**
Nothing ever verifies `subtotal + tax == total`, or that line items sum to
subtotal. An OCR misread turning `$1,300` into `$7,300` in the total field, while
subtotal and line items still say `$1,300`, is invisible to every rule. The data
needed to catch it is already extracted and simply never compared.

**5.6 — Currency is extracted and then ignored entirely (severity: high).**
`currency` is populated by both the LLM and regex paths. It is **never read** by
`matching.py` or `rules.py` — verified by grep, zero occurrences. The
`purchase_orders` table *has* a `currency` column that is likewise never consulted.
A €3,000 invoice against a $5,000 PO is compared as `3000` vs `5000` and approved
as a partial invoice. For a process whose entire premise is comparing amounts, this
is the most embarrassing gap in the audit.

**What is safe:** a null total is caught by `REQUIRED_FIELDS` → review. A missing
vendor name yields `vendor_ok is None` → review. Unreadable scans yield empty
fields → review. The explicit "don't guess" paths do work — the failures above are
all cases where something *else* filled the gap before the rules could notice.

---

## Summary

| Q | Verdict |
|---|---|
| 1. LLM making business decisions | No verdicts from the LLM — genuinely clean. But it selects the decision inputs, and provenance is discarded at the boundary |
| 2. Rules in config | None. All in code; `config.py` holds operational settings only |
| 3. Confidence + provenance | Absent. One document-level `extraction_method` string is the entire signal |
| 4. Structured trace | Partial. Persisted and replayable, but prose-based, unversioned, and undermined by reference data that mutates under history |
| 5. Low-confidence auto-approve | Yes — 5 paths, of which 5.2 and 5.6 are outright bugs |

The build's headline claim — *"the AI reads, the rules decide"* — survives the
audit. The claim it cannot currently support is the implicit one underneath:
*that what the AI read is worth deciding on.*

---

# Refactor plan

Ordered by (risk removed) ÷ (effort). No code changes yet.

## Phase 0 — Get back to green

Nothing else can be validated while the app is broken.

1. Reconcile `main.py` with the new `extraction.extract_invoice()` API and the
   trimmed `schemas.py`.
2. `git init` and commit the working state. There is currently no way to roll back.
3. Write a regression harness — the 7 samples with expected verdicts, as a real
   test file rather than the throwaway scripts used so far. This becomes the safety
   net for every phase below.

**Exit:** 7/7 samples pass from a clean database, on demand.

## Phase 1 — Fix the two live bugs

Small, high-value, independently demoable.

4. **Inferred PO match must set `review = True`** (5.2), and `match_po` should
   refuse to infer beyond a configurable distance rather than always taking the
   nearest. An invoice that names no PO should never auto-approve.
5. **Enforce currency** (5.6): compare invoice currency to PO currency; mismatch is
   a hard review. Never compare magnitudes across currencies.
6. **Tighten vendor matching** (5.3): require exact match after normalisation
   (case, punctuation, legal suffixes), and treat fuzzy hits as *candidates
   requiring review* rather than confirmations.

**Exit:** three new edge-case fixtures — no-PO-reference, EUR-invoice-vs-USD-PO,
`Acme Corp` vs `Acme Office Supplies` — each landing on review, verified by tests.

## Phase 2 — Provenance and confidence (the structural fix)

This is the change the other findings mostly reduce to.

7. Replace bare values with a `Field` wrapper: `value`, `confidence` (0–1),
   `source` (`llm-text` / `llm-vision` / `regex` / `derived`), `evidence` (the
   verbatim snippet the value came from), and `page` + character span where known.
   `ExtractedInvoice` becomes a container of `Field`s.
8. Have the LLM return per-field confidence and the verbatim source snippet
   alongside each value — the prompt already returns structured JSON, so this is a
   schema extension, not a new call. Regex patterns already know their match span
   and can report it for free; assign confidence per pattern tier (anchored label
   match = high, positional guess = low).
9. **Mark derived values explicitly.** `total = subtotal + tax` becomes
   `source="derived"`, confidence capped, with the arithmetic recorded as evidence
   (5.1). It stops being indistinguishable from a read value.
10. Add a confidence gate in `rules`: any field below threshold that is *material
    to the verdict* forces review regardless of whether the numeric checks pass.
    This is what actually closes finding 5 as a class rather than case by case.
11. Add an arithmetic consistency check (5.5): `subtotal + tax` vs `total`, and
    line-item sum vs subtotal, as a real rule with its own tolerance.

**Exit:** a deliberately ambiguous invoice extracts a plausible total at low
confidence and is routed to review *because of the confidence*, with the UI showing
the evidence snippet.

## Phase 3 — Externalise policy

12. Move every rule from §2 into a versioned `policy.yaml`: tolerance formula,
    one-sidedness, required fields, confidence thresholds, duplicate-identity keys,
    which statuses consume budget, severity mapping.
13. Load it into a typed policy object at startup; `rules`/`matching` read only
    from that object. No numeric literal survives in decision code.
14. Stamp `policy_version` onto every run.

**Exit:** changing tolerance from 2% to 3% is a one-line YAML edit, and old runs
still report the tolerance that was actually applied to them.

## Phase 4 — A real trace

15. Introduce a `DecisionTrace` object: for each check — inputs consulted (with
    their `Field` provenance), the rule id and version, the comparison performed as
    structured data, the outcome, and the severity. Human-readable prose is
    *rendered from* the trace, never the trace itself.
16. Snapshot the reference data actually used — the matched PO row and the run IDs
    summed into `remaining_before` — into the trace, fixing the mutating-history
    problem (§4.2, §4.4).
17. Stop re-seeding reference tables on every start; seed only when empty, and make
    reference edits explicit, versioned operations.
18. Record `code_version`, `policy_version`, and `extraction_model` per run.

**Exit:** a stored run can be re-rendered, and its arithmetic re-verified, with the
pipeline switched off and the seed JSON edited underneath it.

## Phase 5 — Surface it

19. Confidence badges and evidence snippets per field in the run view; hovering a
    number shows the text it came from.
20. A "why" view driven by the structured trace rather than prose.
21. Show `policy_version` and extraction route on every historical run.

## Explicitly out of scope

- Multi-currency FX conversion. Detect and refuse to compare; do not convert.
- Learning or auto-tuning thresholds. Policy stays human-set and versioned.
- Replacing SQLite, or any auth/multi-tenant work.

## Sequencing note

Phases 0 and 1 are worth doing before the next demo regardless — Phase 0 restores a
runnable build and Phase 1 removes two defects that a reviewer could trip over by
accident. Phase 2 is the real architectural fix and the honest answer to questions
3 and 5; if only one phase after that gets built, it should be this one. Phases 3–5
are what make the result auditable rather than merely correct.
