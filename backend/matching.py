"""PO matching, including split-PO balance tracking."""
import config
import storage


def tolerance_for(amount: float) -> float:
    """How far over `amount` an invoice may go and still auto-approve.

    Policy lives in config (PO_TOLERANCE_PERCENT / PO_TOLERANCE_DOLLARS), not
    here -- this function is the mechanism, those numbers are the decision.
    """
    return max(amount * config.PO_TOLERANCE_PERCENT, config.PO_TOLERANCE_DOLLARS)


def _row_get(row, key):
    """Read an optional column from a PO row.

    `sqlite3.Row` raises IndexError on an unknown key rather than returning
    None, and a database created before the provenance columns existed will not
    have them. Missing provenance must read as "unknown", never as a crash.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


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
        "po_source_file": None,
        "po_source_row": None,
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
        # Every PO this invoice referenced, and how much of the total is charged
        # to each. One entry for an ordinary invoice; several for one that spans
        # purchase orders. The ledger consumes THESE, not the invoice total.
        "po_numbers": [],
        "allocations": [],
        "is_multi": False,
        "closed_pos": [],
        # Why inference declined to bind a PO, when it was attempted:
        # None | "ambiguous" | "no_close_candidate".
        "inference": None,
        "invoice_currency": None,
        "po_currency": None,
        "currency_mismatch": False,
    }


def split_across(positions, total):
    """Divide an invoice total across the POs it references, in document order.

    Each PO is filled up to its remaining balance before the next is touched,
    and the LAST one absorbs anything still unallocated. That last rule is what
    keeps the ledger honest: the allocations must sum to the invoice total, or
    the ledger is describing money nobody billed. When the invoice exceeds every
    balance combined, the excess has to land somewhere visible rather than
    vanishing, and it shows up as the final PO being over-consumed -- which the
    combined tolerance check then reports.

    Order-and-fill rather than pro-rata because it produces numbers an AP clerk
    can check against the document ("PO-1001 was settled in full, the balance
    went to PO-1002") instead of percentages that appear on no invoice.

    THIS IS A PROPOSAL, NOT A READING. Nothing on the document says how to
    divide the money -- line items do not carry PO references -- so this split is
    computed. That is exactly why a multi-PO invoice is never auto-approved; see
    decide().

    Reduces to "the whole total" for a single PO, so an ordinary invoice is not
    a separate code path.
    """
    amounts = []
    left = round(float(total or 0), 2)
    last = len(positions) - 1
    for i, pos in enumerate(positions):
        if i == last:
            amount = round(left, 2)
        else:
            cap = max(0.0, pos["remaining_before"])
            amount = round(min(left, cap), 2)
        amounts.append(amount)
        left = round(left - amount, 2)
    return amounts


def _position(po_row, exclude_run_id=None):
    """A PO's balance as it stands before this invoice."""
    consumed = storage.consumed_amount_for_po(po_row["po_number"],
                                              exclude_run_id=exclude_run_id)
    return {
        "po_number": po_row["po_number"],
        "po_vendor": po_row["vendor"],
        "po_amount": po_row["amount"],
        "po_status": po_row["status"],
        "po_currency": _norm_currency(po_row.get("currency")),
        "source_file": _row_get(po_row, "source_file"),
        "source_row": _row_get(po_row, "source_row"),
        "consumed_before": round(consumed, 2),
        "remaining_before": round(po_row["amount"] - consumed, 2),
    }


def match_po(extracted: dict, exclude_run_id=None):
    """Returns a po_match dict describing what PO (if any) this invoice lines up
    against, and the remaining balance on that PO before/after this invoice.

    An invoice may reference SEVERAL purchase orders. Every one that resolves is
    bound, and the total is split across them by split_across(); the top-level
    figures then describe the combined position, so the tolerance arithmetic
    compares the invoice against everything it was actually charged to.

    This used to bind the FIRST resolvable reference and ignore the rest, which
    charged the whole invoice to one PO -- over-consuming it by the value of the
    others while those stayed untouched.
    """
    candidates = extracted.get("po_references") or []
    po_rows = []
    seen = set()
    matched_via = "none"

    for ref in candidates:
        row = storage.get_po(ref)
        if row and row["po_number"] not in seen:
            seen.add(row["po_number"])
            po_rows.append(row)
            matched_via = "explicit"

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
    if not po_rows:
        vendor = storage.find_vendor(extracted.get("vendor_name") or "")
        total = extracted.get("total")
        if vendor and total:
            pos = [p for p in storage.list_purchase_orders()
                   if p["vendor"] == vendor["vendor_name"]]
            near = [p for p in pos if abs(p["amount"] - total) <= tolerance_for(p["amount"])]
            if len(near) == 1:
                po_rows = [near[0]]
                matched_via = "inferred"
            elif len(near) > 1:
                inference = "ambiguous"
            elif pos:
                inference = "no_close_candidate"

    if not po_rows:
        return dict(empty_match(extracted.get("total")), inference=inference)

    positions = [_position(r, exclude_run_id) for r in po_rows]
    primary = positions[0]
    is_multi = len(positions) > 1

    # The combined position. For a single PO these all reduce to that PO's own
    # figures, so an ordinary invoice takes exactly the path it always did.
    po_amount = round(sum(p["po_amount"] for p in positions), 2)
    consumed = round(sum(p["consumed_before"] for p in positions), 2)
    remaining_before = round(sum(p["remaining_before"] for p in positions), 2)
    total = extracted.get("total") or 0
    tol = tolerance_for(remaining_before if remaining_before > 0 else po_amount)
    diff = round(total - remaining_before, 2)

    # How the total is charged. For a multi-PO invoice this is a computed
    # proposal, which is why decide() holds it for a human rather than acting
    # on it; for a single PO it is simply the whole total.
    amounts = split_across(positions, total)
    allocations = [
        dict(p, amount=amt,
             remaining_after=round(p["remaining_before"] - amt, 2),
             over=amt > p["remaining_before"] + tolerance_for(
                 p["remaining_before"] if p["remaining_before"] > 0 else p["po_amount"]))
        for p, amt in zip(positions, amounts)
    ]
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
    # Any referenced PO in a different currency makes the combined comparison
    # meaningless, not just the one that differs -- the balances above were
    # summed as bare numbers.
    po_currencies = [p["po_currency"] for p in positions if p["po_currency"]]
    currency_mismatch = bool(invoice_currency and po_currencies
                             and any(c != invoice_currency for c in po_currencies))

    # Closed POs. Reported at the top level as "any of them", with the specific
    # ones named in `closed_pos` so the trail can say which rather than implying
    # it was the primary.
    closed = [p["po_number"] for p in positions if p["po_status"] == "closed"]

    return {
        # The PRIMARY po, kept so `runs.po_number`, the dashboard and every
        # existing consumer stay meaningful. For a multi-PO invoice the full set
        # is in `po_numbers` and the money is in `allocations`.
        "po_number": primary["po_number"],
        "po_vendor": primary["po_vendor"],
        "po_amount": po_amount,
        "po_status": "closed" if closed else primary["po_status"],
        # Where the PO record itself came from, carried through so the audit
        # trail can cite the source of the balance it compared against rather
        # than presenting a number with no origin. Read off the stored row --
        # never derived here, so an unknown source stays unknown.
        "po_source_file": primary["source_file"],
        "po_source_row": primary["source_row"],
        "matched_via": matched_via,
        "consumed_before": consumed,
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
        "po_currency": primary["po_currency"],
        "currency_mismatch": currency_mismatch,
        "po_numbers": [p["po_number"] for p in positions],
        "allocations": allocations,
        "is_multi": is_multi,
        "closed_pos": closed,
    }
