"use client";

/**
 * Every run, and what they did to the PO ledger.
 *
 * PO consumption is derived the same way the backend derives it: only APPROVED
 * runs consume budget. This view sums the runs it was given rather than reading
 * a stored counter, because there is no stored counter -- that is the point of
 * the design.
 */
import { useEffect, useMemo, useState } from "react";
import { apiJson } from "@/lib/api";
import { money, when } from "@/lib/format";
import type { Reference, RunRecord, Verdict } from "@/lib/types";
import { Card, EmptyState, StatusPill } from "./ui";
import RunModal from "./RunModal";

const FILTERS: { key: "ALL" | Verdict; label: string }[] = [
  { key: "ALL", label: "All" },
  { key: "APPROVED", label: "Approved" },
  { key: "NEEDS_REVIEW", label: "Needs review" },
  { key: "REJECTED", label: "Rejected" },
];

export default function Dashboard({ reloadKey }: { reloadKey: number }) {
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [reference, setReference] = useState<Reference | null>(null);
  const [filter, setFilter] = useState<"ALL" | Verdict>("ALL");
  const [open, setOpen] = useState<RunRecord | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [r, ref] = await Promise.all([
          apiJson<RunRecord[]>("/api/runs"),
          apiJson<Reference>("/api/reference"),
        ]);
        if (cancelled) return;
        setRuns(r);
        setReference(ref);
      } catch {
        /* the 401 path signs out on its own; anything else leaves the last view */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey, nonce]);

  const shown = useMemo(
    () => (filter === "ALL" ? runs : runs.filter((r) => r.status === filter)),
    [runs, filter]
  );

  const counts = useMemo(() => {
    const c: Record<string, number> = { APPROVED: 0, NEEDS_REVIEW: 0, REJECTED: 0 };
    runs.forEach((r) => (c[r.status] = (c[r.status] || 0) + 1));
    return c;
  }, [runs]);

  const consumed = useMemo(() => {
    const used: Record<string, number> = {};
    runs
      .filter((r) => r.status === "APPROVED" && r.po_number)
      .forEach((r) => (used[r.po_number!] = (used[r.po_number!] || 0) + (r.total || 0)));
    return used;
  }, [runs]);

  return (
    <div className="grid gap-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Total runs" value={runs.length} />
        <Stat label="Approved" value={counts.APPROVED} tone="ok" />
        <Stat label="Needs review" value={counts.NEEDS_REVIEW} tone="warn" />
        <Stat label="Rejected" value={counts.REJECTED} tone="fail" />
      </div>

      <Card
        title="Run history"
        aside={
          <div className="flex flex-wrap items-center gap-1.5">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                onClick={() => setFilter(f.key)}
                className={`rounded-full border px-2.5 py-1 text-[12px] transition ${
                  filter === f.key
                    ? "border-accent bg-accent-soft text-accent"
                    : "border-border bg-panel2 text-dim hover:border-border-strong"
                }`}
              >
                {f.label}
              </button>
            ))}
            <button
              onClick={() => setNonce((n) => n + 1)}
              className="rounded-full border border-border bg-panel2 px-2.5 py-1 text-[12px] text-dim transition hover:border-border-strong"
            >
              Refresh
            </button>
          </div>
        }
      >
        {shown.length === 0 ? (
          <EmptyState
            title="No runs to show"
            sub="Process an invoice on the Run tab and it will appear here."
          />
        ) : (
          <div className="-mx-4 overflow-x-auto px-4">
            <table className="w-full min-w-[760px] border-collapse text-left">
              <thead>
                <tr className="border-b border-border text-[11px] tracking-wider text-faint uppercase">
                  {["ID", "File", "Vendor", "Invoice #", "Total", "PO", "Status", "When"].map(
                    (h, i) => (
                      <th
                        key={h}
                        className={`py-2 pr-3 font-semibold ${i === 4 ? "text-right" : ""}`}
                      >
                        {h}
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr
                    key={r.id}
                    onClick={() => setOpen(r)}
                    className="cursor-pointer border-b border-border last:border-0 hover:bg-panel2"
                  >
                    <td className="py-2 font-mono text-[12px] text-dim">#{r.id}</td>
                    <td className="py-2 pr-3">{r.filename}</td>
                    <td className="py-2 pr-3">{r.vendor_name || "—"}</td>
                    <td className="py-2 pr-3 font-mono text-[12px]">{r.invoice_number || "—"}</td>
                    <td className="py-2 pr-3 text-right whitespace-nowrap">{money(r.total)}</td>
                    <td className="py-2 pr-3 font-mono text-[12px]">{r.po_number || "—"}</td>
                    <td className="py-2 pr-3">
                      <div className="flex items-center gap-1.5">
                        <StatusPill status={r.status} />
                        {/* A run a person ruled on reads as APPROVED/REJECTED like
                            any other. This chip is the only thing on the row that
                            says a human decided it. */}
                        {r.human_decision && (
                          <span
                            title={`${String(r.final_decision || "").replace(/_/g, " ")} by ${
                              r.reviewed_by || "an unattributed reviewer"
                            }`}
                            className="rounded-full border border-border px-1.5 py-0.5 text-[10px] font-semibold text-dim uppercase"
                          >
                            human
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="py-2 whitespace-nowrap text-dim">{when(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="PO consumption">
        <div className="grid gap-3">
          {(reference?.purchase_orders || []).map((po) => {
            const used = consumed[po.po_number] || 0;
            const pct = Math.min(100, (used / po.amount) * 100);
            const colour =
              used > po.amount
                ? "var(--fail-solid)"
                : pct >= 99.5
                  ? "var(--warn-solid)"
                  : "var(--ok-solid)";
            return (
              <div
                key={po.po_number}
                className="grid items-center gap-3 sm:grid-cols-[180px_minmax(0,1fr)_160px]"
              >
                <div className="min-w-0">
                  <div className="font-mono font-semibold">{po.po_number}</div>
                  <div className="truncate text-dim">{po.vendor}</div>
                </div>
                <div className="h-2.5 overflow-hidden rounded-full bg-panel2">
                  <div
                    className="h-full rounded-full transition-[width]"
                    style={{ width: `${pct}%`, background: colour }}
                  />
                </div>
                <div className="text-right whitespace-nowrap">
                  <b>{money(used)}</b> <span className="text-dim">/ {money(po.amount)}</span>
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      {open && (
        <RunModal
          run={open}
          onClose={() => setOpen(null)}
          onReviewed={() => setNonce((n) => n + 1)}
        />
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-[var(--radius-card)] border border-border bg-panel p-4 shadow-[var(--shadow-card)]">
      <div
        className="text-2xl font-semibold"
        style={tone ? { color: `var(--${tone})` } : undefined}
      >
        {value}
      </div>
      <div className="text-dim">{label}</div>
    </div>
  );
}
