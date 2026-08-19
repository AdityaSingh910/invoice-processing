"use client";

/**
 * Everything rendered here was computed by the Python rule engine and stored
 * with the run. Nothing on this screen is generated, summarised or reworded by
 * a model -- the point of the panel is that a reviewer sees the same numbers
 * the decision was made from.
 */
import { amount, humanise, when } from "@/lib/format";
import { KvTable, StatusPill } from "./ui";
import type { Audit, RunRecord } from "@/lib/types";

/** Enough of a run to render its human ruling, if it has one. */
type ReviewedRun = Pick<
  RunRecord,
  "human_decision" | "final_decision" | "reviewed_by" | "reviewed_at" | "review_note"
>;

export default function AuditTrail({ audit, run }: { audit?: Audit; run?: ReviewedRun }) {
  if (!audit) {
    return <p className="text-dim">No audit trail was stored for this run.</p>;
  }

  const cur = audit.invoice?.currency || "USD";
  const po = audit.purchase_order || {};
  const c = audit.comparison || {};
  const ex = audit.extraction || {};

  // Provenance of the PO record. Never invented: the backend stores null when
  // the data layer does not know, and that is shown as unknown, not as blank.
  const source = po.po_number
    ? po.source_file
      ? `${po.source_file}${po.source_row != null ? `, row ${po.source_row}` : ", row unknown"}`
      : "not recorded"
    : "—";

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <StatusPill status={audit.automated_decision} />
        <span className="text-dim">{audit.reason}</span>
      </div>

      {run?.human_decision && <HumanRuling run={run} />}

      <div className="grid gap-4 md:grid-cols-2">
        <Section title="Invoice">
          <KvTable
            rows={[
              ["Invoice #", audit.invoice?.invoice_number || "—"],
              ["Vendor", audit.invoice?.vendor || "—"],
              ["Total", amount(audit.invoice?.total, cur)],
              ["Read by", ex.method || ex.route || "—"],
            ]}
          />
        </Section>

        <Section title="Matched purchase order">
          <KvTable
            rows={[
              ["PO", po.po_number || "none"],
              ["Matched via", po.matched_via || "—"],
              ["PO status", po.po_status || "—"],
              ["Source", source],
            ]}
          />
        </Section>
      </div>

      {/* The comparison only means anything once a PO was actually bound. */}
      {po.po_number && (
        <Section title="Values compared">
          <KvTable
            rows={[
              ["Invoice total", <b key="t">{amount(c.invoice_total, cur)}</b>],
              ["PO amount", amount(c.po_amount, po.po_currency || cur)],
              ["Already consumed", amount(c.consumed_before, po.po_currency || cur)],
              ["PO remaining", <b key="r">{amount(c.po_remaining, po.po_currency || cur)}</b>],
              [
                "Variance",
                <span key="v" style={Number(c.variance) > 0 ? { color: "var(--fail)" } : undefined}>
                  {amount(c.variance, cur)}
                </span>,
              ],
              ["Tolerance used", amount(c.tolerance, cur)],
            ]}
          />
        </Section>
      )}

      <Section title="Rules">
        <ul className="grid gap-1.5">
          {(audit.rules || []).map((r, i) => (
            <li key={i} className="flex items-start gap-2.5">
              <span className="dot mt-0.5" data-level={r.passed ? "ok" : "fail"}>
                {r.passed ? "✓" : "✕"}
              </span>
              <span className="min-w-0">
                <span className="font-medium">{r.name}</span>
                {r.detail && <span className="text-dim"> — {r.detail}</span>}
              </span>
            </li>
          ))}
        </ul>
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint uppercase">
        {title}
      </div>
      {children}
    </div>
  );
}

/** The history, once a person has ruled. Shown above the evidence so a reviewer
 *  opening it later sees immediately that it was already decided. */
function HumanRuling({ run }: { run: ReviewedRun }) {
  return (
    <div className="rounded-lg border border-border bg-panel2 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill status={run.final_decision} />
        <span>
          Reviewed by <b>{run.reviewed_by || "an unattributed reviewer"}</b>
          {run.reviewed_at ? ` on ${when(run.reviewed_at)}` : ""}
        </span>
      </div>
      {run.review_note && <div className="mt-1.5 text-dim">{run.review_note}</div>}
      <div className="mt-1.5 text-[12px] text-faint">
        The automated decision above is unchanged and kept on record.
      </div>
    </div>
  );
}

export { humanise };
