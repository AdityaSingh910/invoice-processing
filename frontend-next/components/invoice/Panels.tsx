"use client";

/**
 * Read-out panels: what was extracted, and why the engine ruled as it did.
 *
 * A note on confidence. Every extracted field can carry a confidence score,
 * a source location and a quoted piece of evidence (`Extracted.provenance`) --
 * self-reported by the model for the LLM routes, or a deterministic heuristic
 * for regex. It is shown here as exactly what it is: a per-instance signal
 * computed by extraction, not something invented by this component. A field
 * with no entry in `provenance` carries no confidence claim at all -- that is
 * different from carrying a low one, and no badge is drawn for it.
 */
import { amount } from "@/lib/format";
import type { Audit, Extracted, FieldProvenance, Reason } from "@/lib/types";
import { Badge, Callout, KeyValues, StatusBadge, Tooltip } from "@/components/ui";
import { IconAlert, IconCheck, IconShield, IconX } from "@/components/ui/icons";

/* ---------------------------------------------------------------- findings */

const LEVEL = {
  ok: { color: "var(--ok)", Icon: IconCheck },
  fail: { color: "var(--bad)", Icon: IconX },
  warn: { color: "var(--warn)", Icon: IconAlert },
  info: { color: "var(--accent)", Icon: IconShield },
} as const;

/**
 * Findings, failures first.
 *
 * Reordering is safe because these are an unordered set of independent
 * observations, not a sequence — and a reviewer should not have to read four
 * passing checks to reach the one that stopped the invoice.
 */
export function ReasonList({ reasons }: { reasons: Reason[] }) {
  if (!reasons?.length) return <p className="t-meta">No findings recorded.</p>;

  const rank = { fail: 0, warn: 1, info: 2, ok: 3 } as const;
  const items = reasons
    .map((raw) => ({
      level: (typeof raw === "string" ? "info" : raw.level || "info") as keyof typeof LEVEL,
      text: typeof raw === "string" ? raw : raw.text,
    }))
    .sort((a, b) => (rank[a.level] ?? 9) - (rank[b.level] ?? 9));

  return (
    <ul className="flex flex-col gap-2">
      {items.map((r, i) => {
        const { color, Icon } = LEVEL[r.level] ?? LEVEL.info;
        return (
          <li key={i} className="flex gap-2.5 text-[12.5px] leading-snug">
            <span className="mt-px shrink-0" style={{ color }}>
              <Icon size={13} />
            </span>
            <span className={`min-w-0 ${r.level === "fail" ? "text-fg" : "text-muted"}`}>
              {r.text}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/* -------------------------------------------------------------- extraction */

const REQUIRED: (keyof Extracted)[] = ["vendor_name", "invoice_number", "total"];

const ROUTE: Record<
  string,
  { label: string; tone: "ok" | "warn" | "neutral"; note: string }
> = {
  "groq (text)": {
    label: "Groq · text layer",
    tone: "ok",
    note: "The PDF carried a real text layer, so a language model read it directly.",
  },
  "gemini (vision)": {
    label: "Gemini · vision",
    tone: "ok",
    note: "No text layer, so the page was rasterised and read as an image.",
  },
  regex: {
    label: "Pattern matching",
    tone: "warn",
    note: "The model route was unavailable, so built-in patterns read the document. Fields are more likely to be missed.",
  },
  none: {
    label: "Nothing readable",
    tone: "warn",
    note: "Nothing could be read from this document. No values were guessed at.",
  },
};

export function ExtractionSummary({ e }: { e: Extracted }) {
  const missing = REQUIRED.filter((k) => !e[k]);
  const route = ROUTE[e.extraction_method] ?? {
    label: e.extraction_method,
    tone: "neutral" as const,
    note: "",
  };

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="t-meta">Read by</span>
        <Badge tone={route.tone} dot>
          {route.label}
        </Badge>
      </div>
      {route.note && <p className="t-meta text-[11.5px] leading-snug">{route.note}</p>}

      {missing.length > 0 && (
        <Callout
          tone="warn"
          icon={<IconAlert size={13} />}
          title={`${missing.length} required field${missing.length === 1 ? "" : "s"} missing`}
        >
          {missing.map((m) => String(m).replace(/_/g, " ")).join(", ")} could not be read, so this
          invoice cannot be approved automatically.
        </Callout>
      )}
    </div>
  );
}

const Missing = () => (
  <Badge tone="warn" icon={<IconAlert size={10} />}>
    missing
  </Badge>
);

/**
 * A confidence badge for one field, with the source and evidence backing it
 * in a tooltip. Coloured by whether THIS field is the one that tripped the
 * gate (`audit.low_confidence_fields`) -- not by re-deriving a threshold in
 * the browser, which would let the UI's read of "low" drift from the rule
 * engine's. A field the gate does not track (subtotal, tax, currency, date)
 * still shows its score; it just cannot go "bad", only "ok"/"warn", since
 * nothing here decided it was disqualifying.
 */
function ProvenanceBadge({
  field,
  p,
  lowConfidenceFields,
}: {
  field: string;
  p?: FieldProvenance;
  lowConfidenceFields?: Audit["low_confidence_fields"];
}) {
  if (!p || p.confidence == null) return null;
  const flagged = (lowConfidenceFields || []).some((f) => f.field === field);
  const tone: "ok" | "warn" | "bad" = flagged ? "bad" : p.confidence >= 0.85 ? "ok" : "warn";
  const pct = Math.round(p.confidence * 100);

  const tooltipLines = [
    p.source ? `Source: ${p.source}` : null,
    p.evidence
      ? `Evidence: "${p.evidence}"${p.evidence_verified === false ? " (not found in document — unverified)" : ""}`
      : null,
  ].filter(Boolean);

  return (
    <Tooltip label={tooltipLines.join("  ·  ") || "No source recorded"}>
      <Badge tone={tone} className="ml-1.5 shrink-0">
        {pct}%
      </Badge>
    </Tooltip>
  );
}

export function ExtractedFields({ e, audit }: { e: Extracted; audit?: Audit }) {
  const val = (v: string | null) => v || <Missing />;
  const prov = e.provenance || {};
  const low = audit?.low_confidence_fields;

  const withBadge = (field: string, node: React.ReactNode) => (
    <span className="inline-flex items-center justify-end">
      {node}
      <ProvenanceBadge field={field} p={prov[field]} lowConfidenceFields={low} />
    </span>
  );

  return (
    <KeyValues
      rows={[
        ["Vendor", withBadge("vendor_name", val(e.vendor_name))],
        ["Invoice number", withBadge("invoice_number", val(e.invoice_number))],
        ["Invoice date", withBadge("invoice_date", e.invoice_date || "—")],
        ["PO references", (e.po_references || []).join(", ") || "—"],
        [
          "Subtotal",
          withBadge("subtotal", <span key="s" className="tnum">{amount(e.subtotal, e.currency)}</span>),
        ],
        ["Tax", withBadge("tax", <span key="t" className="tnum">{amount(e.tax, e.currency)}</span>)],
        [
          "Total",
          withBadge(
            "total",
            e.total != null ? (
              <span key="tt" className="tnum text-[13px] font-semibold">
                {amount(e.total, e.currency)}
              </span>
            ) : (
              <Missing key="tt" />
            )
          ),
        ],
        ["Line items", (e.line_items || []).length || "—"],
        ["Currency", withBadge("currency", e.currency || "—")],
      ]}
    />
  );
}

/* ------------------------------------------------------------------ verdict */

export const VERDICT: Record<
  string,
  { headline: string; blurb: string; tone: "ok" | "warn" | "bad" }
> = {
  APPROVED: {
    headline: "Approved",
    blurb: "Every rule passed. This invoice can be paid without a human touching it.",
    tone: "ok",
  },
  NEEDS_REVIEW: {
    headline: "Needs review",
    blurb: "A rule could not be satisfied automatically. A person has to decide.",
    tone: "warn",
  },
  REJECTED: {
    headline: "Rejected",
    blurb: "A hard rule failed. This should not be paid as it stands.",
    tone: "bad",
  },
};

/**
 * The verdict, stated once, at the top. Everything below it on the page is
 * evidence for this line.
 */
export function VerdictHeader({
  status,
  filename,
  runId,
  vendor,
  invoiceNumber,
  total,
  currency,
  remaining,
  poCurrency,
  compact = false,
}: {
  status: string;
  filename: string;
  runId?: number;
  vendor?: string | null;
  invoiceNumber?: string | null;
  total?: number | null;
  /** The invoice's own currency. Defaults to USD, matching the extractor's
   *  own fallback when a document carries no currency signal at all. */
  currency?: string | null;
  remaining?: number | null;
  /** The matched PO's currency -- may differ from `currency` when a
   *  conversion applied. Falls back to `currency` so an ordinary same-currency
   *  invoice needs no second prop. */
  poCurrency?: string | null;
  compact?: boolean;
}) {
  const v = VERDICT[status] ?? { headline: status, blurb: "", tone: "neutral" as const };
  const Icon = v.tone === "ok" ? IconCheck : v.tone === "bad" ? IconX : IconAlert;

  return (
    <div
      data-testid="verdict-bar"
      className="panel flex flex-wrap items-center justify-between gap-5 p-4"
      style={{ borderLeft: `2px solid var(--${v.tone}-vivid)` }}
    >
      <div className="flex min-w-0 items-start gap-3">
        <span
          className="mt-px grid h-7 w-7 shrink-0 place-items-center rounded-full"
          style={{ background: `var(--${v.tone}-quiet)`, color: `var(--${v.tone})` }}
        >
          <Icon size={14} />
        </span>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2
              data-testid="verdict-status"
              className="t-metric-sm"
              style={{ color: `var(--${v.tone})` }}
            >
              {v.headline}
            </h2>
            {runId !== undefined && <span className="tnum t-meta text-[11px]">run #{runId}</span>}
          </div>
          {!compact && v.blurb && <p className="t-meta mt-1 max-w-lg">{v.blurb}</p>}
          <p className="t-meta mt-1 truncate text-[11px]">
            <span className="font-medium text-secondary">{filename}</span>
            {vendor ? ` · ${vendor}` : ""}
            {invoiceNumber ? ` · ${invoiceNumber}` : ""}
          </p>
        </div>
      </div>

      <div className="flex gap-5">
        <Figure label="Invoice total" value={amount(total, currency || "USD")} />
        {remaining !== null && remaining !== undefined && (
          <Figure label="PO remaining" value={amount(remaining, poCurrency || currency || "USD")} />
        )}
      </div>
    </div>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="text-right">
      <div className="t-metric-sm tnum">{value}</div>
      <div className="t-caption mt-1">{label}</div>
    </div>
  );
}

/* --------------------------------------------------------------- reviewer */

/**
 * What a reviewer needs before deciding: why the run was held or rejected,
 * which specific field(s) are implicated, the evidence and source behind
 * each one, and one deterministic suggested next step. Every sentence here
 * was computed by rules.py (`reason`, `problematic_fields`,
 * `suggested_resolution`) or extraction.py (`provenance`) -- nothing is
 * composed in this component; it only lays out fields that already exist on
 * `audit`.
 *
 * Renders nothing on an APPROVED run: there is nothing to review.
 */
export function ReviewerBrief({ audit, extracted }: { audit?: Audit; extracted?: Extracted }) {
  if (!audit || audit.automated_decision === "APPROVED") return null;

  const fields = audit.problematic_fields || [];
  // extracted.provenance is the authoritative copy (same run, same moment);
  // audit.provenance is a fallback for callers that only have the audit
  // trail on hand (e.g. a stored run with no live `extracted` object).
  const prov = extracted?.provenance || audit.provenance || {};

  return (
    <div className="flex flex-col gap-3.5">
      {audit.reason && (
        <div>
          <h4 className="t-caption mb-1">Why it was flagged</h4>
          <p className="text-[12.5px] leading-snug">{audit.reason}</p>
        </div>
      )}

      {fields.length > 0 && (
        <div>
          <h4 className="t-caption mb-1.5">
            Field{fields.length === 1 ? "" : "s"} to check
          </h4>
          <ul className="flex flex-col gap-1.5">
            {fields.map((f) => {
              const p = prov[f];
              return (
                <li
                  key={f}
                  className="rounded-[var(--radius-sm)] border border-line bg-sunken px-2.5 py-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[12.5px] font-medium capitalize">
                      {f.replace(/_/g, " ")}
                    </span>
                    {p?.confidence != null && (
                      <Badge tone={p.confidence >= 0.65 ? "warn" : "bad"}>
                        {Math.round(p.confidence * 100)}% confidence
                      </Badge>
                    )}
                  </div>
                  {p?.evidence ? (
                    <p className="t-meta mt-1 text-[11.5px] leading-snug">
                      “{p.evidence}”{p.source ? ` — ${p.source}` : ""}
                      {p.evidence_verified === false && (
                        <span className="text-bad"> (not found in the document — unverified)</span>
                      )}
                    </p>
                  ) : (
                    <p className="t-meta mt-1 text-[11.5px]">No extracted evidence on record for this field.</p>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {audit.suggested_resolution && (
        <Callout tone="accent" icon={<IconAlert size={13} />} title="Suggested next step">
          {audit.suggested_resolution}
        </Callout>
      )}
    </div>
  );
}

export { StatusBadge };
