"use client";

/**
 * Read-out panels: what was extracted, and why the engine ruled as it did.
 *
 * A note on confidence. Premium AP products show a per-field confidence score
 * beside each extracted value. This pipeline does not produce one — extracted
 * fields arrive as bare floats — so none is displayed. Rendering an invented
 * percentage next to a dollar figure, on a screen whose entire purpose is
 * auditability, would be fabricating evidence. What is shown instead is real:
 * which route read the document, what that implies for reliability, and exactly
 * which required fields came back empty.
 */
import { amount } from "@/lib/format";
import type { Extracted, Reason } from "@/lib/types";
import { Badge, Callout, KeyValues, StatusBadge } from "@/components/ui";
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

export function ExtractedFields({ e }: { e: Extracted }) {
  const val = (v: string | null) => v || <Missing />;
  return (
    <KeyValues
      rows={[
        ["Vendor", val(e.vendor_name)],
        ["Invoice number", val(e.invoice_number)],
        ["Invoice date", e.invoice_date || "—"],
        ["PO references", (e.po_references || []).join(", ") || "—"],
        ["Subtotal", <span key="s" className="tnum">{amount(e.subtotal, e.currency)}</span>],
        ["Tax", <span key="t" className="tnum">{amount(e.tax, e.currency)}</span>],
        [
          "Total",
          e.total != null ? (
            <span key="tt" className="tnum text-[13px] font-semibold">
              {amount(e.total, e.currency)}
            </span>
          ) : (
            <Missing key="tt" />
          ),
        ],
        ["Line items", (e.line_items || []).length || "—"],
        ["Currency", e.currency || "—"],
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

export { StatusBadge };
