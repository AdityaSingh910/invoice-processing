/** Shapes the FastAPI backend actually returns. Mirrors backend/schemas.py. */

export type Verdict = "APPROVED" | "NEEDS_REVIEW" | "REJECTED";
export type Level = "ok" | "warn" | "fail" | "info";

export interface LineItem {
  description: string;
  quantity: number | null;
  unit_price: number | null;
  amount: number | null;
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
}

export interface Stage {
  name: string;
  status: Level;
  detail: string;
  ms?: number;
}

/** Reasons arrive either as plain strings (legacy) or as levelled objects. */
export type Reason = string | { level?: Level; text: string };

export interface PoMatch {
  po_number: string | null;
  po_vendor?: string | null;
  po_amount?: number | null;
  matched_via?: string | null;
  invoice_total?: number | null;
  consumed_before?: number | null;
  remaining_before?: number | null;
  remaining_after?: number | null;
  within_tolerance?: boolean;
  is_partial?: boolean;
  diff?: number | null;
  tolerance?: number | null;
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
  };
  comparison?: {
    invoice_total?: number | null;
    po_amount?: number | null;
    consumed_before?: number | null;
    po_remaining?: number | null;
    variance?: number | null;
    tolerance?: number | null;
  };
  extraction?: { method?: string; route?: string };
  rules?: AuditRule[];
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
