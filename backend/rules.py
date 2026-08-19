"""Decision rules: required fields, vendor approval, duplicates, and the final
aggregation of every check into one status + reasons trail."""
import config
import storage

REQUIRED_FIELDS = ["vendor_name", "invoice_number", "total"]


def validate_required_fields(extracted: dict):
    return [f for f in REQUIRED_FIELDS if not extracted.get(f)]


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
    if not vendor_name:
        return None, None, "No vendor name could be extracted -- cannot verify approval status."
    vendor_row = storage.find_vendor(vendor_name)
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


def decide(extract_info: dict, missing_fields, vendor_ok, vendor_detail,
           dup_row, dup_detail, po_match: dict, arithmetic=None):
    """Aggregates every check into one status plus a severity-tagged reasoning trail.

    Each reason is {"text": ..., "level": "ok"|"warn"|"fail"|"info"} so the UI can
    colour-code the trail rather than showing a flat list of bullets. "fail" marks
    the findings that actually drove a REJECT/REVIEW; "ok" marks checks that passed.

    `extract_info` is the dict returned alongside the invoice by
    extraction.extract_invoice(): which route ran, whether a text layer existed,
    and any notes about degraded extraction.
    """
    reasons = []
    reject = False
    review = False

    def add(text, level="info"):
        reasons.append({"text": text, "level": level})

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

    if route == "none":
        review = True
        add(
            "Nothing could be read from this document — it has no embedded text layer and no "
            "vision extraction was available. Refusing to guess at field values; route for "
            "manual entry or ask the vendor to re-send a text-based PDF.",
            "fail",
        )
    elif route == "llm-vision":
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

    if dup_row:
        reject = True
        add(dup_detail, "fail")

    if vendor_ok is False:
        reject = True
        add(vendor_detail, "fail")
    elif vendor_ok is None:
        review = True
        add(vendor_detail, "warn")
    else:
        add(vendor_detail, "ok")

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
    else:
        if po_match["po_status"] == "closed":
            review = True
            add(f"Matched PO {po_match['po_number']} but it is already closed.", "fail")

        # Currency is checked before any of the amount reasoning below, because
        # when the units differ none of that reasoning means anything: "within
        # tolerance" compares two bare numbers, and 3,000 of one currency is not
        # 3,000 of another. No conversion is attempted and no rate is fetched --
        # a verdict that depends on a third party and the time of day is not one
        # an auditor can reproduce. A human converts and decides.
        if po_match.get("currency_mismatch"):
            review = True
            add(
                f"Currency mismatch: invoice is {po_match['invoice_currency']}, "
                f"PO {po_match['po_number']} is {po_match['po_currency']}. The amount "
                f"comparison below treats both as the same unit, so it cannot be relied on. "
                f"No conversion was applied — confirm the correct amount before payment.",
                "fail",
            )

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
        else:
            add(f"Matched explicit PO reference {po_match['po_number']}.", "ok")

        if po_match["remaining_before"] is not None and po_match["remaining_before"] < po_match["po_amount"]:
            add(
                f"PO {po_match['po_number']} had ${po_match['po_amount']:.2f} total, "
                f"${po_match['po_amount'] - po_match['remaining_before']:.2f} already consumed by prior "
                f"approved invoices, ${po_match['remaining_before']:.2f} remaining before this invoice.",
                "info",
            )

        if not po_match["within_tolerance"]:
            review = True
            add(
                f"Invoice total is ${po_match['diff']:.2f} over the remaining PO balance of "
                f"${po_match['remaining_before']:.2f} — outside the ${po_match['tolerance']:.2f} tolerance. "
                f"The vendor is billing for more than is currently authorized on this PO.",
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
                f"Invoice total matches remaining PO balance within tolerance "
                f"(diff ${po_match['diff']:.2f}, tolerance ${po_match['tolerance']:.2f}).",
                "ok",
            )

    if reject:
        status = "REJECTED"
    elif review:
        status = "NEEDS_REVIEW"
    else:
        status = "APPROVED"

    return status, reasons
