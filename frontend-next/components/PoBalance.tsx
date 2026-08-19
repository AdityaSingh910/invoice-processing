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

  const currentColour = fits ? "var(--ok-solid)" : "var(--warn-solid)";

  const seg = (bg: string, width: number, label: string, key: string) =>
    width <= 0.4 ? null : (
      <div
        key={key}
        className="grid h-full place-items-center overflow-hidden text-[11px] font-bold whitespace-nowrap text-white tabular-nums"
        style={{ width: `${width}%`, background: bg }}
      >
        {width > 11 ? label : ""}
      </div>
    );

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <span className="rounded-md bg-panel2 px-2 py-0.5 font-mono text-[13px] font-bold">
          {pm.po_number}
        </span>
        <span className="text-[13px] text-dim">
          {pm.po_vendor} · <b className="text-text tabular-nums">{money(total)}</b> authorised
        </span>
      </div>

      <div className="flex h-9 w-full overflow-hidden rounded-[var(--radius-inner)] border border-border bg-panel3">
        {seg("var(--text-faint)", pct(consumed), money(consumed), "consumed")}
        {seg(currentColour, pct(shown), money(shown), "current")}
        {seg("var(--fail-solid)", pct(over), "+" + money(over), "over")}
        {seg("transparent", pct(leftover), "", "left")}
      </div>

      <div className="mt-3.5 flex flex-wrap gap-x-6 gap-y-2">
        {(
          [
            ["var(--text-faint)", "Consumed earlier", money(consumed)],
            [currentColour, "This invoice", money(claim)],
            [
              "var(--border-strong)",
              "Remaining after",
              money(fits ? pm.remaining_after : pm.remaining_before),
            ],
          ] as [string, string, string][]
        ).map(([colour, label, value]) => (
          <div key={label} className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full" style={{ background: colour }} />
            <span className="text-[13px] text-dim">
              {label} <b className="text-text tabular-nums">{value}</b>
            </span>
          </div>
        ))}
      </div>

      <Callout pm={pm} fits={fits} />
    </div>
  );
}

function Callout({ pm, fits }: { pm: PoMatch; fits: boolean }) {
  const base =
    "mt-4 flex items-start gap-2.5 rounded-[var(--radius-inner)] border px-3.5 py-3 text-[14px]";

  if (!fits) {
    return (
      <div
        className={base}
        style={{
          borderColor: "var(--fail-border)",
          background: "var(--fail-soft)",
          color: "var(--fail)",
        }}
      >
        <span className="mt-0.5 font-bold" aria-hidden>
          ✕
        </span>
        <span>
          <b>Over by {money(pm.diff)}.</b> Only {money(pm.remaining_before)} is left on this PO and
          tolerance is {money(pm.tolerance)}. The vendor is billing beyond what&apos;s authorised.
        </span>
      </div>
    );
  }

  if (pm.is_partial) {
    return (
      <div
        className={base}
        style={{
          borderColor: "var(--ok-border)",
          background: "var(--ok-soft)",
          color: "var(--ok)",
        }}
      >
        <span className="mt-0.5 font-bold" aria-hidden>
          ✓
        </span>
        <span>
          <b>Partial invoice — accepted.</b> {money(pm.remaining_after)} stays available on{" "}
          {pm.po_number} for the next invoice.
        </span>
      </div>
    );
  }

  return (
    <div className={`${base} border-border bg-panel2 text-dim`}>
      <span className="mt-0.5 font-bold text-accent" aria-hidden>
        ✓
      </span>
      <span>
        Matches the remaining balance within tolerance (diff {money(pm.diff)}, tolerance{" "}
        {money(pm.tolerance)}). {money(pm.remaining_after)} left on this PO.
      </span>
    </div>
  );
}
