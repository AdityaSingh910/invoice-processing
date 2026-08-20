/** Shapes the FastAPI backend actually returns. Mirrors backend/schemas.py. */

export type Verdict = "APPROVED" | "NEEDS_REVIEW" | "REJECTED";
export type Level = "ok" | "warn" | "fail" | "info";

export interface LineItem {
  description: string;
  quantity: number | null;
  unit_price: number | null;
  amount: number | null;
}

/**
 * One field's provenance: how confident the extractor is, where the value
 * came from, and a quoted snippet backing it. LLM routes self-report the
 * confidence/evidence; regex assigns a deterministic heuristic instead. A
 * field absent from `provenance` was never attempted, or read with total
 * certainty (regex) -- that is different from carrying a low score, and
 * must not be rendered as though it were one.
 */
export interface FieldProvenance {
  confidence: number | null;
  source: string | null;
  evidence: string | null;
  /** Whether `evidence` was actually found in the extracted text. false means
   *  the model's quote does not appear there -- a real possibility, not a
   *  bug, and must be shown as such rather than silently trusted. */
  evidence_verified: boolean | null;
}

export interface Extracted {
  vendor_name: string | null;
  invoice_number: string | null;
  invoice_date: string | null;
  po_references: string[];
  line_items: LineItem[];
  subtotal: number | null;
  tax: number | null;
  total: number | null;
  currency: string;
  extraction_method: string;
  provenance?: Record<string, FieldProvenance>;
}

export interface Stage {
  name: string;
  status: Level;
  detail: string;
  ms?: number;
}

/** Reasons arrive either as plain strings (legacy) or as levelled objects. */
export type Reason = string | { level?: Level; text: string };

/**
 * How much of an invoice was charged to one purchase order.
 *
 * An ordinary invoice has exactly one of these, for its full total. An invoice
 * spanning several POs has one per PO, and they always sum to the invoice total
 * — that invariant is what the ledger consumes against.
 */
export interface Allocation {
  po_number: string;
  amount: number;
  po_amount?: number | null;
  po_vendor?: string | null;
  po_status?: string | null;
  consumed_before?: number | null;
  remaining_before?: number | null;
  remaining_after?: number | null;
  /** True when this PO was charged beyond its remaining balance. */
  over?: boolean;
  source_file?: string | null;
  source_row?: number | null;
}

export interface PoMatch {
  /** The FIRST PO referenced. Kept for display; the money is in `allocations`. */
  po_number: string | null;
  po_vendor?: string | null;
  /** Combined across every bound PO when the invoice spans more than one. */
  po_amount?: number | null;
  po_status?: string | null;
  matched_via?: string | null;
  invoice_total?: number | null;
  consumed_before?: number | null;
  remaining_before?: number | null;
  remaining_after?: number | null;
  within_tolerance?: boolean;
  is_partial?: boolean;
  diff?: number | null;
  tolerance?: number | null;
  po_numbers?: string[];
  allocations?: Allocation[];
  is_multi?: boolean;
  closed_pos?: string[];
  currency_mismatch?: boolean;
  invoice_currency?: string | null;
  po_currency?: string | null;
  /** Present only when `currency_mismatch` is true. */
  fx?: FxConversion | null;
  /** True when the invoice states the PO's own figure under a different
   *  currency -- no correct conversion produces identical digits, so this is
   *  rejected outright rather than held. */
  currency_same_number_suspected?: boolean;
}

/** A currency conversion attempted at the pinned, versioned rate table. */
export interface FxConversion {
  applied: boolean;
  from_currency: string | null;
  to_currency: string | null;
  rate: number | null;
  rate_version: string | null;
  converted_total: number | null;
}

export interface AuditRule {
  name: string;
  passed: boolean;
  detail?: string;
}

export interface Audit {
  automated_decision?: string;
  reason?: string;
  invoice?: { invoice_number?: string; vendor?: string; total?: number | null; currency?: string };
  purchase_order?: {
    po_number?: string | null;
    matched_via?: string | null;
    po_status?: string | null;
    po_currency?: string | null;
    source_file?: string | null;
    source_row?: number | null;
    po_numbers?: string[];
    is_multi?: boolean;
  };
  allocations?: Allocation[];
  /**
   * Where the division of the invoice across POs came from.
   * `single_po` — the whole total on one PO, which is a fact.
   * `calculated` — the document stated no split and the process proposed one,
   *   which is why such a run is always held for a human.
   */
  allocation_basis?: "single_po" | "calculated" | null;
  comparison?: {
    /** Raw, in the invoice's own currency, as printed. */
    invoice_total?: number | null;
    /** What was actually compared against the PO balances -- equal to
     *  `invoice_total` unless a conversion applied. */
    invoice_total_converted?: number | null;
    po_amount?: number | null;
    consumed_before?: number | null;
    po_remaining?: number | null;
    variance?: number | null;
    tolerance?: number | null;
  };
  /** Present whenever the invoice and PO currencies differ. */
  currency?: {
    invoice_currency?: string | null;
    po_currency?: string | null;
    mismatch?: boolean;
    same_number_suspected?: boolean;
    fx?: FxConversion | null;
  };
  extraction?: { method?: string; route?: string };
  rules?: AuditRule[];
  /** One deterministic next step, derived from the same rule that produced
   *  `reason` -- static text keyed by rule name, never generated. None on
   *  APPROVED, or when the triggering rule has no suggestion of its own. */
  suggested_resolution?: string | null;
  /** Every field any failing check implicates, de-duplicated. Empty on
   *  APPROVED. */
  problematic_fields?: string[];
  /** Per-field confidence/source/evidence, exactly as extraction produced it.
   *  A field absent here was never attempted or was read with total
   *  certainty -- not the same as a low score. */
  provenance?: Record<string, FieldProvenance>;
  /** The subset of `provenance` that actually triggered the "Extraction
   *  confidence" check, if it fired. */
  low_confidence_fields?: {
    field: string;
    confidence: number | null;
    source?: string | null;
    evidence?: string | null;
  }[];
}

/** The live result streamed by POST /api/runs/stream. */
export interface RunResult {
  run_id: number;
  filename: string;
  status: Verdict;
  reasons: Reason[];
  extracted: Extracted;
  po_match: PoMatch;
  stages: Stage[];
  audit?: Audit;
  created_at?: string;
}

/** A stored run from GET /api/runs. */
export interface RunRecord {
  id: number;
  filename: string;
  vendor_name: string | null;
  invoice_number: string | null;
  total: number | null;
  po_number: string | null;
  status: Verdict;
  created_at: string;
  reasons: Reason[];
  stages: Stage[];
  /** Present on every stored run; carries the allocations the ledger consumed. */
  po_match?: PoMatch;
  audit?: Audit;
  automated_decision?: Verdict;
  human_decision?: string | null;
  final_decision?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  review_note?: string | null;
}

export interface PurchaseOrder {
  po_number: string;
  vendor: string;
  amount: number;
  status: string;
}

export interface Vendor {
  vendor_name: string;
  vendor_id: string;
  status: string;
}

export interface Reference {
  purchase_orders: PurchaseOrder[];
  vendors: Vendor[];
}

export interface SampleInvoice {
  filename: string;
  label?: string;
  note?: string;
  expect?: Verdict;
}

export interface Identity {
  username: string;
  scopes: string[];
}

/** Server-sent events emitted by the pipeline. */
export type RunEvent =
  | { type: "stage"; stage: Stage }
  | { type: "final"; result: RunResult }
  | { type: "error"; error: string };
