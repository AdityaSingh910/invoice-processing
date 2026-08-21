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

/* ------------------------------------------------------------- analytics
 * Phase H. Shapes returned by /api/analytics/*.
 *
 * `number | null` is load-bearing throughout and is NOT a convenience for
 * missing data: the backend returns null for a rate whose denominator is zero,
 * because "no invoices were processed" and "0% were automated" are different
 * statements and only one of them is true on a quiet day. Anything rendering
 * these must treat null as "not available", never coerce it to 0.
 */

export interface AnalyticsRange {
  key: string;
  label: string;
  from: string | null;
  to: string | null;
  timezone: string;
}

/** A KPI ships the arithmetic behind it, so the UI can show the counts and can
 *  decline to render a percentage computed from three runs. */
export interface Kpi {
  value: number | null;
  numerator: number;
  denominator: number;
  definition: string;
}

export interface CurrencyValue {
  runs: number;
  processed: number;
  approved: number;
  held: number;
  rejected: number;
}

export interface AnalyticsOverview {
  range: AnalyticsRange;
  generated_at: string;
  volume: {
    runs: number;
    automated: number;
    held: number;
    reviewed: number;
    overridden: number;
    extraction_failures: number;
  };
  kpis: {
    automation_rate: Kpi;
    processing_success_rate: Kpi;
    task_success_ratio: Kpi;
    human_review_rate: Kpi;
    review_completion_rate: Kpi;
  };
  decisions: {
    automated: Record<string, number>;
    human: Record<string, number>;
    status: Record<string, number>;
  };
  value_by_currency: Record<string, CurrencyValue>;
  backlog: {
    awaiting_review: number;
    claimed_now: number;
    oldest_awaiting_at: string | null;
    oldest_awaiting_age_seconds: number | null;
  };
  data_quality: {
    runs_scanned: number;
    runs_with_timing: number;
    malformed_json: Record<string, number>;
    malformed_total: number;
  };
}

export interface TrendBucket {
  day: string;
  runs: number;
  approved: number;
  needs_review: number;
  rejected: number;
  reviewed: number;
  automation_rate: number | null;
  approval_rate: number | null;
  rejection_rate: number | null;
  review_rate: number | null;
  avg_processing_ms: number | null;
  timed_runs: number;
}

export interface AnalyticsTrends {
  range: AnalyticsRange;
  timezone: string;
  buckets: TrendBucket[];
}

export interface StageStat {
  stage: string;
  runs: number;
  total_ms: number;
  statuses: Record<string, number>;
  samples: number;
  average: number | null;
  median: number | null;
  p95: number | null;
  min: number | null;
  max: number | null;
  share_of_time: number | null;
}

export interface AnalyticsProcessing {
  range: AnalyticsRange;
  run_time_ms: {
    samples: number;
    average: number | null;
    median: number | null;
    p95: number | null;
    min: number | null;
    max: number | null;
  };
  stages: StageStat[];
  extraction: {
    by_route: Record<string, number>;
    by_provider: Record<string, number>;
    failures: number;
    failure_rate: number | null;
  };
  quota: {
    today: string;
    providers: {
      provider: string;
      used_today: number;
      limit: number;
      remaining: number;
      utilisation: number | null;
    }[];
    /** Always false: this application persists request counts, never cost. */
    cost_available: boolean;
    note: string;
  };
  /** What this window's answer was built on, and what it had to skip. Same
   *  shape the overview carries — modelled here too so the type describes the
   *  whole payload rather than only the part this screen happens to read. */
  data_quality: {
    runs_scanned: number;
    runs_with_timing: number;
    malformed_json: Record<string, number>;
    malformed_total: number;
  };
}

export interface LatencyBlock {
  samples: number;
  average_seconds: number | null;
  median_seconds: number | null;
  min_seconds: number | null;
  max_seconds: number | null;
  definition: string;
  unclaimed_reviews?: number;
}

export interface AnalyticsReviews {
  range: AnalyticsRange;
  funnel: {
    runs: number;
    held_for_review: number;
    ruled_on: number;
    accepted: number;
    rejected: number;
    still_awaiting: number;
  };
  rates: Record<string, Kpi>;
  transitions: {
    automated: string;
    human: string;
    final_status: string;
    final_decision: string;
    n: number;
  }[];
  latency: {
    time_to_decision: LatencyBlock;
    handling_time: LatencyBlock;
  };
  reasons: { rule: string; runs: number; share_of_runs: number | null }[];
  activity: Record<string, number>;
}

export interface VendorStat {
  vendor: string;
  runs: number;
  approved: number;
  held: number;
  rejected: number;
  reviewed: number;
  approved_now: number;
  approval_rate: number | null;
  hold_rate: number | null;
  rejection_rate: number | null;
  avg_processing_ms: number | null;
  timed_runs: number;
}

export interface PoStat {
  po_number: string;
  vendor: string | null;
  currency: string | null;
  status: string | null;
  amount: number;
  /** Ledger figures: all time, by design -- a balance "as of last week" would
   *  be meaningless to anyone approving an invoice against the PO. */
  consumed: number;
  remaining: number;
  utilisation: number | null;
  over_budget: boolean;
  /** Activity figures: inside the selected reporting window. */
  runs_in_range: number;
  approved_in_range: number;
  held_in_range: number;
  rejected_in_range: number;
  allocated_approved_in_range: number;
}

export interface AnalyticsVendors {
  range: AnalyticsRange;
  vendors: VendorStat[];
  truncated: boolean;
  purchase_orders: PoStat[];
}

export interface AnalyticsEmail {
  range: AnalyticsRange;
  funnel: {
    received: number;
    relevant: number;
    filtered_out: number;
    relevance_unrecorded: number;
    admitted: number;
    quarantined: number;
    discarded: number;
    with_pdf_attachment: number;
    attachments: number;
    invoice_candidates: number;
    runs_created: number;
    runs_approved: number;
    runs_held: number;
    runs_rejected: number;
  };
  rates: Record<string, Kpi>;
  by_relevance: Record<string, number>;
  by_classification: Record<string, number>;
  by_security_status: Record<string, number>;
  by_ingest_status: Record<string, number>;
  by_sender_type: Record<string, number>;
  by_trust_status: Record<string, number>;
  attachments_by_status: Record<string, number>;
}

export interface UserStat {
  username: string;
  reviews: number;
  accepted: number;
  rejected: number;
  accept_rate: number | null;
  avg_time_to_decision_seconds: number | null;
  median_time_to_decision_seconds: number | null;
  last_review_at: string | null;
  claims_held_now: number;
  events: Record<string, number>;
}

export interface AnalyticsUsers {
  range: AnalyticsRange;
  /** "self" unless the caller holds invoice:admin. The server decides this
   *  from the token, never from a query parameter. */
  scope: "self" | "all";
  viewer: string;
  users: UserStat[];
  note: string | null;
}
