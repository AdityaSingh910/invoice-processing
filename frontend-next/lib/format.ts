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

/**
 * Plain-language names for the nine stages.
 *
 * The technical name still shows beside each one -- these are a translation for
 * a non-technical reader, never a replacement. Nothing here restates a result;
 * every label describes only what the stage DOES, so it cannot contradict the
 * outcome the server reported.
 */
export const STAGE_LABEL: Record<string, string> = {
  INGEST: "Received the file",
  EXTRACT_TEXT: "Read the page",
  EXTRACT_FIELDS: "Pulled out the fields",
  VALIDATE: "Checked the basics",
  VENDOR_CHECK: "Checked the vendor",
  PO_MATCH: "Found the purchase order",
  DUPLICATE_CHECK: "Looked for duplicates",
  TOLERANCE_CHECK: "Compared against the PO balance",
  DECISION: "Made the call",
};

/**
 * A one-line headline for each verdict, in the words a buyer would use.
 *
 * The formal status is always shown alongside; this softens the delivery
 * without softening the meaning.
 */
export const VERDICT_HEADLINE: Record<string, string> = {
  APPROVED: "Good to pay",
  NEEDS_REVIEW: "Needs a human look",
  REJECTED: "Not approved",
};

export const VERDICT_BLURB: Record<string, string> = {
  APPROVED: "Every check passed, so this one can go straight through.",
  NEEDS_REVIEW: "Nothing is wrong exactly, but something needs a person to decide.",
  REJECTED: "A hard rule failed. This should not be paid as it stands.",
};
