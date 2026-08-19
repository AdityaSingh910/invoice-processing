"use client";

/**
 * Read-out panels: what the model extracted, and why the engine ruled as it did.
 *
 * A note on confidence. Premium AP products show a per-field confidence score
 * next to each extracted value. This pipeline does not produce one — extracted
 * fields arrive as bare values — so none is displayed. Rendering an invented
 * percentage beside a dollar figure would be fabricating evidence in a screen
 * whose entire purpose is to be auditable. What IS shown instead is real: which
 * route read the document, and exactly which required fields came back empty.
 */
import { money } from "@/lib/format";
import type { Extracted, Reason, RunResult } from "@/lib/types";
import { Badge, Callout, DescriptionList } from "@/components/ui";
import { IconAlert, IconCheck, IconShield, IconX } from "@/components/ui/icons";

/* ---------------------------------------------------------------- reasons */

const LEVEL = {
  ok: { tone: "success", Icon: IconCheck },
  fail: { tone: "danger", Icon: IconX },
  warn: { tone: "warning", Icon: IconAlert },
  info: { tone: "accent", Icon: IconShield },
} as const;

export function ReasonList({ reasons }: { reasons: Reason[] }) {
  if (!reasons?.length) return <p className="text-[13px] text-muted">No findings recorded.</p>;

  return (
    <ul className="flex flex-col gap-2.5">
      {reasons.map((raw, i) => {
        const level = (typeof raw === "string" ? "info" : raw.level || "info") as keyof typeof LEVEL;
        const text = typeof raw === "string" ? raw : raw.text;
        const { tone, Icon } = LEVEL[level] ?? LEVEL.info;
        const colour = {
          success: "var(--success)",
          danger: "var(--danger)",
          warning: "var(--warning)",
          accent: "var(--accent)",
        }[tone];

        return (
          <li key={i} className="flex gap-2.5 text-[13px] leading-snug">
            <span className="mt-0.5 shrink-0" style={{ color: colour }}>
              <Icon size={14} />
            </span>
            <span className="min-w-0 text-muted">{text}</span>
          </li>
        );
      })}
    </ul>
  );
}

/* -------------------------------------------------------------- extraction */

const REQUIRED: (keyof Extracted)[] = ["vendor_name", "invoice_number", "total"];

const ROUTE_COPY: Record<string, { label: string; tone: "success" | "warning" | "neutral"; note: string }> = {
  "groq (text)": {
    label: "Groq · text layer",
    tone: "success",
    note: "The PDF carried a real text layer, so a language model read it directly.",
  },
  "gemini (vision)": {
    label: "Gemini · vision",
    tone: "success",
    note: "No text layer, so the page was rasterised and read as an image.",
  },
  regex: {
    label: "Pattern matching",
    tone: "warning",
    note: "The model route was unavailable, so built-in patterns read the document. Fields are more likely to be missed.",
  },
  none: {
    label: "Nothing readable",
    tone: "warning",
    note: "Nothing could be read from this document. No values were guessed at.",
  },
};

export function ExtractionSummary({ e }: { e: Extracted }) {
  const missing = REQUIRED.filter((k) => !e[k]);
  const route = ROUTE_COPY[e.extraction_method] ?? {
    label: e.extraction_method,
    tone: "neutral" as const,
    note: "",
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] text-muted">Read by</span>
        <Badge tone={route.tone}>{route.label}</Badge>
      </div>
      {route.note && <p className="text-[12px] leading-snug text-subtle">{route.note}</p>}

      {missing.length > 0 && (
        <Callout
          tone="warning"
          icon={<IconAlert size={14} />}
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
  <Badge tone="warning" icon={<IconAlert size={11} />}>
    missing
  </Badge>
);

export function ExtractedFields({ e }: { e: Extracted }) {
  const val = (v: string | null) => v || <Missing />;

  return (
    <DescriptionList
      rows={[
        ["Vendor", val(e.vendor_name)],
        ["Invoice number", val(e.invoice_number)],
        ["Invoice date", e.invoice_date || "—"],
        ["PO references", (e.po_references || []).join(", ") || "—"],
        ["Subtotal", <span key="s" className="num">{money(e.subtotal)}</span>],
        ["Tax", <span key="t" className="num">{money(e.tax)}</span>],
        [
          "Total",
          e.total != null ? (
            <span key="tt" className="num text-[14px] font-semibold">
              {money(e.total)}
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

/* ------------------------------------------------------------------ header */

export const VERDICT_COPY: Record<
  string,
  { headline: string; blurb: string; tone: "success" | "warning" | "danger" }
> = {
  APPROVED: {
    headline: "Approved",
    blurb: "Every rule passed. This invoice can be paid without a human touching it.",
    tone: "success",
  },
  NEEDS_REVIEW: {
    headline: "Needs review",
    blurb: "A rule could not be satisfied automatically. A person has to decide.",
    tone: "warning",
  },
  REJECTED: {
    headline: "Rejected",
    blurb: "A hard rule failed. This should not be paid as it stands.",
    tone: "danger",
  },
};

/** The verdict, stated once. Everything else on the screen is evidence for it. */
export function VerdictHeader({
  status,
  filename,
  runId,
  vendor,
  invoiceNumber,
  total,
  remaining,
  compact = false,
}: {
  status: string;
  filename: string;
  runId?: number;
  vendor?: string | null;
  invoiceNumber?: string | null;
  total?: number | null;
  remaining?: number | null;
  compact?: boolean;
}) {
  const v = VERDICT_COPY[status] ?? {
    headline: status,
    blurb: "",
    tone: "neutral" as const,
  };
  const Icon = v.tone === "success" ? IconCheck : v.tone === "danger" ? IconX : IconAlert;

  return (
    <div
      data-testid="verdict-bar"
      className="flex flex-wrap items-start justify-between gap-5 rounded-[var(--radius-lg)] border border-border bg-surface p-4 shadow-[var(--shadow-xs)] sm:p-5"
      style={{ borderLeft: `3px solid var(--${v.tone})` }}
    >
      <div className="flex min-w-0 gap-3">
        <span
          className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full border"
          style={{
            background: `var(--${v.tone}-weak)`,
            borderColor: `var(--${v.tone}-line)`,
            color: `var(--${v.tone})`,
          }}
        >
          <Icon size={16} />
        </span>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2
              data-testid="verdict-status"
              className="text-[17px] leading-tight font-semibold tracking-[-0.01em]"
              style={{ color: `var(--${v.tone})` }}
            >
              {v.headline}
            </h2>
            {runId !== undefined && (
              <span className="num text-[12px] text-subtle">run #{runId}</span>
            )}
          </div>
          {!compact && v.blurb && <p className="mt-1 text-[13px] text-muted">{v.blurb}</p>}
          <p className="mt-1.5 truncate text-[12px] text-subtle">
            <span className="font-medium text-muted">{filename}</span>
            {vendor ? ` · ${vendor}` : ""}
            {invoiceNumber ? ` · ${invoiceNumber}` : ""}
          </p>
        </div>
      </div>

      <div className="flex gap-6">
        <Figure label="Invoice total" value={money(total)} />
        {remaining !== null && remaining !== undefined && (
          <Figure label="PO remaining" value={money(remaining)} />
        )}
      </div>
    </div>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="text-right">
      <div className="num text-[20px] leading-none font-semibold tracking-[-0.02em]">{value}</div>
      <div className="label mt-1.5">{label}</div>
    </div>
  );
}

export function verdictOf(r: RunResult) {
  return VERDICT_COPY[r.status];
}
