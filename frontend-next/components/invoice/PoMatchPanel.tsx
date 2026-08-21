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
import type { Allocation, Audit, PoMatch } from "@/lib/types";
import { Badge, Callout, DataTable, KeyValues, TD, TH } from "@/components/ui";
import { IconAlert, IconCheck, IconX } from "@/components/ui/icons";

/** Every PO this invoice charges. One entry for an ordinary invoice. */
function allocationsOf(pm: PoMatch): Allocation[] {
  if (pm.allocations?.length) return pm.allocations;
  if (!pm.po_number) return [];
  return [
    {
      po_number: pm.po_number,
      amount: pm.invoice_total ?? 0,
      po_amount: pm.po_amount,
      po_vendor: pm.po_vendor,
      consumed_before: pm.consumed_before,
      remaining_before: pm.remaining_before,
      remaining_after: pm.remaining_after,
    },
  ];
}

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
      <Badge tone="ok" icon={<IconCheck size={10} />}>
        {label ?? "Match"}
      </Badge>
    );
  if (state === "mismatch")
    return (
      <Badge tone="bad" icon={<IconX size={10} />}>
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
  const bound = pm.po_numbers?.length ? pm.po_numbers : pm.po_number ? [pm.po_number] : [];
  rows.push({
    field: "Purchase order",
    invoice: (inv as { po_references?: string[] }).po_references?.join(", ") || pm.po_number || "—",
    po: bound.join(", ") || "none",
    state: pm.po_number ? (poRule?.passed === false ? "mismatch" : "match") : "mismatch",
    label: !pm.po_number
      ? "No match"
      : pm.is_multi
        ? `${bound.length} POs`
        : pm.matched_via === "inferred"
          ? "Inferred"
          : "Explicit",
    note: poRule?.detail,
  });

  // --- how the invoice was split ------------------------------------------
  // Only shown when there is a split to describe. The rule's own pass/fail
  // drives the badge, as everywhere else in this table.
  if (pm.is_multi) {
    const splitRule = findRule(audit, "split");
    rows.push({
      field: "Split",
      invoice: <span className="text-muted">not stated</span>,
      po: allocationsOf(pm)
        .map((a) => `${a.po_number} ${money(a.amount)}`)
        .join(" · "),
      state: splitRule ? (splitRule.passed ? "match" : "mismatch") : "unknown",
      label: "Calculated",
      note: splitRule?.detail,
    });
  }

  // --- currency -----------------------------------------------------------
  const curRule = (audit?.rules || []).find((r) => r.name === "Currency match");
  const fx = pm.fx;
  rows.push({
    field: "Currency",
    invoice: cur,
    po: po.po_currency || cur,
    state: curRule ? (curRule.passed ? "match" : "mismatch") : "unknown",
    label: fx?.applied
      ? `Converts to ${money(fx.converted_total)}`
      : pm.currency_mismatch
        ? "No rate"
        : undefined,
    note: curRule?.detail,
  });

  // The same-number collision is a distinct finding from an ordinary
  // mismatch -- shown only when it fired, since it drives a REJECT rather
  // than a hold and deserves its own row rather than hiding inside "Currency".
  if (pm.currency_same_number_suspected) {
    const collisionRule = (audit?.rules || []).find(
      (r) => r.name === "Currency/amount not reused across currencies"
    );
    rows.push({
      field: "Currency vs amount",
      invoice: <b>{amount(pm.invoice_total, cur)}</b>,
      po: fx?.applied ? `converts to ${money(fx.converted_total)}` : "unconvertible",
      state: "mismatch",
      label: "Same digits, wrong currency",
      note: collisionRule?.detail,
    });
  }

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
    <DataTable minWidth={560}>
      <thead>
        <tr>
          <TH>Field</TH>
          <TH>On the invoice</TH>
          <TH>On the purchase order</TH>
          <TH align="right">Result</TH>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.field} className={r.state === "mismatch" ? "bg-bad-quiet" : undefined}>
            <TD className="text-[12.5px] font-medium whitespace-nowrap">{r.field}</TD>
            <TD className="text-[12.5px]">{r.invoice}</TD>
            <TD className="text-[12.5px] text-muted">{r.po}</TD>
            <TD className="text-right">
              <StateBadge state={r.state} label={r.label} />
            </TD>
          </tr>
        ))}
      </tbody>
    </DataTable>
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
  // An invoice spanning several POs gets a bar each. One combined bar would
  // have to add balances from different purchase orders together and show the
  // invoice sitting inside the sum, which reads as "it fits" while saying
  // nothing about whether any individual PO can carry its share.
  if (pm.is_multi) return <PoBudgetSplit pm={pm} />;
  return <PoBudgetSingle pm={pm} />;
}

/**
 * One invoice, several purchase orders.
 *
 * The caveat above the bars is the point of the screen: the division shown was
 * computed by the process, not read off the document, which is why the invoice
 * is waiting for a person.
 */
function PoBudgetSplit({ pm }: { pm: PoMatch }) {
  const allocations = allocationsOf(pm);

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <span className="t-meta">
          This invoice <b className="tnum text-fg">{money(pm.invoice_total)}</b> across{" "}
          <span className="font-medium text-fg">{allocations.length} purchase orders</span>
        </span>
        <span className="t-meta">
          {money(pm.remaining_before)} combined remaining
        </span>
      </div>

      <Callout
        tone="warn"
        icon={<IconAlert size={13} />}
        title="The split below was calculated, not read off the invoice"
      >
        The document does not say how much belongs to each purchase order, so each was
        filled to its remaining balance in the order the invoice referenced them. Confirm
        these amounts before approving — nothing is charged to any PO until you do.
      </Callout>

      <div className="mt-4 space-y-4">
        {allocations.map((a) => (
          <AllocationBar key={a.po_number} a={a} />
        ))}
      </div>

      {/* The explicit ledger a reviewer scans first: what was allocated,
          against the invoice total, and whether it balances to zero. Every
          figure here is the same one the bars above already show — this is
          a summary line, not a second calculation. */}
      <div className="mt-4 border-t border-line-strong pt-3">
        <div className="ledger-row">
          <span className="text-muted">Total allocated</span>
          <span className="tnum font-medium">
            {money(allocations.reduce((sum, a) => sum + (a.amount || 0), 0))}
          </span>
        </div>
        <div className="ledger-row">
          <span className="text-muted">Invoice total</span>
          <span className="tnum font-medium">{money(pm.invoice_total)}</span>
        </div>
        <div className="ledger-row total">
          <span>Variance</span>
          <span
            className="tnum"
            style={{
              color:
                Math.abs(
                  allocations.reduce((sum, a) => sum + (a.amount || 0), 0) - (pm.invoice_total || 0)
                ) > 0.01
                  ? "var(--bad)"
                  : "var(--ok)",
            }}
          >
            {money(
              allocations.reduce((sum, a) => sum + (a.amount || 0), 0) - (pm.invoice_total || 0)
            )}
          </span>
        </div>
      </div>
    </div>
  );
}

/** One PO's share: what was already spent, what this invoice claims of it. */
function AllocationBar({ a }: { a: Allocation }) {
  const total = a.po_amount || 0;
  const consumed = a.consumed_before || 0;
  const claim = a.amount || 0;
  const remaining = a.remaining_before || 0;

  const scale = Math.max(total, consumed + claim) || 1;
  const shown = a.over ? Math.max(0, remaining) : claim;
  const over = a.over ? claim - Math.max(0, remaining) : 0;
  const free = Math.max(0, total - consumed - shown);

  const seg = (w: number, bg: string, key: string) =>
    w <= 0 ? null : <div key={key} style={{ width: `${(w / scale) * 100}%`, background: bg }} />;

  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-2">
        <span className="t-meta">
          <span className="font-medium text-fg">{a.po_number}</span>
          {a.po_status === "closed" && (
            <>
              {" "}
              <Badge tone="bad">Closed</Badge>
            </>
          )}
          {" · "}
          {money(total)} authorised
        </span>
        <span className="tnum t-meta">
          <b className="text-fg">{money(claim)}</b> of {money(remaining)} remaining
        </span>
      </div>

      <div
        className="flex h-2 w-full overflow-hidden rounded-full bg-sunken"
        role="img"
        aria-label={`${a.po_number}: ${money(consumed)} already consumed, this invoice claims ${money(claim)} of ${money(total)} authorised`}
      >
        {seg(consumed, "var(--fg-faint)", "consumed")}
        {seg(shown, a.over ? "var(--warn-vivid)" : "var(--ok-vivid)", "claim")}
        {seg(over, "var(--bad-vivid)", "over")}
        {seg(free, "transparent", "free")}
      </div>

      {a.over && (
        <p className="t-meta mt-1.5 text-bad">
          {money(claim - Math.max(0, remaining))} beyond what is left on this PO.
        </p>
      )}
    </div>
  );
}

function PoBudgetSingle({ pm }: { pm: PoMatch }) {
  const total = pm.po_amount || 0;
  const consumed = pm.consumed_before || 0;
  // The PO-currency equivalent of the invoice, when a conversion applied --
  // `po_amount`/`remaining_before` are always in the PO's currency, so the bar
  // must compare against the same figure or it under- or over-fills relative
  // to what was actually approved.
  const claim = pm.fx?.applied ? (pm.fx.converted_total ?? 0) : pm.invoice_total || 0;
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
        <span className="t-meta">
          Authorised <b className="tnum text-fg">{money(total)}</b> on{" "}
          <span className="font-medium text-fg">{pm.po_number}</span>
        </span>
        <span className="t-meta">{pm.po_vendor}</span>
      </div>

      {pm.fx?.applied && (
        <p className="t-meta mb-2 text-[11.5px]">
          Invoice is {amount(pm.invoice_total, pm.invoice_currency || "")} — converted to{" "}
          <span className="tnum font-medium text-fg">{money(claim)}</span> at the pinned rate{" "}
          {pm.fx.rate?.toFixed(4)} (FX table v{pm.fx.rate_version}).
        </p>
      )}

      <div
        className="flex h-2 w-full overflow-hidden rounded-full bg-sunken"
        role="img"
        aria-label={`${money(consumed)} consumed, this invoice ${money(claim)}, of ${money(total)}`}
      >
        {seg(consumed, "var(--fg-faint)", "consumed")}
        {seg(shown, fits ? "var(--ok-vivid)" : "var(--warn-vivid)", "claim")}
        {seg(over, "var(--bad-vivid)", "over")}
        {seg(free, "transparent", "free")}
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
        {[
          ["Consumed earlier", money(consumed), "var(--fg-faint)"],
          ["This invoice", money(claim), fits ? "var(--ok-vivid)" : "var(--warn-vivid)"],
          ["Remaining before", money(pm.remaining_before), "var(--line-strong)"],
          [
            "Remaining after",
            money(fits ? pm.remaining_after : pm.remaining_before),
            "var(--line-strong)",
          ],
        ].map(([label, value, colour]) => (
          <div key={label as string}>
            <dt className="t-meta flex items-center gap-1.5 text-[11px]">
              <span
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ background: colour as string }}
              />
              {label}
            </dt>
            <dd className="tnum mt-0.5 text-[13px] font-semibold">{value}</dd>
          </div>
        ))}
      </dl>

      <div className="mt-4">
        {!fits ? (
          <Callout
            tone="bad"
            icon={<IconAlert size={13} />}
            title={`Over the remaining balance by ${money(pm.diff)}`}
          >
            Only {money(pm.remaining_before)} is left on {pm.po_number} and the tolerance is{" "}
            {money(pm.tolerance)}. The vendor is billing beyond what was authorised.
          </Callout>
        ) : pm.is_partial ? (
          <Callout tone="ok" icon={<IconCheck size={13} />} title="Partial invoice — accepted">
            {money(pm.remaining_after)} stays available on {pm.po_number} for the next invoice.
          </Callout>
        ) : (
          <Callout tone="neutral" icon={<IconCheck size={13} />}>
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

  const allocations = audit?.allocations || [];
  const rows: [string, React.ReactNode][] = [
    [
      po.is_multi ? "Purchase orders" : "Purchase order",
      (po.po_numbers?.length ? po.po_numbers.join(", ") : po.po_number) || "none",
    ],
    ["Matched via", po.matched_via || "—"],
    ["PO status", po.po_status || "—"],
  ];

  // Only worth a row when there is a division to explain. For a single-PO
  // invoice the allocation is just the total, which the comparison already says.
  if (po.is_multi) {
    rows.push([
      "Charged",
      <span key="alloc" className="tnum">
        {allocations.map((a) => `${a.po_number} ${money(a.amount)}`).join(" · ")}
      </span>,
    ]);
    rows.push([
      "Split basis",
      <span key="basis" className="text-muted">
        {audit?.allocation_basis === "calculated"
          ? "calculated by the process — not stated on the invoice"
          : audit?.allocation_basis || "—"}
      </span>,
    ]);
  }

  rows.push(["Source record", <span key="s" className="text-muted">{source}</span>]);

  return <KeyValues rows={rows} />;
}
