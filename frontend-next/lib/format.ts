/** Presentation helpers. No business logic lives here -- every number shown
 *  was computed by the Python rule engine and is rendered verbatim. */

const CURRENCY_SYMBOL: Record<string, string> = {
  USD: "$", EUR: "€", GBP: "£", INR: "₹", JPY: "¥",
};

const nf = (v: number) =>
  v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Dollar-assuming formatter, for the places the original UI assumed USD. */
export function money(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return "$" + nf(Number(v));
}

/** Currency-aware formatter for the audit trail: printing "$" beside a rupee
 *  figure would make the trail say something untrue. */
export function amount(v: number | null | undefined, currency?: string | null): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const sym = CURRENCY_SYMBOL[String(currency || "").toUpperCase()];
  const n = nf(Number(v));
  return sym ? sym + n : `${n} ${currency || ""}`.trim();
}

export const humanise = (s: string | null | undefined) =>
  String(s || "").replace(/_/g, " ");

export const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString() : "";

export const LEVEL_ICON: Record<string, string> = {
  ok: "✓", warn: "!", fail: "✕", info: "i",
};

export const STAGE_ORDER = [
  "INGEST", "EXTRACT_TEXT", "EXTRACT_FIELDS", "VALIDATE", "VENDOR_CHECK",
  "PO_MATCH", "DUPLICATE_CHECK", "TOLERANCE_CHECK", "DECISION",
] as const;
