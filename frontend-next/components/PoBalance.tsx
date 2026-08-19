"use client";

/**
 * The segmented bar is the visual heart of the split-PO story: what earlier
 * approved invoices already consumed, what this invoice claims, and whether
 * that claim fits inside what is left on the PO.
 *
 * Every figure here was computed by matching.py. Nothing is recalculated in the
 * browser -- the bar is a rendering of the ledger, not a second opinion on it.
 */
import { money } from "@/lib/format";
import type { PoMatch } from "@/lib/types";

export default function PoBalance({ pm }: { pm: PoMatch }) {
  const total = pm.po_amount || 0;
  const consumed = pm.consumed_before || 0;
  const claim = pm.invoice_total || 0;
  const fits = !!pm.within_tolerance;

  const scale = Math.max(total, consumed + claim) || 1;
  const pct = (v: number) => (v / scale) * 100;

  const shown = fits ? claim : Math.max(0, pm.remaining_before || 0);
  const over = fits ? 0 : claim - Math.max(0, pm.remaining_before || 0);
  const leftover = Math.max(0, total - consumed - shown);

  const seg = (bg: string, width: number, label: string, key: string) =>
    width <= 0.4 ? null : (
      <div
        key={key}
        className="grid h-full place-items-center overflow-hidden text-[11px] font-semibold whitespace-nowrap text-white"
        style={{ width: `${width}%`, background: bg }}
      >
        {width > 9 ? label : ""}
      </div>
    );

  const currentColour = fits ? "var(--ok-solid)" : "var(--warn-solid)";

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-mono font-semibold">{pm.po_number}</span>
        <span className="text-dim">
          {pm.po_vendor} · {money(total)} authorised
        </span>
      </div>

      <div className="flex h-7 w-full overflow-hidden rounded-md border border-border bg-panel2">
        {seg("var(--text-faint)", pct(consumed), money(consumed), "consumed")}
        {seg(currentColour, pct(shown), money(shown), "current")}
        {seg("var(--fail-solid)", pct(over), "+" + money(over), "over")}
        {seg("transparent", pct(leftover), "", "left")}
      </div>

      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5">
        {[
          ["var(--text-faint)", "Consumed earlier", money(consumed)],
          [currentColour, "This invoice", money(claim)],
          ["var(--border-strong)", "Remaining after", money(fits ? pm.remaining_after : pm.remaining_before)],
        ].map(([colour, label, value]) => (
          <div key={label} className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full" style={{ background: colour }} />
            <span className="text-dim">
              {label} <b className="text-text">{value}</b>
            </span>
          </div>
        ))}
      </div>

      <Callout pm={pm} fits={fits} />
    </div>
  );
}

function Callout({ pm, fits }: { pm: PoMatch; fits: boolean }) {
  const base = "mt-3 rounded-lg border px-3 py-2";

  if (!fits) {
    return (
      <div
        className={base}
        style={{ borderColor: "var(--fail-solid)", background: "var(--fail-soft)", color: "var(--fail)" }}
      >
        Over by {money(pm.diff)} — only {money(pm.remaining_before)} left on this PO, tolerance is{" "}
        {money(pm.tolerance)}. The vendor is billing beyond what&apos;s authorised.
      </div>
    );
  }

  if (pm.is_partial) {
    return (
      <div
        className={base}
        style={{ borderColor: "var(--ok-solid)", background: "var(--ok-soft)", color: "var(--ok)" }}
      >
        Partial invoice — accepted. {money(pm.remaining_after)} stays available on {pm.po_number} for
        the next invoice.
      </div>
    );
  }

  return (
    <div className={`${base} border-border bg-panel2 text-dim`}>
      Matches the remaining balance within tolerance (diff {money(pm.diff)}, tolerance{" "}
      {money(pm.tolerance)}). {money(pm.remaining_after)} left on this PO.
    </div>
  );
}
