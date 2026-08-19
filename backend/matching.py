"""PO matching, including split-PO balance tracking."""
import config
import storage


def tolerance_for(amount: float) -> float:
    """How far over `amount` an invoice may go and still auto-approve.

    Policy lives in config (PO_TOLERANCE_PERCENT / PO_TOLERANCE_DOLLARS), not
    here -- this function is the mechanism, those numbers are the decision.
    """
    return max(amount * config.PO_TOLERANCE_PERCENT, config.PO_TOLERANCE_DOLLARS)


def _norm_currency(value):
    """A comparable currency code, or None when nothing usable is present.

    Returns None rather than a default, so "we could not read a currency" stays
    distinguishable from "the currency is USD" at the point of comparison.
    """
    code = (value or "").strip().upper()
    return code or None


def empty_match(invoice_total):
    """The shape returned when no PO could be matched. Kept in one place so every
    consumer (matching, and the unreadable-file abort path) emits the same keys."""
    return {
        "po_number": None,
        "po_vendor": None,
        "po_amount": None,
        "po_status": None,
        "matched_via": "none",
        "consumed_before": None,
        "invoice_total": invoice_total,
        "remaining_before": None,
        "remaining_after": None,
        "tolerance": None,
        "diff": None,
        "within_tolerance": False,
        "is_partial": False,
        "over_within_tolerance": False,
        # Why inference declined to bind a PO, when it was attempted:
        # None | "ambiguous" | "no_close_candidate".
        "inference": None,
        "invoice_currency": None,
        "po_currency": None,
        "currency_mismatch": False,
    }


def match_po(extracted: dict, exclude_run_id=None):
    """Returns a po_match dict describing what PO (if any) this invoice lines up
    against, and the remaining balance on that PO before/after this invoice."""
    candidates = extracted.get("po_references") or []
    po_row = None
    matched_via = "none"

    for ref in candidates:
        row = storage.get_po(ref)
        if row:
            po_row = row
            matched_via = "explicit"
            break

    # No explicit reference: fall back to inferring one from vendor + amount.
    #
    # This used to take the vendor's nearest-amount PO with no distance cap and
    # no tie-breaking, so a $9,000 invoice could bind to a $200 PO simply because
    # it was the only one on file. Two guards now apply, and both must pass:
    #
    #   1. CLOSE  -- the PO amount must be within tolerance of the invoice total.
    #      Reuses tolerance_for(), so the closeness policy is the same configured
    #      number as everything else rather than a second magic constant.
    #   2. UNAMBIGUOUS -- exactly one PO may qualify. If two are equally
    #      plausible, picking either is a guess, and guessing which PO to charge
    #      is precisely the judgement that belongs to a human.
    #
    # Failing either guard binds nothing. `inference` records why, so the
    # reasoning trail can say "amount matched no PO" rather than the much less
    # useful "no PO found".
    inference = None
    if po_row is None:
        vendor = storage.find_vendor(extracted.get("vendor_name") or "")
        total = extracted.get("total")
        if vendor and total:
            pos = [p for p in storage.list_purchase_orders()
                   if p["vendor"] == vendor["vendor_name"]]
            near = [p for p in pos if abs(p["amount"] - total) <= tolerance_for(p["amount"])]
            if len(near) == 1:
                po_row = near[0]
                matched_via = "inferred"
            elif len(near) > 1:
                inference = "ambiguous"
            elif pos:
                inference = "no_close_candidate"

    if po_row is None:
        return dict(empty_match(extracted.get("total")), inference=inference)

    consumed = storage.consumed_amount_for_po(po_row["po_number"], exclude_run_id=exclude_run_id)
    remaining_before = round(po_row["amount"] - consumed, 2)
    total = extracted.get("total") or 0
    tol = tolerance_for(remaining_before if remaining_before > 0 else po_row["amount"])
    diff = round(total - remaining_before, 2)
    # Tolerance only bounds the OVER side: an invoice asking for more than the
    # remaining PO balance (beyond a small tolerance) is a real problem -- the
    # vendor is billing for money that isn't authorized. An invoice for LESS than
    # the remaining balance is a normal partial invoice against a PO that's being
    # split across multiple bills, and should not be blocked; it just leaves a
    # smaller remaining balance for the next one.
    within = diff <= tol
    is_partial = diff < -tol
    # Over the remaining balance, but inside the tax/freight allowance. This is
    # the case that must NOT be silent: it approves an invoice for more than the
    # PO authorised, so it earns an explicit audit note naming the overage.
    over_within_tolerance = 0 < diff <= tol

    # Currency. Every comparison above is a bare number: `diff = total - remaining`
    # says nothing about what unit either side is in, so a EUR 3,000 invoice
    # against a USD 5,000 PO reads as a comfortable partial. Flag it here and let
    # rules decide -- no conversion, no rate lookup, no third party.
    #
    # Only compared when BOTH sides are known. Note the honest limitation: the
    # extractor falls back to "USD" when a document carries no currency signal at
    # all, so a genuinely-unmarked invoice is indistinguishable from a
    # USD-marked one. That is an extraction question, not a matching one; the
    # comparison here errs toward review whenever the two disagree.
    invoice_currency = _norm_currency(extracted.get("currency"))
    po_currency = _norm_currency(po_row.get("currency"))
    currency_mismatch = bool(invoice_currency and po_currency
                             and invoice_currency != po_currency)

    return {
        "po_number": po_row["po_number"],
        "po_vendor": po_row["vendor"],
        "po_amount": po_row["amount"],
        "po_status": po_row["status"],
        "matched_via": matched_via,
        "consumed_before": round(consumed, 2),
        "invoice_total": round(total, 2),
        "remaining_before": remaining_before,
        "remaining_after": round(remaining_before - total, 2) if within else remaining_before,
        "tolerance": round(tol, 2),
        "diff": diff,
        "within_tolerance": within,
        "is_partial": is_partial,
        "over_within_tolerance": over_within_tolerance,
        "inference": inference,
        "invoice_currency": invoice_currency,
        "po_currency": po_currency,
        "currency_mismatch": currency_mismatch,
    }
