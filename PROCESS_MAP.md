# Process Map — Invoice Processing (PS-1)

## Input
A vendor invoice PDF (clean text, scanned image, itemized or bundled, with or without
an explicit PO reference).

## Pipeline stages (each one is a visible step in the live run view)

1. **INGEST** — receive the PDF, record filename/size/page count.
2. **EXTRACT_TEXT** — pull embedded text (pdfplumber). If the page has ~0 extractable
   characters, treat it as a scanned image and attempt OCR (pytesseract). If OCR isn't
   available in the runtime, don't guess at the content — flag it and continue to a
   review decision instead of fabricating fields.
3. **EXTRACT_FIELDS** — parse structured fields out of the text: vendor name, invoice
   number, invoice date, PO reference(s), line items, subtotal, tax, total, currency.
   Uses an LLM (Anthropic API) extractor when `ANTHROPIC_API_KEY` is set, otherwise
   falls back to a deterministic regex/heuristic extractor — same output schema either
   way, so the rest of the pipeline doesn't care which one ran.
4. **VALIDATE** — check that the fields required to make a decision are present
   (vendor, invoice number, total). Missing critical fields → can't safely auto-approve.
5. **VENDOR_CHECK** — is the vendor on the approved vendor list?
6. **PO_MATCH** — find the referenced PO. If no explicit reference, try to infer one
   from vendor + amount. Handles the case where a PO has already been partially
   consumed by earlier approved invoices (split PO).
7. **DUPLICATE_CHECK** — hash (vendor, invoice number, total) against every previously
   processed run. A resubmission of the same invoice is flagged, not silently paid twice.
8. **TOLERANCE_CHECK** — compare the invoice total against the PO's *remaining* balance
   (PO amount minus what earlier invoices already consumed) within a tolerance of
   max(2% of remaining, $25).
9. **DECISION** — aggregate every check into one of three outcomes:
   - **APPROVED** — every check passed.
   - **NEEDS_REVIEW** — recoverable problem (missing field, amount outside tolerance,
     no PO match, OCR unavailable). A human can resolve it.
   - **REJECTED** — a check the process shouldn't override on its own (duplicate,
     vendor not approved).

   Every decision carries the full reasoning trail, not just the verdict.

## Output shape
```json
{
  "status": "APPROVED | NEEDS_REVIEW | REJECTED",
  "reasons": ["..."],
  "extracted": { ...fields... },
  "po_match": { "po_number": "...", "remaining_before": 0, "remaining_after": 0 },
  "stages": [ {"name": "...", "status": "...", "detail": "..."} ]
}
```

## Decision hierarchy
- Any REJECT-level finding (duplicate, unapproved vendor) wins outright.
- Otherwise any REVIEW-level finding (missing field, no OCR text, PO not found,
  PO closed, amount outside tolerance) sends it to review.
- Otherwise APPROVED.

## Edge cases built and demonstrated
1. **Scanned invoice, no text layer** — OCR is attempted; when unavailable, the process
   refuses to fabricate fields and routes to review with a clear reason.
2. **Missing critical field** — invoice with no invoice number → review, not a guess.
3. **Split PO** — a PO fulfilled across two invoices; the second is matched against the
   *remaining* balance, not the original PO total. A third invoice against the same PO
   after it's exhausted is rejected/reviewed for exceeding the balance.
4. **Duplicate invoice** — the same vendor/invoice number/total submitted twice is
   caught against run history and rejected instead of being paid again.

## Why this split (LLM vs. rules)
Extraction (turning messy PDF text into structured fields) is exactly what an LLM is
good at and rules are bad at — vendors format invoices differently and there's no fixed
schema to regex against reliably. The *decision* (tolerance math, duplicate hashing, PO
balance tracking) is exactly what deterministic code is good at and an LLM is bad at —
it needs to be the same answer every time, auditable, and explainable to a non-technical
AP reviewer. So the LLM only ever touches extraction; every dollar comparison and status
decision is plain code.
