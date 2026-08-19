"use client";

/**
 * Three-way match, made legible.
 *
 * IMPORTANT: this panel never decides anything. Each row's pass/fail comes from
 * the rule the Python engine emitted for that field — looked up by name in
 * `audit.rules` — not from comparing the two values in JavaScript. A browser
 * that re-derived "vendor matches" could disagree with the engine that actually
 * made the call (vendor matching is normalised server-side, so a textual
 * difference is not necessarily a mismatch), and a match screen that contradicts
 * the verdict beside it is worse than no match screen.
 *
 * Where no rule was emitted for a field, the row says so rather than guessing.
 */
import { amount, money } from "@/lib/format";
import type { Audit, PoMatch } from "@/lib/types";
import { Badge, Callout, DescriptionList } from "@/components/ui";
import { IconAlert, IconCheck, IconX } from "@/components/ui/icons";

/** Locate the rule covering a field. Names come from rules.py. */
function findRule(audit: Audit | undefined, fragment: string) {
  return (audit?.rules || []).find((r) =>
    r.name.toLowerCase().includes(fragment.toLowerCase())
  );
}

type RowState = "match" | "mismatch" | "unknown";

function StateBadge({ state, label }: { state: RowState; label?: string }) {
  if (state === "match")
    return (
      <Badge tone="success" icon={<IconCheck size={11} />}>
        {label ?? "Match"}
      </Badge>
    );
  if (state === "mismatch")
    return (
      <Badge tone="danger" icon={<IconX size={11} />}>
        {label ?? "Discrepancy"}
      </Badge>
    );
  return <Badge tone="neutral">{label ?? "Not evaluated"}</Badge>;
}

export function MatchTable({ pm, audit }: { pm: PoMatch; audit?: Audit }) {
  const inv = audit?.invoice || {};
  const po = audit?.purchase_order || {};
  const cur = inv.currency || "USD";

  const rows: {
    field: string;
    invoice: React.ReactNode;
    po: React.ReactNode;
    state: RowState;
    label?: string;
    note?: string;
  }[] = [];

  // --- vendor -------------------------------------------------------------
  const vendorRule = findRule(audit, "vendor");
  rows.push({
    field: "Vendor",
    invoice: inv.vendor || "—",
    po: pm.po_vendor || "—",
    state: vendorRule ? (vendorRule.passed ? "match" : "mismatch") : "unknown",
    label: vendorRule?.passed ? "Approved" : vendorRule ? "Not approved" : undefined,
    note: vendorRule?.detail,
  });

  // --- purchase order -----------------------------------------------------
  const poRule = findRule(audit, "po matched");
  rows.push({
    field: "Purchase order",
    invoice: (inv as { po_references?: string[] }).po_references?.join(", ") || pm.po_number || "—",
    po: pm.po_number || "none",
    state: pm.po_number ? (poRule?.passed === false ? "mismatch" : "match") : "mismatch",
    label: pm.po_number
      ? pm.matched_via === "inferred"
        ? "Inferred"
        : "Explicit"
      : "No match",
    note: poRule?.detail,
  });

  // --- currency -----------------------------------------------------------
  const curRule = findRule(audit, "currency");
  rows.push({
    field: "Currency",
    invoice: cur,
    po: po.po_currency || cur,
    state: curRule ? (curRule.passed ? "match" : "mismatch") : "unknown",
    note: curRule?.detail,
  });

  // --- amount -------------------------------------------------------------
  const amountRule = findRule(audit, "po remaining");
  const over = (pm.diff ?? 0) > 0 && !pm.within_tolerance;
  rows.push({
    field: "Amount",
    invoice: <b>{amount(pm.invoice_total, cur)}</b>,
    po: `${amount(pm.remaining_before, po.po_currency || cur)} remaining`,
    state: amountRule ? (amountRule.passed ? "match" : "mismatch") : "unknown",
    label: amountRule?.passed
      ? pm.is_partial
        ? "Partial"
        : "Within tolerance"
      : over
        ? "Over budget"
        : undefined,
    note: amountRule?.detail,
  });

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[540px] border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-border">
            <th scope="col" className="label px-2 py-1.5 text-left">
              Field
            </th>
            <th scope="col" className="label px-2 py-1.5 text-left">
              On the invoice
            </th>
            <th scope="col" className="label px-2 py-1.5 text-left">
              On the purchase order
            </th>
            <th scope="col" className="label px-2 py-1.5 text-right">
              Result
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((r) => (
            <tr key={r.field} className={r.state === "mismatch" ? "bg-[var(--danger-weak)]" : ""}>
              <td className="px-2 py-2.5 font-medium whitespace-nowrap">{r.field}</td>
              <td className="px-2 py-2.5">{r.invoice}</td>
              <td className="px-2 py-2.5 text-muted">{r.po}</td>
              <td className="px-2 py-2.5 text-right">
                <StateBadge state={r.state} label={r.label} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The PO budget bar: what earlier approved invoices consumed, what this invoice
 * claims, and whether that claim fits in what is left.
 *
 * Every figure was computed by matching.py. Nothing is recalculated here — the
 * bar renders the ledger, it does not audit it.
 */
export function PoBudget({ pm }: { pm: PoMatch }) {
  const total = pm.po_amount || 0;
  const consumed = pm.consumed_before || 0;
  const claim = pm.invoice_total || 0;
  const fits = !!pm.within_tolerance;

  const scale = Math.max(total, consumed + claim) || 1;
  const shown = fits ? claim : Math.max(0, pm.remaining_before || 0);
  const over = fits ? 0 : claim - Math.max(0, pm.remaining_before || 0);
  const free = Math.max(0, total - consumed - shown);

  const seg = (w: number, bg: string, key: string) =>
    w <= 0 ? null : <div key={key} style={{ width: `${(w / scale) * 100}%`, background: bg }} />;

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-[13px] text-muted">
          Authorised <b className="num text-fg">{money(total)}</b> on{" "}
          <span className="font-medium text-fg">{pm.po_number}</span>
        </span>
        <span className="text-[13px] text-muted">{pm.po_vendor}</span>
      </div>

      <div
        className="flex h-2.5 w-full overflow-hidden rounded-full bg-surface2"
        role="img"
        aria-label={`${money(consumed)} consumed, this invoice ${money(claim)}, of ${money(total)}`}
      >
        {seg(consumed, "var(--fg-subtle)", "consumed")}
        {seg(shown, fits ? "var(--success-solid)" : "var(--warning-solid)", "claim")}
        {seg(over, "var(--danger-solid)", "over")}
        {seg(free, "transparent", "free")}
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
        {[
          ["Consumed earlier", money(consumed), "var(--fg-subtle)"],
          ["This invoice", money(claim), fits ? "var(--success-solid)" : "var(--warning-solid)"],
          ["Remaining before", money(pm.remaining_before), "var(--border-strong)"],
          [
            "Remaining after",
            money(fits ? pm.remaining_after : pm.remaining_before),
            "var(--border-strong)",
          ],
        ].map(([label, value, colour]) => (
          <div key={label as string}>
            <dt className="flex items-center gap-1.5 text-[11px] text-subtle">
              <span
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ background: colour as string }}
              />
              {label}
            </dt>
            <dd className="num mt-0.5 text-[14px] font-semibold">{value}</dd>
          </div>
        ))}
      </dl>

      <div className="mt-4">
        {!fits ? (
          <Callout
            tone="danger"
            icon={<IconAlert size={14} />}
            title={`Over the remaining balance by ${money(pm.diff)}`}
          >
            Only {money(pm.remaining_before)} is left on {pm.po_number} and the tolerance is{" "}
            {money(pm.tolerance)}. The vendor is billing beyond what was authorised.
          </Callout>
        ) : pm.is_partial ? (
          <Callout tone="success" icon={<IconCheck size={14} />} title="Partial invoice — accepted">
            {money(pm.remaining_after)} stays available on {pm.po_number} for the next invoice.
          </Callout>
        ) : (
          <Callout tone="neutral" icon={<IconCheck size={14} />}>
            Within tolerance — variance {money(pm.diff)} against a {money(pm.tolerance)} allowance.
          </Callout>
        )}
      </div>
    </div>
  );
}

/** Compact PO provenance, for the audit panel. */
export function PoProvenance({ audit }: { audit?: Audit }) {
  const po = audit?.purchase_order || {};
  const source = po.po_number
    ? po.source_file
      ? `${po.source_file}${po.source_row != null ? `, row ${po.source_row}` : ", row unknown"}`
      : "not recorded"
    : "—";

  return (
    <DescriptionList
      rows={[
        ["Purchase order", po.po_number || "none"],
        ["Matched via", po.matched_via || "—"],
        ["PO status", po.po_status || "—"],
        ["Source record", <span key="s" className="text-muted">{source}</span>],
      ]}
    />
  );
}
