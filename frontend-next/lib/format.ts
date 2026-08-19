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
  const n = Number(v);
  // The minus sign belongs in front of the currency symbol: "$-2,000.00" reads
  // as a corrupted figure, "-$2,000.00" reads as a credit.
  return `${n < 0 ? "-" : ""}$${nf(Math.abs(n))}`;
}

/** Currency-aware formatter for the audit trail: printing "$" beside a rupee
 *  figure would make the trail say something untrue. */
export function amount(v: number | null | undefined, currency?: string | null): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const raw = Number(v);
  const sign = raw < 0 ? "-" : "";
  const sym = CURRENCY_SYMBOL[String(currency || "").toUpperCase()];
  const n = nf(Math.abs(raw));
  return sym ? `${sign}${sym}${n}` : `${sign}${n} ${currency || ""}`.trim();
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

/**
 * The nine stages, grouped into the six phases an AP person thinks in.
 *
 * This is presentation only: the pipeline still runs and reports all nine
 * stages, and each phase's state is derived from the stages inside it rather
 * than tracked separately. Nothing here can report a phase complete that the
 * backend did not actually finish.
 */
export const PHASES: { key: string; label: string; stages: string[] }[] = [
  { key: "upload", label: "Document", stages: ["INGEST"] },
  { key: "extract", label: "Extraction", stages: ["EXTRACT_TEXT", "EXTRACT_FIELDS"] },
  { key: "validate", label: "Validation", stages: ["VALIDATE"] },
  { key: "match", label: "PO match", stages: ["VENDOR_CHECK", "PO_MATCH"] },
  { key: "rules", label: "Business rules", stages: ["DUPLICATE_CHECK", "TOLERANCE_CHECK"] },
  { key: "decision", label: "Decision", stages: ["DECISION"] },
];

/**
 * Compact timestamp for dense feeds: the time alone for today, otherwise the
 * day and month. A full "19/08/2026, 20:06:27" in a narrow column truncates to
 * something unreadable, and the year is never the interesting part in an
 * activity feed.
 */
export function whenCompact(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();

  return sameDay
    ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
