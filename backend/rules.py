"""Decision rules: required fields, vendor approval, duplicates, and the final
aggregation of every check into one status + reasons trail."""
import re

import config
import storage

REQUIRED_FIELDS = ["vendor_name", "invoice_number", "total"]


def _is_missing(value):
    """Presence, not truthiness.

    `not value` would call a total of 0.00 "missing", which is wrong twice over:
    the figure IS present, and reporting it as absent sends an AP clerk looking
    for a number that is printed on the page. A zero total is a *invalid amount*
    problem -- see validate_amount() -- and belongs to that check, with its own
    message.
    """
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def validate_required_fields(extracted: dict):
    return [f for f in REQUIRED_FIELDS if _is_missing(extracted.get(f))]


def validate_confidence(extracted: dict):
    """Fields central to the decision (config.CONFIDENCE_GATED_FIELDS) that the
    extractor itself is not confident it read correctly.

    Returns a list of {"field", "confidence", "source", "evidence"} dicts, one
    per gated field scored below config.CONFIDENCE_THRESHOLD. Empty means every
    gated field either has no confidence signal or scored at or above it.

    A field with NO provenance entry at all is not reported here -- that is
    either a field the extractor never attempted (regex does not track every
    field) or one it found with total certainty and nothing worth flagging.
    A field that is MISSING entirely is validate_required_fields()'s business,
    not this one's: reporting the same absence as two different findings would
    double-count one fact. This only fires for a value that IS present but
    whose own reader is not sure of it -- a distinct failure class (a reading-
    quality problem) from either of those.
    """
    provenance = extracted.get("provenance") or {}
    low = []
    for field_name in config.CONFIDENCE_GATED_FIELDS:
        if _is_missing(extracted.get(field_name)):
            continue
        info = provenance.get(field_name) or {}
        conf = info.get("confidence")
        if conf is None or conf >= config.CONFIDENCE_THRESHOLD:
            continue
        low.append({
            "field": field_name,
            "confidence": conf,
            "source": info.get("source"),
            "evidence": info.get("evidence"),
        })
    return low


def validate_amount(extracted: dict):
    """The invoice total must be a positive number.

    Returns None when the total is valid or absent, and a dict describing the
    problem when it is not. Absence is deliberately not this check's business --
    validate_required_fields owns that, and reporting it twice would put two
    findings on one fact.

    Two cases, both of which previously reached approval:

    * **Negative.** A negative total is a credit note, not a payable. Paying one
      as an invoice moves money the wrong way. Before this check it sailed
      through: matching compares `total - remaining`, and a negative total makes
      that comfortably negative, which reads as a small partial invoice.
    * **Zero.** Nothing is owed. Approving it to pay is meaningless, and it is
      more likely a misread figure than a real zero-value bill.

    Deliberately no upper bound. A large invoice is a PO/tolerance question, and
    an arbitrary ceiling here would reject legitimate high-value invoices that
    the ledger is perfectly capable of judging.
    """
    total = extracted.get("total")
    if total is None:
        return None
    try:
        value = float(total)
    except (TypeError, ValueError):
        return None      # non-numeric is an extraction problem, not an amount one
    if value < 0:
        return {"total": value, "kind": "negative"}
    if value == 0:
        return {"total": value, "kind": "zero"}
    return None


def validate_arithmetic(extracted: dict):
    """Check that the invoice adds up: subtotal + tax == total.

    Returns None when the invoice is consistent OR when there is not enough
    information to judge, and a dict of the numbers when it is not. Callers treat
    a dict as "hold this for a human".

    Three deliberate limits:

    * **All three fields must be present.** A missing tax line is not evidence of
      bad arithmetic -- it is evidence of a missing tax line. Checking
      `subtotal == total` in that case would fabricate a failure on every invoice
      whose tax the extractor did not pick up.
    * **`is None`, not truthiness.** A genuine `tax` of 0.00 is a *present* value
      and the check applies. Testing `if not tax` would silently skip every
      zero-rated invoice, which is the population most worth checking.
    * **subtotal + tax only.** The schema carries exactly these three financial
      fields -- there is no shipping or freight column to fold in. If one is ever
      added, it belongs in this sum, and this comment is where to start.

    Note the interaction with extraction: when a document has no printed total,
    `regex_extract` synthesises one as `subtotal + tax`, so this check passes by
    construction. That is correct -- there is no printed figure to contradict --
    but it does mean the check only bites on invoices that actually stated a
    total.
    """
    subtotal, tax, total = extracted.get("subtotal"), extracted.get("tax"), extracted.get("total")
    if subtotal is None or tax is None or total is None:
        return None
    try:
        expected = round(float(subtotal) + float(tax), 2)
        diff = round(float(total) - expected, 2)
    except (TypeError, ValueError):
        # Non-numeric values are an extraction problem, not an arithmetic one,
        # and the required-field check already covers a missing total.
        return None

    if abs(diff) <= config.ARITHMETIC_TOLERANCE_DOLLARS:
        return None
    return {"subtotal": float(subtotal), "tax": float(tax), "total": float(total),
            "expected": expected, "diff": diff}


def vendor_check(extracted: dict):
    """Returns (ok, vendor_row, detail) where ok is True/False/None.
    None means "unknown" -- e.g. no vendor name could be extracted at all -- which
    is a review case, not a confident rejection. False means a vendor name WAS
    extracted and it's confirmed not on the approved list."""
    vendor_name = extracted.get("vendor_name")
    # Whitespace-only counts as unreadable, not as a name. `not vendor_name` alone
    # would let "   " through to the lookup, where it matches nothing and reads as
    # a confident rejection -- exactly the confusion the tri-state exists to avoid.
    if not vendor_name or not str(vendor_name).strip():
        return None, None, "No vendor name could be extracted -- cannot verify approval status."

    matches = storage.find_vendor_matches(vendor_name)

    # More than one approved vendor normalises to this name. That is ambiguity,
    # not disapproval: the name IS on the list, we just cannot say which entry it
    # means, and picking one would be a guess about who gets paid. Review, not
    # reject -- the same tri-state distinction the rest of this function makes
    # between "confirmed not approved" and "could not tell".
    if len(matches) > 1:
        names = ", ".join(f"\"{m['vendor_name']}\" ({m['vendor_id']})" for m in matches[:4])
        return None, None, (
            f"Vendor \"{vendor_name}\" matches {len(matches)} approved vendors -- {names}. "
            f"Cannot determine which is intended; confirm the vendor before payment."
        )

    vendor_row = matches[0] if matches else None
    if vendor_row is None:
        return False, None, f"Vendor \"{vendor_name}\" is not on the approved vendor list."
    if vendor_row["status"] != "approved":
        return False, vendor_row, f"Vendor \"{vendor_row['vendor_name']}\" is on file but status is \"{vendor_row['status']}\", not approved."
    return True, vendor_row, f"Vendor \"{vendor_row['vendor_name']}\" is approved ({vendor_row['vendor_id']})."


def duplicate_check(extracted: dict, exclude_run_id=None):
    dup = storage.find_duplicate(extracted.get("vendor_name"), extracted.get("invoice_number"), extracted.get("total"))
    if dup and dup["id"] != exclude_run_id:
        return dup, (
            f"Invoice #{extracted.get('invoice_number')} for {extracted.get('total')} from "
            f"{extracted.get('vendor_name')} matches run #{dup['id']} processed on "
            f"{dup['created_at'][:10]} (status {dup['status']})."
        )
    return None, "No prior run matches this vendor/invoice number/total combination."


# --------------------------------------------------------------------------
# Line-item agreement with the purchase order
#
# WHY THIS EXISTS, GIVEN THE PO BALANCE CHECK ALREADY RUNS
#
# Every money check before this one compares ONE number -- the invoice total --
# against what the PO authorises. That is the right check and it catches
# overbilling, but it is blind by construction to a rearrangement UNDERNEATH a
# correct total: 8 laptops at 62,500 and 10 laptops at 50,000 are both 500,000,
# so the balance check sees an exact match and every tolerance test passes. The
# vendor has shipped two fewer machines and charged 25% more each, and nothing
# above notices, because nothing above looks below the total.
#
# So this rule does not ask "is the total right". It asks "do the numbers that
# PRODUCE the total agree with what was ordered".
#
# IT HOLDS, IT NEVER REJECTS. A quantity that differs is very often legitimate:
# a partial shipment, a substituted part, a renegotiated price nobody put on the
# PO. What it is not is something to approve unattended. Rejecting would also
# turn a stale PO -- one the buyer agreed to vary by email -- into a bounced
# invoice, which is a worse failure than a held one.
#
# IT IS SKIPPED, NOT FAILED, WHEN EITHER SIDE HAS NO LINE ITEMS. Most POs state
# a total and nothing else, and the regex extraction route often reads no line
# items at all. In both cases the honest answer is "there was nothing to
# compare", and a check that failed on absence would hold nearly every invoice
# this application already approves.
# --------------------------------------------------------------------------

LINE_ITEM_RULE = "Line items match the PO"

# A money comparison needs a cent of slack for float representation, and no more
# than that: this rule exists to notice a repriced line, so a real tolerance band
# would defeat it. Quantities get a smaller epsilon because they are usually
# whole numbers and occasionally fractional (hours, kilograms).
_LINE_MONEY_EPSILON = 0.01
_LINE_QTY_EPSILON = 0.001


def normalise_item_name(name) -> str:
    """Fold a line-item description to a comparable form.

    Deliberately NOT storage.normalize_vendor_name: that one carries
    vendor-specific rules (company suffixes and the like) and is the single
    definition of vendor identity used to decide who sees whose invoices
    (CLAUDE.md 7g.5). Borrowing it here would tie two unrelated notions of
    sameness together, so that tightening one silently moves the other.

    Punctuation and case are dropped and whitespace collapsed, so
    "Laptop - Model X" and "laptop model x" are the same item. Nothing more
    clever is attempted: a synonym table would be guessing at what a vendor
    meant, and guessing wrong here means comparing two different products'
    prices and reporting the difference as a discrepancy.
    """
    folded = re.sub(r"[^a-z0-9]+", " ", (name or "").strip().lower())
    return " ".join(folded.split())


def _num(v):
    """A finite number, or None. Strings, bools, None and NaN all read as absent."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return None if f != f else f


def line_item_check(extracted: dict, po_match: dict) -> dict:
    """Compare an invoice's line items against those of the PO(s) it names.

    Returns {"applicable", "skipped_because", "compared", "findings"}.
    `findings` is empty when everything agreed; each entry is
    {"kind", "item", "detail"} where kind is one of:

        quantity      the invoice bills a different number of units
        unit_price    the invoice bills a different price per unit
        line_total    quantity x unit_price does not equal the line's own amount
        po_line_total the line's amount differs from what the PO authorised for it
        unknown_item  the invoice bills for something the PO does not list

    `line_total` is the one check that does not involve the PO at all -- it is
    the line's internal arithmetic, and it is worth having separately because a
    line that does not multiply out is wrong whoever ordered it. It therefore
    runs even for a PO that does not itemise, which is the only part of this
    rule that does.

    A PO line the invoice never bills is NOT a finding. Billing less than was
    ordered is a partial invoice, which this system treats as normal everywhere
    else (tolerance is one-sided for exactly this reason).
    """
    findings = []
    inv_items = [i for i in (extracted or {}).get("line_items") or [] if isinstance(i, dict)]

    # Gather the PO side across every PO this invoice bound, so a multi-PO
    # invoice can match a line to whichever order actually carries it.
    po_items = []
    for alloc in (po_match or {}).get("allocations") or []:
        for item in alloc.get("po_line_items") or []:
            po_items.append((alloc.get("po_number"), item))

    if not inv_items:
        return {"applicable": False, "compared": 0, "findings": [],
                "skipped_because": "the invoice states no line items"}

    # Internal arithmetic first: it needs no PO and is checked either way.
    for item in inv_items:
        qty = _num(item.get("quantity"))
        price = _num(item.get("unit_price"))
        amount = _num(item.get("amount"))
        if qty is None or price is None or amount is None:
            continue
        expected = round(qty * price, 2)
        if abs(expected - amount) > _LINE_MONEY_EPSILON:
            findings.append({
                "kind": "line_total",
                "item": item.get("description") or "(unnamed line)",
                "detail": ("{:g} x {:,.2f} = {:,.2f}, but the line states {:,.2f}"
                           .format(qty, price, expected, amount)),
            })

    if not po_items:
        return {"applicable": bool(findings), "compared": 0, "findings": findings,
                "skipped_because": (None if findings
                                    else "no purchase order on file states line items")}

    # One queue per item name, popped as it is matched, so an invoice billing the
    # same item on two lines is compared against two PO lines rather than the
    # same one twice.
    queues = {}
    for po_number, item in po_items:
        queues.setdefault(normalise_item_name(item.get("description")), []).append((po_number, item))

    compared = 0
    for item in inv_items:
        name = item.get("description") or ""
        queue = queues.get(normalise_item_name(name))
        if not queue:
            findings.append({
                "kind": "unknown_item",
                "item": name or "(unnamed line)",
                "detail": "this line is not on any purchase order this invoice references",
            })
            continue

        po_number, po_item = queue.pop(0)
        compared += 1
        label = "{} (against {})".format(name or "(unnamed line)", po_number)

        inv_qty, po_qty = _num(item.get("quantity")), _num(po_item.get("quantity"))
        if inv_qty is not None and po_qty is not None and abs(inv_qty - po_qty) > _LINE_QTY_EPSILON:
            findings.append({
                "kind": "quantity", "item": label,
                "detail": "purchase order authorises {:g}, invoice bills {:g}".format(po_qty, inv_qty),
            })

        inv_price, po_price = _num(item.get("unit_price")), _num(po_item.get("unit_price"))
        if inv_price is not None and po_price is not None and abs(inv_price - po_price) > _LINE_MONEY_EPSILON:
            findings.append({
                "kind": "unit_price", "item": label,
                "detail": ("purchase order price {:,.2f} per unit, invoice bills {:,.2f}"
                           .format(po_price, inv_price)),
            })

        inv_amt, po_amt = _num(item.get("amount")), _num(po_item.get("amount"))
        if inv_amt is not None and po_amt is not None and abs(inv_amt - po_amt) > _LINE_MONEY_EPSILON:
            findings.append({
                "kind": "po_line_total", "item": label,
                "detail": ("purchase order authorises {:,.2f} for this line, invoice bills {:,.2f}"
                           .format(po_amt, inv_amt)),
            })

    return {"applicable": True, "compared": compared, "findings": findings,
            "skipped_because": None}


def reevaluate_po_queue(po_number: str, triggered_by: int = None):
    """Re-run the PO balance check on invoices held for review against this PO.

    Called when a PO's balance changes -- typically because an approved invoice
    was reversed, freeing budget. Any held invoice that now fits is approved, in
    submission order, so the one that queued first gets the money.

    Only invoices held *purely* on balance are eligible. A run held because it is
    a duplicate, has no invoice number, or tripped the security guard stays held:
    freeing budget says nothing about those, and auto-approving them would turn a
    reversal into a way to launder a blocked invoice through the system.

    Returns a list of {run_id, from, to, reason} describing what changed.
    """
    changed = []
    for run in storage.runs_pending_on_po(po_number):
        blockers = [r for r in (run.get("reasons") or [])
                    if r.get("level") == "fail" and not _is_balance_reason(r.get("text", ""))]
        if blockers:
            continue

        # A multi-PO invoice is never released by this path. It is not held
        # because a balance was short -- it is held because the document never
        # said how to divide the money, and freeing budget says nothing about
        # that. Approving it here would let a reversal elsewhere commit a split
        # no person ever confirmed.
        if len(storage.allocations_for_run(run["id"])) > 1:
            continue

        total = run.get("total")
        if total is None:
            continue

        remaining = storage.remaining_for_po(po_number, exclude_run_id=run["id"])
        if remaining is None:
            continue

        import matching
        tol = matching.tolerance_for(remaining if remaining > 0 else total)
        if round(total - remaining, 2) > tol:
            continue

        note = (
            f"Auto-approved on re-evaluation: ${remaining:.2f} became available on {po_number}"
            + (f" after run #{triggered_by} was reversed" if triggered_by else "")
            + f", which covers this ${total:.2f} invoice."
        )
        ok, old, _ = storage.set_run_status(run["id"], "APPROVED", note)
        if ok:
            storage.log_activity(run["id"], "AUTO_APPROVED", actor=None, note=note,
                                 metadata={"po_number": po_number, "triggered_by": triggered_by})
            changed.append({"run_id": run["id"], "from": old, "to": "APPROVED", "reason": note})
    return changed


# Text fragments that identify a reason as "held on PO balance" rather than a
# substantive finding. Matched against the reason text the decide() below writes.
_BALANCE_MARKERS = (
    "over the remaining PO balance",
    "Balance changed while this invoice was being processed",
)


def _is_balance_reason(text: str) -> bool:
    return any(marker in text for marker in _BALANCE_MARKERS)


# Routes where a language model actually read the document. Only these support
# the "no fields at all" inference below: if the regex fallback or nothing at
# all ran, an empty result says the EXTRACTOR failed, not that the document is
# not an invoice.
_LLM_ROUTES = ("groq-text", "gemini-vision")


def looks_like_an_invoice(extracted: dict) -> bool:
    """True if the extractor found anything that identifies this as an invoice.

    Any single signal is enough — a vendor, an invoice number, a total, a date,
    a PO reference or a line item. Invoices vary enormously in format, so the
    bar is deliberately one field, not a required combination.
    """
    if not extracted:
        return False
    if any(extracted.get(k) for k in ("vendor_name", "invoice_number", "total", "invoice_date")):
        return True
    return bool(extracted.get("po_references") or extracted.get("line_items"))


def is_not_an_invoice(extracted: dict, extract_info: dict) -> bool:
    """True when a model read the document cleanly and found no invoice in it.

    WHY THIS IS THE SIGNAL

    No keyword list. A vocabulary check ("does the text contain the word
    invoice?") is both too weak and too strong: it misses invoices in other
    languages, and it fires on any document that merely DISCUSSES invoicing —
    a contract, a policy, this project's own brief. The extractor is already a
    document classifier: when a model reads a page and cannot find a vendor, a
    number, an amount, a date, a PO reference or a single line item, the
    document does not contain an invoice.

    Gated on the model routes on purpose. If extraction fell back to regex or
    failed outright, an empty result is evidence about the extractor, not about
    the document, and this must not fire.
    """
    # `None` means the caller did not supply extraction data at all, which is
    # not the same as supplying data that turned out empty. Treating the first
    # as "not an invoice" would hard-reject a perfectly good invoice on any code
    # path that happens not to pass `extracted` — absence of evidence read as
    # evidence of absence. Only an explicitly empty result may classify.
    if extracted is None:
        return False

    route = (extract_info or {}).get("route")
    if route not in _LLM_ROUTES:
        return False
    return not looks_like_an_invoice(extracted)


def decide(extract_info: dict, missing_fields, vendor_ok, vendor_detail,
           dup_row, dup_detail, po_match: dict, arithmetic=None, amount=None,
           audit=None, extracted=None, low_confidence=None):
    """Aggregates every check into one status plus a severity-tagged reasoning trail.

    AUDIT TRAIL

    Pass a dict as `audit` and it is filled in with a structured record of this
    evaluation: the values compared, the PO and where its record came from, the
    variance, the tolerance, every rule that passed or failed, and the
    deterministic reason for the outcome.

    It is built HERE, by the same pass that sets `reject` / `review`, and not by
    a second function that re-derives the outcome. That is the point: a trail
    assembled by re-running the logic is a trail that can disagree with the
    decision it claims to explain. Each `_check(...)` call sits next to the
    branch it describes and reads the same variable that branch reads.

    No model is involved in any of it. Every sentence in the trail is written by
    this function from numbers computed by Python.

    `extracted` is optional and supplies invoice identity (number, vendor) for
    the trail. When it is absent those fields are null rather than guessed.

    Each reason is {"text": ..., "level": "ok"|"warn"|"fail"|"info"} so the UI can
    colour-code the trail rather than showing a flat list of bullets. "fail" marks
    the findings that actually drove a REJECT/REVIEW; "ok" marks checks that passed.

    `extract_info` is the dict returned alongside the invoice by
    extraction.extract_invoice(): which route ran, whether a text layer existed,
    and any notes about degraded extraction.

    `low_confidence` is the list validate_confidence() returns: gated fields
    the extractor itself is not confident it read correctly. Passed in rather
    than computed here, matching how `arithmetic`/`amount`/`missing_fields`
    already arrive pre-computed -- main.py needs the same result for its own
    stage messaging and this keeps there being exactly one computation of it.
    """
    reasons = []
    reject = False
    review = False
    checks = []

    def add(text, level="info"):
        reasons.append({"text": text, "level": level})

    def _check(name, passed, detail, reason=None):
        """Record one named rule result for the audit trail.

        `reason` is the short, canonical sentence used as THE reason for the
        decision when this is the first rule to fail -- deterministic text
        chosen by the branch, never generated.
        """
        checks.append({
            "name": name,
            "passed": bool(passed),
            "detail": detail,
            "reason": reason if not passed else None,
        })

    route = (extract_info or {}).get("route")

    # Security screening comes first: if the document was carrying text aimed at
    # the extractor, that fact outranks every ordinary finding below and a human
    # must see it. It forces review, never rejection -- the invoice may well be
    # legitimate, and auto-rejecting on a keyword would hand anyone a way to
    # block a competitor's payment by printing a phrase on their invoice.
    security_flags = (extract_info or {}).get("security_flags") or []
    if security_flags:
        review = True
        add(
            "SECURITY: this document contains text that reads as an instruction to the "
            "extraction system rather than invoice data. The values below were transcribed "
            "as they appeared and no instruction was acted on, but the document is not "
            "trustworthy input. Verify it against the vendor before paying. "
            f"Detected — {'; '.join(security_flags[:5])}"
            + (f" (and {len(security_flags) - 5} more)" if len(security_flags) > 5 else ""),
            "fail",
        )
    _check("Security screen", not security_flags,
           f"{len(security_flags)} instruction-like finding(s) in the document"
           if security_flags else "No instruction-like text found in the document",
           reason="Document contains text aimed at the extraction system.")

    if route == "none":
        review = True
        add(
            "Nothing could be read from this document — it has no embedded text layer and no "
            "vision extraction was available. Refusing to guess at field values; route for "
            "manual entry or ask the vendor to re-send a text-based PDF.",
            "fail",
        )
    _check("Document readable", route != "none",
           "Nothing could be read from the document" if route == "none"
           else f"Fields extracted via route '{route}'",
           reason="Nothing could be read from the document.")

    # Is this an invoice at all?
    #
    # Everything below assumes the document IS one and asks whether it may be
    # paid. Without this check a CV, a contract or a policy document lands in
    # the AP review queue reported as an invoice with "missing required fields"
    # — technically true, and useless to the person reading it.
    #
    # This REJECTS rather than holds. A hold means "a human must decide whether
    # to pay this", and there is nothing to decide about a document that
    # contains no invoice. The cost is that a genuine invoice so degraded that a
    # model finds not one field in it is rejected rather than queued; that is
    # accepted deliberately, the reason below says exactly what was observed,
    # and an administrator can move the run back through /status.
    not_invoice = is_not_an_invoice(extracted, extract_info)
    if not_invoice:
        reject = True
        add(
            "This document does not appear to be an invoice. It was read successfully, but it "
            "contains no vendor, invoice number, amount, date, purchase-order reference or line "
            "item — nothing that identifies it as something to be paid. Check that the right "
            "file was submitted.",
            "fail",
        )
    _check("Document is an invoice", not not_invoice,
           "No invoice fields were found in a document that was read successfully"
           if not_invoice else "Recognised as an invoice",
           reason="The document does not appear to be an invoice.")

    if route == "gemini-vision":
        add(
            "No embedded text layer — fields were read from page images rather than text. "
            "Values are worth a second look before payment.",
            "warn",
        )

    for note in (extract_info or {}).get("notes", []):
        add(note, "warn")

    if missing_fields:
        review = True
        add(f"Missing required field(s): {', '.join(missing_fields)}. Cannot safely auto-approve.", "fail")
    _check("Required fields present", not missing_fields,
           f"Missing: {', '.join(missing_fields)}" if missing_fields
           else "All required fields present",
           reason=(f"Required field(s) missing: {', '.join(missing_fields)}."
                   if missing_fields else None))

    # Confidence sits right after presence: a field the extractor found but is
    # not sure of is a different problem from a field it never found, and a
    # more fundamental one than whether the NUMBERS reconcile (arithmetic,
    # amount, PO balance) -- those all assume the readings feeding them are
    # trustworthy, which is exactly what this check is questioning.
    #
    # Only ever holds for review, same as every other extraction-uncertainty
    # signal in this pipeline (unreadable scan, injection guard). Low
    # confidence about a READING is not evidence the invoice itself is wrong.
    if low_confidence:
        review = True
        parts = "; ".join(
            f"{f['field']} ({f['confidence'] * 100:.0f}%"
            + (f", {f['source']}" if f.get("source") else "")
            + ")"
            for f in low_confidence
        )
        add(
            f"Low extraction confidence on {len(low_confidence)} field(s) central to this "
            f"decision: {parts}. The extractor itself is not confident these values were "
            f"read correctly — confirm against the original document before approving. "
            f"(Self-reported by the model; not independently verified.)",
            "fail",
        )
    _check("Extraction confidence", not low_confidence,
           f"{len(low_confidence)} gated field(s) below "
           f"{config.CONFIDENCE_THRESHOLD * 100:.0f}% confidence" if low_confidence
           else "All gated fields at or above the confidence threshold",
           reason="Extractor confidence below threshold on a field central to the decision."
           if low_confidence else None)

    # An invalid total is checked before the arithmetic and before any PO
    # reasoning, because every one of those compares against `total`. If the
    # figure itself is not a payable amount, none of what follows means anything.
    if amount:
        review = True
        if amount["kind"] == "negative":
            add(
                f"Invalid invoice amount: total must be greater than zero, but this invoice "
                f"states ${amount['total']:.2f}. A negative total is a credit note rather than "
                f"a payable invoice — paying it would move money the wrong way. Route to AP to "
                f"confirm whether a credit was intended.",
                "fail",
            )
        else:
            add(
                "Invalid invoice amount: total must be greater than zero, but this invoice "
                "states $0.00. Nothing is payable, and a zero total is more often a misread "
                "figure than a genuine zero-value bill. Confirm the amount against the document.",
                "fail",
            )
    _check("Invoice amount valid", not amount,
           f"Total {amount['total']:.2f} is not a payable amount" if amount
           else "Total is greater than zero",
           reason="Invoice total is not a payable amount." if amount else None)

    # Arithmetic sits with the other document-integrity checks, before any PO
    # reasoning: if the invoice does not add up, which figure the PO should be
    # compared against is itself in question.
    if arithmetic:
        review = True
        add(
            f"Invoice arithmetic mismatch: subtotal + tax does not equal total. "
            f"Subtotal ${arithmetic['subtotal']:.2f} + tax ${arithmetic['tax']:.2f} "
            f"= ${arithmetic['expected']:.2f}, but the invoice states ${arithmetic['total']:.2f} "
            f"— a difference of ${arithmetic['diff']:.2f}. Either a figure was misread or the "
            f"invoice is wrong; confirm the payable amount before paying.",
            "fail",
        )
    _check("Invoice arithmetic", not arithmetic,
           (f"subtotal {arithmetic['subtotal']:.2f} + tax {arithmetic['tax']:.2f} "
            f"= {arithmetic['expected']:.2f}, stated total {arithmetic['total']:.2f}")
           if arithmetic else "subtotal + tax equals the stated total",
           reason="Invoice does not add up: subtotal + tax does not equal the total.")

    if dup_row:
        reject = True
        add(dup_detail, "fail")
    _check("Duplicate check", not dup_row,
           f"Matches earlier run #{dup_row['id']}" if dup_row
           else "No earlier run matches this invoice",
           reason="Invoice duplicates an earlier submission.")

    if vendor_ok is False:
        reject = True
        add(vendor_detail, "fail")
    elif vendor_ok is None:
        review = True
        add(vendor_detail, "warn")
    else:
        add(vendor_detail, "ok")
    _check("Vendor approved", vendor_ok is True, vendor_detail,
           reason=("Vendor is not on the approved list."
                   if vendor_ok is False else "Vendor could not be identified confidently."))

    if po_match["po_number"] is None:
        review = True
        # Say *why* nothing bound. "No PO found" sends a clerk hunting; "two POs
        # were equally plausible" tells them exactly what to decide.
        inference = po_match.get("inference")
        if inference == "ambiguous":
            add(
                "No explicit PO reference, and more than one purchase order for this vendor "
                "matches the invoice amount closely enough to be plausible. Binding to either "
                "would be a guess, so none was chosen — confirm which PO this belongs to.",
                "fail",
            )
        elif inference == "no_close_candidate":
            add(
                "No explicit PO reference, and no purchase order for this vendor is close "
                "enough in amount to infer one. The invoice was not bound to a PO.",
                "fail",
            )
        else:
            add("No matching purchase order found (no explicit PO reference, and no vendor+amount match).", "fail")
        _check("PO matched", False,
               f"No purchase order bound (inference: {po_match.get('inference') or 'not attempted'})",
               reason="No matching purchase order could be identified.")
    else:
        allocations = po_match.get("allocations") or []
        is_multi = bool(po_match.get("is_multi"))
        po_list = ", ".join(po_match.get("po_numbers") or [po_match["po_number"]])

        _check("PO matched", True,
               f"{po_list} ({po_match['matched_via']} match)" if is_multi
               else f"{po_match['po_number']} ({po_match['matched_via']} match)")

        # An invoice covering several POs is represented correctly -- each PO is
        # bound and the total is split across them -- but it is never approved
        # automatically, and the reason is not caution for its own sake.
        #
        # Nothing on the document states the split. Line items do not carry PO
        # references, so the division is computed by split_across(): fill each PO
        # to its remaining balance in the order the invoice named them. That is a
        # reasonable proposal and it is not evidence. Approving it would commit
        # money against purchase orders in amounts no human and no document ever
        # specified -- the same objection that already holds an INFERRED single-PO
        # match for review, applied to the division rather than to the binding.
        #
        # The proposal is still computed, stored and shown, so the reviewer
        # confirms figures rather than working them out.
        if is_multi:
            review = True
            add(
                f"This invoice covers {len(allocations)} purchase orders ({po_list}). "
                f"The document does not state how much belongs to each, so the split below "
                f"was calculated — each PO filled to its remaining balance in the order the "
                f"invoice referenced them — and is a proposal for a person to confirm, not "
                f"grounds for automatic approval. "
                + "; ".join(f"{a['po_number']} ${a['amount']:.2f} of ${a['remaining_before']:.2f} remaining"
                            for a in allocations),
                "fail",
            )
        _check("Invoice-to-PO split stated", not is_multi,
               f"Invoice spans {len(allocations)} POs and the document states no split"
               if is_multi else "Invoice charges a single purchase order",
               reason="Invoice covers multiple purchase orders and the split between them "
                      "was calculated rather than stated on the document.")

        if po_match["po_status"] == "closed":
            review = True
            closed = po_match.get("closed_pos") or [po_match["po_number"]]
            add(f"Matched PO {', '.join(closed)} but it is already closed."
                if len(closed) == 1 else
                f"Matched POs {', '.join(closed)} but they are already closed.", "fail")

        # Currency is checked before any of the amount reasoning below, because
        # when the units differ none of that reasoning means anything: "within
        # tolerance" compares two bare numbers, and 3,000 of one currency is not
        # 3,000 of another.
        #
        # Three outcomes when currencies differ, not one:
        #
        #  1. SAME RAW NUMBER, different currency ("1500" billed as EUR against
        #     a "1500" USD PO). No correct conversion produces identical digits
        #     in a different currency, so this is not an ordinary discrepancy
        #     for a human to reconcile -- it is a currency-code error that would
        #     silently mis-pay by the full FX difference, or a copied figure.
        #     REJECTED outright.
        #  2. A PINNED, versioned exchange rate (config.FX_RATES) resolves the
        #     conversion within tolerance. Approved -- the reason a live-fetched
        #     rate was refused ("not reproducible by an auditor") does not apply
        #     to a table that is pinned and stamped with a version, and the
        #     audit trail records exactly which version priced it.
        #  3. No pinned rate is available for the pair, or the converted amount
        #     still does not fit. Held for a human, same as before this existed.
        fx = po_match.get("fx") or {}
        suspected = bool(po_match.get("currency_same_number_suspected"))
        mismatch = bool(po_match.get("currency_mismatch"))
        fx_resolved = mismatch and fx.get("applied") and po_match["within_tolerance"] and not suspected
        # The figure actually compared against the PO balance below -- the
        # converted amount when a conversion applied, the raw total otherwise.
        # `remaining_before`/`diff`/`tolerance` are always in the PO's currency,
        # so this must be too or the comparison sentence mixes units.
        compared_total = fx["converted_total"] if fx.get("applied") else po_match["invoice_total"]

        if suspected:
            reject = True
            add(
                f"Invoice states {po_match['invoice_total']:.2f} {po_match['invoice_currency']} — "
                f"the exact same figure as PO {po_match['po_number']}, but in a different "
                f"currency ({po_match['po_currency']}). No correct currency conversion produces "
                f"identical digits, so this reads as a currency-code error or a copied number "
                f"rather than a legitimate invoice — rejected rather than held."
                + (f" Converted at the pinned rate it is actually "
                   f"{fx['converted_total']:.2f} {po_match['po_currency']}, not "
                   f"{po_match['invoice_total']:.2f}."
                   if fx.get("applied") else ""),
                "fail",
            )
        elif fx_resolved:
            add(
                f"Currency converted: invoice is {po_match['invoice_total']:.2f} "
                f"{po_match['invoice_currency']}, which is {fx['converted_total']:.2f} "
                f"{po_match['po_currency']} at the pinned rate {fx['rate']:.4f} "
                f"(FX table v{fx['rate_version']}) — within tolerance of PO "
                f"{po_match['po_number']}. The rate is pinned and versioned, not fetched at "
                f"run time, so this figure is reproducible by an auditor.",
                "warn",
            )
        elif mismatch:
            review = True
            if fx.get("applied"):
                add(
                    f"Currency mismatch: invoice is {po_match['invoice_total']:.2f} "
                    f"{po_match['invoice_currency']}, PO {po_match['po_number']} is "
                    f"{po_match['po_currency']}. Converted at the pinned rate "
                    f"(v{fx['rate_version']}) it is {fx['converted_total']:.2f} "
                    f"{po_match['po_currency']}, which does not fit the remaining balance "
                    f"within tolerance — confirm before payment.",
                    "fail",
                )
            else:
                add(
                    f"Currency mismatch: invoice is {po_match['invoice_currency']}, "
                    f"PO {po_match['po_number']} is {po_match['po_currency']}. No pinned "
                    f"exchange rate is available for this pair, so it cannot be converted — "
                    f"confirm the correct amount before payment.",
                    "fail",
                )
        _check("Currency match", not mismatch or fx_resolved,
               f"invoice {po_match.get('invoice_currency') or 'unknown'} vs "
               f"PO {po_match.get('po_currency') or 'unknown'}"
               + (f", converts to {fx['converted_total']:.2f} at v{fx['rate_version']}"
                  if fx.get("applied") else ""),
               reason="Invoice currency does not match the purchase order currency.")
        _check("Currency/amount not reused across currencies", not suspected,
               "Invoice figure matches the PO's raw amount under a different currency code"
               if suspected else "No currency/amount collision detected",
               reason="Invoice states the purchase order's own figure under a different "
                      "currency, which no correct conversion would produce.")

        if po_match["matched_via"] == "inferred":
            # An inferred match is a suggestion, not an authorisation. The
            # invoice never named this PO -- the process picked it -- so a human
            # confirms the binding before money moves. Previously this was a
            # warn-level note that changed nothing, which meant an invoice that
            # named no PO at all could auto-approve against a PO the process
            # guessed. The note now carries the verdict with it.
            review = True
            add(
                f"No explicit PO reference on the invoice — {po_match['po_number']} was "
                f"inferred from the vendor and a matching amount. The invoice never named "
                f"this PO, so the match is a suggestion for a human to confirm, not grounds "
                f"for automatic approval.",
                "warn",
            )
        elif is_multi:
            add(f"Matched explicit PO references {po_list}.", "ok")
        else:
            add(f"Matched explicit PO reference {po_match['po_number']}.", "ok")

        if po_match["remaining_before"] is not None and po_match["remaining_before"] < po_match["po_amount"]:
            # For a multi-PO invoice these are sums across every bound PO, so
            # the sentence has to say so -- naming the primary beside a combined
            # figure would attribute the other POs' budget to it.
            if is_multi:
                add(
                    f"POs {po_list} authorise ${po_match['po_amount']:.2f} between them, "
                    f"${po_match['po_amount'] - po_match['remaining_before']:.2f} already consumed by "
                    f"prior approved invoices, ${po_match['remaining_before']:.2f} remaining before "
                    f"this invoice. Per PO: "
                    + "; ".join(f"{a['po_number']} ${a['remaining_before']:.2f}" for a in allocations)
                    + ".",
                    "info",
                )
            else:
                add(
                    f"PO {po_match['po_number']} had ${po_match['po_amount']:.2f} total, "
                    f"${po_match['po_amount'] - po_match['remaining_before']:.2f} already consumed by prior "
                    f"approved invoices, ${po_match['remaining_before']:.2f} remaining before this invoice.",
                    "info",
                )

        _check(
            "PO remaining check", po_match["within_tolerance"],
            (f"invoice {compared_total:.2f} vs remaining "
             f"{po_match['remaining_before']:.2f}, variance {po_match['diff']:.2f}, "
             f"tolerance {po_match['tolerance']:.2f}"
             + (f" (converted from {po_match['invoice_total']:.2f} "
                f"{po_match['invoice_currency']})" if fx.get("applied") else ""))
            if po_match["remaining_before"] is not None else "no balance to compare",
            reason="Invoice total exceeds PO remaining amount.")

        if not po_match["within_tolerance"]:
            review = True
            add(
                f"Invoice total is ${po_match['diff']:.2f} over the "
                + (f"combined remaining balance of {po_list}" if is_multi
                   else "remaining PO balance")
                + f" of ${po_match['remaining_before']:.2f} — outside the "
                  f"${po_match['tolerance']:.2f} tolerance. The vendor is billing for more than is "
                + ("currently authorized on these POs." if is_multi
                   else "currently authorized on this PO."),
                "fail",
            )
        elif po_match.get("over_within_tolerance"):
            # Approved for MORE than the PO authorises. Allowed, because tax and
            # freight are added after a PO is raised, but it must never pass
            # silently -- the overage is named in dollars so an auditor reading
            # the trail can see exactly what was waved through and under which
            # threshold.
            add(
                f"Invoice total is ${po_match['diff']:.2f} over the remaining PO balance of "
                f"${po_match['remaining_before']:.2f}, within the ${po_match['tolerance']:.2f} "
                f"tax/shipping tolerance. Approved under tolerance rather than blocked; "
                f"the overage is authorised by policy, not by the purchase order.",
                "warn",
            )
        elif po_match["is_partial"]:
            invoice_total = po_match["remaining_before"] + po_match["diff"]
            add(
                f"Invoice total (${invoice_total:.2f}) is less than the remaining PO balance "
                f"of ${po_match['remaining_before']:.2f} — treated as a partial invoice against a PO being "
                f"split across multiple bills. ${po_match['remaining_after']:.2f} will remain on "
                f"{po_match['po_number']} after this one.",
                "ok",
            )
        else:
            add(
                f"Invoice total matches "
                + (f"the combined remaining balance of {po_list}" if is_multi
                   else "remaining PO balance")
                + f" within tolerance (diff ${po_match['diff']:.2f}, "
                  f"tolerance ${po_match['tolerance']:.2f}).",
                "ok",
            )

    # Line items, LAST among the PO checks and deliberately so: it asks a
    # question that only makes sense once a PO has been bound and its total
    # compared. Note it can fire on an invoice whose total matched the PO
    # exactly -- that is the entire point of it, and why it cannot be folded
    # into the balance check above.
    li = line_item_check(extracted or {}, po_match)
    if li["findings"]:
        review = True
        add(
            "Line items do not agree with the purchase order. The invoice total may still "
            "match what was authorised, so the balance checks above can pass while the "
            "quantities or prices underneath them have changed. Confirm the delivery and "
            "the agreed price before paying — "
            + "; ".join(f"{f['item']}: {f['detail']}" for f in li["findings"][:5])
            + (f" (and {len(li['findings']) - 5} more)" if len(li["findings"]) > 5 else ""),
            "fail",
        )
    _check(
        LINE_ITEM_RULE, not li["findings"],
        ((f"{len(li['findings'])} line-item discrepancy(ies)"
          + (f" across {li['compared']} compared line(s)" if li["compared"] else ""))
         if li["findings"] else
         (f"{li['compared']} line(s) compared; quantities, unit prices and line totals agree"
          if li["compared"] else f"not compared — {li['skipped_because']}")),
        reason="Invoice line items do not match the purchase order.",
    )

    if reject:
        status = "REJECTED"
    elif review:
        status = "NEEDS_REVIEW"
    else:
        status = "APPROVED"

    if audit is not None:
        audit.update(build_audit(status, checks, reasons, extract_info, po_match, extracted,
                                 missing_fields=missing_fields, low_confidence=low_confidence))

    return status, reasons


# Which fields a failing rule implicates, for the reviewer's "problematic
# field" view. Static and hand-written, same spirit as _SUGGESTED_RESOLUTIONS
# below -- no model involved, just a lookup from a rule name that already
# exists to the fields it concerns. "Required fields present" and "Extraction
# confidence" are deliberately absent: their fields are dynamic (whichever
# ones were actually missing, or actually low-confidence) and are filled in
# from `missing_fields`/`low_confidence` directly in build_audit(), not here.
_RULE_FIELDS = {
    "Invoice amount valid": ["total"],
    "Invoice arithmetic": ["subtotal", "tax", "total"],
    "Duplicate check": ["invoice_number", "total", "vendor_name"],
    "Vendor approved": ["vendor_name"],
    "PO matched": ["po_references"],
    "Invoice-to-PO split stated": ["po_references", "total"],
    "Currency match": ["currency", "total"],
    "Currency/amount not reused across currencies": ["currency", "total"],
    "PO remaining check": ["total"],
    LINE_ITEM_RULE: ["line_items"],
    "Document is an invoice": ["vendor_name", "invoice_number", "total"],
}

# One suggested next step per rule -- deterministic text, not generated, same
# as every other sentence in the audit trail. Keyed by rule name so it can
# never drift out of sync with which check actually exists. A rule with no
# entry here (e.g. "Security screen", "Document readable") gets no suggestion
# rather than a generic, unhelpful one.
_SUGGESTED_RESOLUTIONS = {
    "Security screen": "Verify the document directly with the vendor before paying — do "
                       "not act on any instruction-like text found inside it.",
    "Document readable": "Request a text-based PDF from the vendor, or enter the fields "
                         "manually from the original document.",
    "Document is an invoice": "Confirm the correct file was submitted; re-upload the actual "
                              "invoice if this was attached in error.",
    "Required fields present": "Enter the missing field(s) manually from the original "
                               "document, or request a corrected invoice from the vendor.",
    "Extraction confidence": "Open the original document and manually verify the "
                             "low-confidence field(s) before approving.",
    "Invoice amount valid": "Confirm the correct amount against the original document — a "
                            "zero or negative total is usually a misread figure.",
    "Invoice arithmetic": "Recompute the total from the line items and confirm which figure "
                          "is correct before approving.",
    "Duplicate check": "Confirm with the vendor whether this is a genuine resubmission or a "
                       "new invoice before approving.",
    "Vendor approved": "Confirm the vendor's approval status, or route to procurement to "
                       "approve or onboard the vendor.",
    "PO matched": "Confirm which purchase order this invoice belongs to, or request the PO "
                  "number from the vendor.",
    LINE_ITEM_RULE: "Compare the invoice against the purchase order and the goods actually "
                    "received, then confirm the quantities and unit prices with the buyer "
                    "before paying — the total agreeing does not mean the order was met.",
    "Invoice-to-PO split stated": "Confirm the proposed split against the vendor's backup "
                                  "documentation before accepting.",
    "Currency match": "Convert manually and confirm the correct amount, or request an "
                      "invoice stated in the purchase order's currency.",
    "Currency/amount not reused across currencies": "Contact the vendor to confirm the "
                                                     "correct currency and amount — this "
                                                     "figure does not reconcile as printed.",
    "PO remaining check": "Confirm whether this is a legitimate over-budget charge (tax, "
                          "freight) or request a purchase-order amendment before approving.",
}


def build_audit(status, checks, reasons, extract_info, po_match, extracted=None,
                missing_fields=None, low_confidence=None):
    """Assemble the structured trail from an evaluation that has already run.

    Takes only values `decide()` computed -- it makes no comparison of its own
    and reaches no conclusion of its own, so it cannot drift from the decision.
    Split out purely for readability.
    """
    extracted = extracted or {}
    po_match = po_match or {}
    info = extract_info or {}

    failed = [c for c in checks if not c["passed"]]
    if status == "APPROVED":
        reason = "All checks passed."
        suggested_resolution = None
        primary_failure = None
    else:
        # The FIRST failing rule is the reason AND drives the suggestion. Checks
        # are appended in evaluation order, which is deliberate: document
        # integrity is established before anything is compared against a PO, so
        # the first failure is the one closest to the root of the problem --
        # and the suggestion should point at that same root, not a downstream
        # symptom of it.
        primary_failure = next((c for c in failed if c["reason"]), None)
        reason = (primary_failure["reason"] if primary_failure
                 else "One or more checks did not pass.")
        suggested_resolution = (_SUGGESTED_RESOLUTIONS.get(primary_failure["name"])
                                if primary_failure else None)

    # Every field ANY failing check implicates, not just the primary one -- a
    # reviewer benefits from seeing all of them, even though only the first is
    # cited as *the* reason. De-duplicated, order preserved.
    problematic_fields = []
    for c in failed:
        if c["name"] == "Required fields present":
            problematic_fields.extend(missing_fields or [])
        elif c["name"] == "Extraction confidence":
            problematic_fields.extend(f["field"] for f in (low_confidence or []))
        else:
            problematic_fields.extend(_RULE_FIELDS.get(c["name"], []))
    seen = set()
    problematic_fields = [f for f in problematic_fields if not (f in seen or seen.add(f))]

    return {
        "automated_decision": status,
        "reason": reason,
        # A deterministic next step, derived from the same rule that produced
        # `reason` -- never generated, never inferred beyond the lookup above.
        # None on APPROVED, and None on a hold/reject whose triggering rule has
        # no entry (nothing to say beyond the reason itself).
        "suggested_resolution": suggested_resolution,
        # Every field any failing check implicates. Empty on APPROVED.
        "problematic_fields": problematic_fields,
        "invoice": {
            "invoice_number": extracted.get("invoice_number"),
            "vendor": extracted.get("vendor_name"),
            "total": extracted.get("total"),
            "currency": extracted.get("currency"),
        },
        "extraction": {
            "route": info.get("route"),
            "provider": info.get("provider"),
            "method": extracted.get("extraction_method"),
            "notes": list(info.get("notes") or []),
            # What language the document was read AS (Phase L). Recorded so an
            # auditor can see it, and deliberately not consulted by anything
            # below: no check in this function branches on it, and the same
            # numbers produce the same verdict whatever it says. It is
            # provenance about the reading, in the block that already holds the
            # route and the provider -- not an input to the decision.
            "document_language": (info.get("language") or {}).get("language"),
            "document_script": (info.get("language") or {}).get("script"),
            "language_confidence": (info.get("language") or {}).get("confidence"),
        },
        "purchase_order": {
            "po_number": po_match.get("po_number"),
            "matched_via": po_match.get("matched_via"),
            "po_status": po_match.get("po_status"),
            "po_amount": po_match.get("po_amount"),
            "po_currency": po_match.get("po_currency"),
            # Provenance of the PO record itself. Null when the data layer does
            # not know -- never filled in with a plausible guess.
            "source_file": po_match.get("po_source_file"),
            "source_row": po_match.get("po_source_row"),
            # Every PO this invoice was charged against. A single-PO invoice
            # lists one, so an auditor reads the same field either way.
            "po_numbers": list(po_match.get("po_numbers") or []),
            "is_multi": bool(po_match.get("is_multi")),
        },
        # How the invoice total was divided, and against which balances. This is
        # what the ledger actually consumed, so it is the figure an auditor needs
        # -- `comparison` below describes the COMBINED position, which for a
        # multi-PO invoice is a sum and not a single PO's balance.
        #
        # `basis` says where the division came from. "single_po" is the whole
        # total on one PO and is a fact; "calculated" means the document stated
        # no split and the process proposed one, which is why such a run is
        # always held for a human.
        "allocations": [
            {
                "po_number": a.get("po_number"),
                "amount": a.get("amount"),
                "po_amount": a.get("po_amount"),
                "po_status": a.get("po_status"),
                "consumed_before": a.get("consumed_before"),
                "remaining_before": a.get("remaining_before"),
                "remaining_after": a.get("remaining_after"),
                "over": bool(a.get("over")),
                "source_file": a.get("source_file"),
                "source_row": a.get("source_row"),
            }
            for a in (po_match.get("allocations") or [])
        ],
        "allocation_basis": ("calculated" if po_match.get("is_multi")
                             else ("single_po" if po_match.get("po_number") else None)),
        "comparison": {
            # The RAW total, in the invoice's own currency, as printed.
            "invoice_total": po_match.get("invoice_total"),
            # What was actually compared against the balances below -- equal to
            # `invoice_total` unless a currency conversion applied, in which
            # case `variance`/`po_remaining`/`tolerance` are all in PO currency
            # and this is the figure that produced them.
            "invoice_total_converted": (po_match.get("fx") or {}).get("converted_total"),
            "po_amount": po_match.get("po_amount"),
            "consumed_before": po_match.get("consumed_before"),
            "po_remaining": po_match.get("remaining_before"),
            "variance": po_match.get("diff"),
            "tolerance": po_match.get("tolerance"),
            "remaining_after": po_match.get("remaining_after"),
        },
        # Present only when the invoice and PO currencies actually differ.
        # `fx` is the pinned-rate conversion attempted (see config.FX_RATES);
        # its absence when `mismatch` is true means no rate was available for
        # the pair, not that conversion was skipped.
        "currency": {
            "invoice_currency": po_match.get("invoice_currency"),
            "po_currency": po_match.get("po_currency"),
            "mismatch": bool(po_match.get("currency_mismatch")),
            "same_number_suspected": bool(po_match.get("currency_same_number_suspected")),
            "fx": po_match.get("fx"),
        },
        # Per-field confidence, source and evidence, exactly as extraction
        # produced it -- keyed by field name. Fields the extractor never
        # attempted (or was completely certain of, for regex) may be absent;
        # absence is not the same as a low score, so the UI must not treat a
        # missing entry as a red flag.
        "provenance": dict(extracted.get("provenance") or {}),
        # The subset of `provenance` that actually held up this run -- i.e.
        # what triggered the "Extraction confidence" check, if it fired.
        "low_confidence_fields": list(low_confidence or []),
        "rules": checks,
        "rules_passed": [c["name"] for c in checks if c["passed"]],
        "rules_failed": [c["name"] for c in failed],
        "reasons": list(reasons),
    }
