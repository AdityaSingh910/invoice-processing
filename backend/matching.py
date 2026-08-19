"""PO matching, including split-PO balance tracking."""
import config
import storage


def tolerance_for(amount: float) -> float:
    """How far over `amount` an invoice may go and still auto-approve.

    Policy lives in config (PO_TOLERANCE_PERCENT / PO_TOLERANCE_DOLLARS), not
    here -- this function is the mechanism, those numbers are the decision.
    """
    return max(amount * config.PO_TOLERANCE_PERCENT, config.PO_TOLERANCE_DOLLARS)


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

    if po_row is None:
        vendor = storage.find_vendor(extracted.get("vendor_name") or "")
        if vendor:
            pos = [p for p in storage.list_purchase_orders() if p["vendor"] == vendor["vendor_name"]]
            total = extracted.get("total")
            if pos and total:
                best = min(pos, key=lambda p: abs((p["amount"]) - total))
                po_row = best
                matched_via = "inferred"

    if po_row is None:
        return empty_match(extracted.get("total"))

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
    }
