"use client";

/**
 * Overview.
 *
 * Ordered by what an AP lead needs first: what is blocked on a person, then how
 * much is flowing through untouched, then reporting. The exception queue is the
 * only metric that implies work, so it is the one given size and an action.
 *
 * Every figure is computed from records the API returned. No sample data, no
 * placeholder series, and no metric the backend cannot actually support.
 */
import { useMemo } from "react";
import { money, whenCompact } from "@/lib/format";
import {
  byDay,
  compactMoney,
  formatDuration,
  formatPercent,
  poUsage,
  topExceptionReasons,
  totals,
} from "@/lib/metrics";
import type { Reference, RunRecord } from "@/lib/types";
import type { Async } from "@/lib/useData";
import type { Section } from "@/components/layout/AppShell";
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Meter,
  Panel,
  PanelHeader,
  Skeleton,
  StatusBadge,
  Tooltip,
} from "@/components/ui";
import {
  IconAlert,
  IconArrowUp,
  IconCheck,
  IconChevronRight,
  IconClock,
  IconInvoice,
  IconRefresh,
  IconUpload,
} from "@/components/ui/icons";
import { LegendItem, SERIES, Sparkline, VolumeChart } from "@/components/charts";
import ResetDemoButton from "@/components/ResetDemoButton";

export default function OverviewPage({
  runs,
  reference,
  onNavigate,
}: {
  runs: Async<RunRecord[]>;
  reference: Async<Reference>;
  onNavigate: (s: Section) => void;
}) {
  const rows = useMemo(() => runs.data ?? [], [runs.data]);
  const t = useMemo(() => totals(rows), [rows]);
  const days = useMemo(() => byDay(rows), [rows]);
  const reasons = useMemo(() => topExceptionReasons(rows), [rows]);
  const pos = useMemo(
    () => poUsage(rows, reference.data?.purchase_orders ?? []),
    [rows, reference.data]
  );
  const recent = useMemo(
    () =>
      [...rows]
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
        .slice(0, 7),
    [rows]
  );

  const loading = runs.loading && !runs.data;

  if (runs.error) {
    return (
      <>
        <PageHeader title="Overview" />
        <PageBody>
          <Panel>
            <ErrorState description={runs.error} onRetry={runs.refresh} />
          </Panel>
        </PageBody>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Overview"
        description="Automation performance and everything waiting on a person."
        actions={
          <>
            <ResetDemoButton onReset={runs.refresh} />
            <Button size="sm" onClick={runs.refresh} icon={<IconRefresh size={13} />}>
              Refresh
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => onNavigate("process")}
              icon={<IconUpload size={13} />}
            >
              Process invoice
            </Button>
          </>
        }
      />

      <PageBody>
        {/* ---------------------------------------------------------- hero + strip
            The exception queue is the only figure here that means work, so it
            gets its own panel and an action. The rest are reporting and share
            one divided strip — four separate floating boxes give equal weight
            to metrics that are not equally important. */}
        <div className="grid gap-4 lg:grid-cols-[minmax(0,300px)_minmax(0,1fr)]">
          {loading ? (
            <Skeleton className="h-[132px]" />
          ) : (
            <Panel
              className={t.openExceptions > 0 ? "border-warn-line" : ""}
              hover={t.openExceptions > 0}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span
                    className={`grid h-6 w-6 place-items-center rounded-[var(--radius-sm)] ${
                      t.openExceptions > 0 ? "bg-warn-quiet text-warn" : "bg-ok-quiet text-ok"
                    }`}
                  >
                    {t.openExceptions > 0 ? <IconAlert size={13} /> : <IconCheck size={13} />}
                  </span>
                  <span className="t-caption">Awaiting review</span>
                </div>
                {t.openExceptions > 0 && (
                  <span className="mt-1.5 h-1.5 w-1.5 rounded-full bg-warn-vivid pulse-ring" />
                )}
              </div>

              <div className="mt-3 flex items-end gap-2.5">
                <span className="t-display tnum">{t.openExceptions}</span>
                <span className="t-meta mb-1">
                  {t.openExceptions === 1 ? "invoice" : "invoices"}
                </span>
              </div>

              <p className="t-meta mt-1.5">
                {t.openExceptions === 0
                  ? "Nothing is blocked. Every invoice cleared the rules or was ruled on."
                  : t.valueHeld > 0
                    ? `${money(t.valueHeld)} of spend is held pending a decision.`
                    : "Amounts could not be read on the held invoices."}
              </p>

              {t.openExceptions > 0 && (
                <Button
                  size="sm"
                  variant="secondary"
                  className="mt-3 w-full"
                  onClick={() => onNavigate("invoices")}
                  icon={<IconChevronRight size={13} />}
                >
                  Open review queue
                </Button>
              )}
            </Panel>
          )}

          {loading ? (
            <Skeleton className="h-[132px]" />
          ) : (
            <Panel flush>
              <div className="grid grid-cols-1 divide-line sm:grid-cols-3 sm:divide-x sm:divide-y-0">
                <Stat
                  label="Straight through"
                  value={formatPercent(t.straightThroughRate)}
                  caption={`${t.approved} of ${t.runs} with no human touch`}
                  icon={<IconArrowUp size={12} />}
                  tone="ok"
                  hint="Share of all invoices the rules approved on their own. Runs a person accepted are excluded — a success, but not automation."
                  spark={days.map((d) => d.approved)}
                  sparkTone="var(--ok-vivid)"
                />
                <Stat
                  label="Value processed"
                  value={compactMoney(t.valueProcessed)}
                  caption={`${money(t.valueApproved)} approved`}
                  icon={<IconInvoice size={12} />}
                  spark={days.map((d) => d.total)}
                />
                <Stat
                  label="Average run time"
                  value={formatDuration(t.avgProcessingMs)}
                  caption="Extraction through decision"
                  icon={<IconClock size={12} />}
                  hint="Mean of the summed stage timings the pipeline recorded per run."
                />
              </div>
            </Panel>
          )}
        </div>

        {/* ------------------------------------------------------ volume + why */}
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
          <Panel>
            <PanelHeader
              title="Volume"
              description="Last 14 days, by outcome"
              actions={
                <div className="flex flex-wrap gap-3">
                  <LegendItem color={SERIES.approved} label="Approved" value={t.approved} />
                  <LegendItem color={SERIES.needsReview} label="Review" value={t.needsReview} />
                  <LegendItem color={SERIES.rejected} label="Rejected" value={t.rejected} />
                </div>
              }
            />
            <div className="mt-4">
              {loading ? <Skeleton className="h-[132px] w-full" /> : <VolumeChart data={days} />}
            </div>
          </Panel>

          <Panel flush>
            <PanelHeader
              bordered
              title="Why invoices stop"
              description="Ranked by how often the rule bites"
            />
            {loading ? (
              <div className="flex flex-col gap-3 p-4">
                {Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-8 w-full" />
                ))}
              </div>
            ) : reasons.length === 0 ? (
              <EmptyState
                compact
                icon={<IconCheck size={16} />}
                title="No rules have failed"
                description="Nothing has been held or rejected yet."
              />
            ) : (
              <ul className="divide-line">
                {reasons.map((r, i) => {
                  // Ranked by frequency, and coloured by whether the rule is a
                  // hard stop (rejects) or a hold (needs a person).
                  const blocking = /duplicate|vendor not approved/i.test(r.reason);
                  return (
                    <li key={r.reason}>
                      <button
                        onClick={() => onNavigate("invoices")}
                        className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-hover"
                      >
                        <span className="tnum w-3 shrink-0 text-[11px] text-faint">{i + 1}</span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-baseline justify-between gap-2">
                            <span className="truncate text-[12.5px]" title={r.reason}>
                              {r.reason}
                            </span>
                            <span className="tnum shrink-0 text-[13px] font-semibold">
                              {r.count}
                            </span>
                          </span>
                          <span className="mt-1.5 block">
                            <Meter
                              value={r.count}
                              max={reasons[0].count}
                              tone={blocking ? "bad" : "warn"}
                              height={3}
                              ariaLabel={`${r.count} occurrences`}
                            />
                          </span>
                        </span>
                        <Badge tone={blocking ? "bad" : "warn"}>
                          {blocking ? "Blocks" : "Holds"}
                        </Badge>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </Panel>
        </div>

        {/* ----------------------------------------------------- budgets + feed */}
        <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
          <Panel flush>
            <PanelHeader
              bordered
              title="Purchase order budgets"
              description="Only approved invoices consume budget"
              actions={
                <Button size="xs" variant="ghost" onClick={() => onNavigate("reference")}>
                  View all
                </Button>
              }
            />
            {reference.loading && !reference.data ? (
              <div className="flex flex-col gap-3 p-4">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-7 w-full" />
                ))}
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[520px] border-collapse">
                  <thead>
                    <tr>
                      <th className="t-caption border-b border-line px-4 py-2 text-left">PO</th>
                      <th className="t-caption border-b border-line px-3 py-2 text-left">Vendor</th>
                      <th className="t-caption border-b border-line px-3 py-2 text-right">
                        Consumed
                      </th>
                      <th className="t-caption border-b border-line px-3 py-2 text-right">
                        Remaining
                      </th>
                      <th className="t-caption w-[124px] border-b border-line px-4 py-2 text-left">
                        Utilisation
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-line">
                    {pos.map(({ po, consumed, remaining, pct, over }) => {
                      const tone = over ? "bad" : pct >= 99.5 ? "warn" : "ok";
                      return (
                        <tr key={po.po_number} className="transition-colors hover:bg-hover">
                          <td className="tnum px-4 py-2 text-[12.5px] font-medium">
                            {po.po_number}
                          </td>
                          <td className="max-w-[150px] truncate px-3 py-2 text-[12.5px] text-muted">
                            {po.vendor}
                          </td>
                          <td className="tnum px-3 py-2 text-right text-[12.5px]">
                            {money(consumed)}
                          </td>
                          <td className="tnum px-3 py-2 text-right text-[12.5px] font-medium">
                            {money(remaining)}
                          </td>
                          <td className="px-4 py-2">
                            <div className="flex items-center gap-2">
                              <Meter
                                value={consumed}
                                max={po.amount}
                                tone={tone}
                                height={4}
                                ariaLabel={`${pct.toFixed(0)}% consumed`}
                              />
                              <span className="tnum w-8 shrink-0 text-right text-[11px] text-faint">
                                {pct.toFixed(0)}%
                              </span>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Panel flush>
            <PanelHeader
              bordered
              title="Recent activity"
              actions={
                <Button size="xs" variant="ghost" onClick={() => onNavigate("invoices")}>
                  View all
                </Button>
              }
            />
            {loading ? (
              <div className="flex flex-col gap-3 p-4">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-7 w-full" />
                ))}
              </div>
            ) : recent.length === 0 ? (
              <EmptyState
                compact
                icon={<IconInvoice size={16} />}
                title="Nothing processed yet"
                description="Run your first invoice to see it here."
                action={
                  <Button size="sm" variant="primary" onClick={() => onNavigate("process")}>
                    Process an invoice
                  </Button>
                }
              />
            ) : (
              <ul className="divide-line">
                {recent.map((r) => (
                  <li key={r.id}>
                    <button
                      onClick={() => onNavigate("invoices")}
                      className="flex w-full items-center gap-3 px-4 py-2 text-left transition-colors hover:bg-hover"
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[12.5px] font-medium">
                          {r.filename}
                        </span>
                        <span className="t-meta block truncate text-[11px]">
                          {r.vendor_name || "unknown vendor"}
                          {r.po_number ? ` · ${r.po_number}` : ""}
                        </span>
                      </span>
                      <span className="tnum shrink-0 text-[12.5px] font-medium">
                        {money(r.total)}
                      </span>
                      <StatusBadge status={r.status} />
                      <span className="tnum t-meta w-11 shrink-0 text-right text-[11px]">
                        {whenCompact(r.created_at)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>
      </PageBody>
    </>
  );
}

/** One cell of the reporting strip. */
function Stat({
  label,
  value,
  caption,
  icon,
  tone,
  hint,
  spark,
  sparkTone,
}: {
  label: string;
  value: string;
  caption: string;
  icon?: React.ReactNode;
  tone?: "ok";
  hint?: string;
  spark?: number[];
  sparkTone?: string;
}) {
  return (
    <div className="flex flex-col justify-between p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5">
          <span className={tone === "ok" ? "text-ok" : "text-faint"}>{icon}</span>
          <span className="t-caption">{label}</span>
        </span>
        {hint && (
          <Tooltip label={hint}>
            <span
              tabIndex={0}
              className="grid h-3.5 w-3.5 cursor-help place-items-center rounded-full border border-line text-[9px] text-faint"
            >
              ?
            </span>
          </Tooltip>
        )}
      </div>

      <div className="mt-2.5 flex items-end justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="t-metric tnum">{value}</div>
          <p className="t-meta mt-1 text-[11px] leading-snug">{caption}</p>
        </div>
        {spark && spark.some((v) => v > 0) && (
          <div className="hidden shrink-0 xl:block">
            <Sparkline values={spark} tone={sparkTone ?? "var(--accent)"} width={64} />
          </div>
        )}
      </div>
    </div>
  );
}
