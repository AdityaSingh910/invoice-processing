"use client";

/**
 * The audit trail.
 *
 * Everything here was computed by the Python rule engine and stored with the
 * run. Nothing on this screen is generated, summarised or reworded by a model —
 * the point of the panel is that a reviewer sees the same numbers the decision
 * was made from, in the same order the engine evaluated them.
 */
import { amount, when } from "@/lib/format";
import type { Audit, RunRecord } from "@/lib/types";
import { Badge, KeyValues, StatusBadge } from "@/components/ui";
import { IconCheck, IconUser, IconX } from "@/components/ui/icons";
import { PoProvenance } from "./PoMatchPanel";

type Reviewed = Pick<
  RunRecord,
  "human_decision" | "final_decision" | "reviewed_by" | "reviewed_at" | "review_note"
>;

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="t-caption mb-2">{title}</h3>
      {children}
    </div>
  );
}

export default function AuditTrail({ audit, run }: { audit?: Audit; run?: Reviewed }) {
  if (!audit) {
    return <p className="t-meta">No audit trail was stored for this run.</p>;
  }

  const cur = audit.invoice?.currency || "USD";
  const po = audit.purchase_order || {};
  const c = audit.comparison || {};
  const rules = audit.rules || [];
  const failed = rules.filter((r) => !r.passed).length;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={audit.automated_decision} />
        <span className="t-meta">{audit.reason}</span>
      </div>

      {run?.human_decision && <HumanRuling run={run} />}

      <div className="grid gap-4 md:grid-cols-2">
        <Section title="Invoice">
          <KeyValues
            rows={[
              ["Invoice number", audit.invoice?.invoice_number || "—"],
              ["Vendor", audit.invoice?.vendor || "—"],
              [
                "Total",
                <span key="t" className="tnum">
                  {amount(audit.invoice?.total, cur)}
                </span>,
              ],
              ["Read by", audit.extraction?.method || audit.extraction?.route || "—"],
            ]}
          />
        </Section>

        <Section title="Matched purchase order">
          <PoProvenance audit={audit} />
        </Section>
      </div>

      {/* Only meaningful once a PO was actually bound. */}
      {po.po_number && (
        <Section title="Values compared">
          <KeyValues
            rows={[
              ["Invoice total", <b key="a" className="tnum">{amount(c.invoice_total, cur)}</b>],
              ["PO amount", <span key="b" className="tnum">{amount(c.po_amount, po.po_currency || cur)}</span>],
              [
                "Already consumed",
                <span key="c" className="tnum">{amount(c.consumed_before, po.po_currency || cur)}</span>,
              ],
              [
                "PO remaining",
                <b key="d" className="tnum">{amount(c.po_remaining, po.po_currency || cur)}</b>,
              ],
              [
                "Variance",
                <span
                  key="e"
                  className="tnum"
                  style={Number(c.variance) > 0 ? { color: "var(--bad)" } : undefined}
                >
                  {amount(c.variance, cur)}
                </span>,
              ],
              ["Tolerance applied", <span key="f" className="tnum">{amount(c.tolerance, cur)}</span>],
            ]}
          />
        </Section>
      )}

      <Section title={`Rules evaluated · ${rules.length - failed} of ${rules.length} passed`}>
        <ul className="flex flex-col gap-1.5">
          {rules.map((r, i) => (
            <li key={i} className="flex items-start gap-2.5 text-[12.5px] leading-snug">
              <span
                className="mt-0.5 shrink-0"
                style={{ color: r.passed ? "var(--ok)" : "var(--bad)" }}
              >
                {r.passed ? <IconCheck size={13} /> : <IconX size={13} />}
              </span>
              <span className="min-w-0">
                <span className="font-medium">{r.name}</span>
                {r.detail && <span className="text-muted"> — {r.detail}</span>}
              </span>
            </li>
          ))}
        </ul>
      </Section>
    </div>
  );
}

/** Shown above the evidence, so a reviewer opening a run later sees at once
 *  that a person already ruled on it. */
function HumanRuling({ run }: { run: Reviewed }) {
  const approved = run.final_decision === "HUMAN_APPROVED";
  return (
    <div className="rounded-[var(--radius-md)] border border-line bg-sunken p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={approved ? "ok" : "bad"} icon={<IconUser size={10} />}>
          {String(run.final_decision || "").replace(/_/g, " ").toLowerCase()}
        </Badge>
        <span className="text-[12.5px]">
          Reviewed by <b>{run.reviewed_by || "an unattributed reviewer"}</b>
          {run.reviewed_at ? ` on ${when(run.reviewed_at)}` : ""}
        </span>
      </div>
      {run.review_note && <p className="t-meta mt-1.5">{run.review_note}</p>}
      <p className="t-meta mt-1.5 text-[11px]">
        The automated decision above is unchanged and kept on record.
      </p>
    </div>
  );
}
