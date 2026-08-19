"use client";

/**
 * Overview.
 *
 * Ordered by what an AP lead needs in the first five seconds: what is waiting on
 * a human, how much is flowing through untouched, and where the exceptions are
 * coming from. Volume and value come after that, because they are reporting
 * rather than work.
 *
 * Every figure is computed from records the API returned. There is no sample
 * data and no placeholder series anywhere on this page.
 */
import { useMemo } from "react";
import { money } from "@/lib/format";
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
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
  StatusBadge,
  Tooltip,
} from "@/components/ui";
import {
  IconAlert,
  IconCheck,
  IconClock,
  IconInvoice,
  IconRefresh,
  IconUpload,
} from "@/components/ui/icons";
import { CHART_COLORS, DailyVolume, LegendDot, MeterBar } from "@/components/charts";
import type { Section } from "@/components/layout/AppShell";

export default function OverviewPage({
  runs,
  reference,
  onNavigate,
}: {
  runs: Async<RunRecord[]>;
  reference: Async<Reference>;
  onNavigate: (s: Section) => void;
}) {
  const rows = runs.data ?? [];
  const t = useMemo(() => totals(rows), [rows]);
  const days = useMemo(() => byDay(rows), [rows]);
  const pos = useMemo(
    () => poUsage(rows, reference.data?.purchase_orders ?? []),
    [rows, reference.data]
  );
  const reasons = useMemo(() => topExceptionReasons(rows), [rows]);
  const recent = useMemo(
    () =>
      [...rows]
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
        .slice(0, 6),
    [rows]
  );

  if (runs.error) {
    return (
      <PageBody>
        <PageHeader title="Overview" />
        <Card>
          <ErrorState description={runs.error} onRetry={runs.refresh} />
        </Card>
      </PageBody>
    );
  }

  const loading = runs.loading && !runs.data;

  return (
    <PageBody>
      <PageHeader
        title="Overview"
        description="Automation performance and everything currently waiting on a person."
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              onClick={runs.refresh}
              icon={<IconRefresh size={14} />}
            >
              Refresh
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => onNavigate("process")}
              icon={<IconUpload size={14} />}
            >
              Process invoice
            </Button>
          </>
        }
      />

      {/* ------------------------------------------------------------ KPIs */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {loading ? (
          Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[104px]" />)
        ) : (
          <>
            <Kpi
              label="Awaiting review"
              value={String(t.openExceptions)}
              tone={t.openExceptions > 0 ? "warning" : "success"}
              icon={<IconAlert size={14} />}
              caption={
                t.openExceptions === 0
                  ? "Nothing is blocked"
                  : t.valueHeld > 0
                    ? `${money(t.valueHeld)} held`
                    : "Amounts could not be read"
              }
              action={
                t.openExceptions > 0
                  ? { label: "Review queue", onClick: () => onNavigate("invoices") }
                  : undefined
              }
            />
            <Kpi
              label="Straight through"
              value={formatPercent(t.straightThroughRate)}
              tone="success"
              icon={<IconCheck size={14} />}
              caption={`${t.approved} approved with no human touch`}
              hint="Share of all invoices the rules approved on their own. Runs a person accepted are excluded — they are a success, but they are not automation."
            />
            <Kpi
              label="Value processed"
              value={compactMoney(t.valueProcessed)}
              icon={<IconInvoice size={14} />}
              caption={`${money(t.valueApproved)} approved`}
            />
            <Kpi
              label="Average run time"
              value={formatDuration(t.avgProcessingMs)}
              icon={<IconClock size={14} />}
              caption="Extraction through decision"
              hint="Mean of the summed stage timings the pipeline recorded for each run."
            />
          </>
        )}
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        {/* ------------------------------------------------------- volume */}
        <Card>
          <CardHeader
            title="Volume"
            description="Invoices processed per day, by outcome."
            actions={
              <div className="flex flex-wrap gap-3">
                <LegendDot color={CHART_COLORS.approved} label="Approved" value={t.approved} />
                <LegendDot color={CHART_COLORS.needsReview} label="Review" value={t.needsReview} />
                <LegendDot color={CHART_COLORS.rejected} label="Rejected" value={t.rejected} />
              </div>
            }
          />
          <div className="mt-5">
            {loading ? <Skeleton className="h-32 w-full" /> : <DailyVolume data={days} />}
          </div>
        </Card>

        {/* --------------------------------------------------- exceptions */}
        <Card>
          <CardHeader
            title="Why invoices stop"
            description="Most frequent failing rules."
          />
          <div className="mt-4">
            {loading ? (
              <div className="flex flex-col gap-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  <Skeleton key={i} className="h-8 w-full" />
                ))}
              </div>
            ) : reasons.length === 0 ? (
              <EmptyState
                icon={<IconCheck size={18} />}
                title="No failures recorded"
                description="Nothing has been held or rejected yet."
              />
            ) : (
              <ul className="flex flex-col gap-3">
                {reasons.map((r) => (
                  <li key={r.reason}>
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="min-w-0 flex-1 truncate text-[13px]" title={r.reason}>
                        {r.reason}
                      </span>
                      <span className="num shrink-0 text-[13px] font-semibold">{r.count}</span>
                    </div>
                    <div className="mt-1.5">
                      <MeterBar
                        height={4}
                        ariaLabel={`${r.count} occurrences`}
                        segments={[
                          { value: r.count, color: "var(--warning-solid)" },
                          {
                            value: Math.max(0, (reasons[0]?.count ?? 0) - r.count),
                            color: "transparent",
                          },
                        ]}
                      />
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        {/* ------------------------------------------------ PO budgets */}
        <Card>
          <CardHeader
            title="Purchase order budgets"
            description="Only approved invoices consume budget."
            actions={
              <Button variant="ghost" size="sm" onClick={() => onNavigate("reference")}>
                View all
              </Button>
            }
          />
          <div className="mt-4 flex flex-col gap-3.5">
            {reference.loading && !reference.data
              ? Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-9 w-full" />)
              : pos.map(({ po, consumed, pct, over }) => (
                  <div key={po.po_number}>
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="num text-[13px] font-medium">{po.po_number}</span>
                      <span className="num text-[12px] text-muted">
                        <span className="font-semibold text-fg">{money(consumed)}</span> /{" "}
                        {money(po.amount)}
                      </span>
                    </div>
                    <div className="mt-1.5">
                      <MeterBar
                        height={6}
                        ariaLabel={`${po.po_number} ${pct.toFixed(0)}% consumed`}
                        segments={[
                          {
                            value: consumed,
                            color: over
                              ? "var(--danger-solid)"
                              : pct >= 99.5
                                ? "var(--warning-solid)"
                                : "var(--success-solid)",
                          },
                          { value: Math.max(0, po.amount - consumed), color: "transparent" },
                        ]}
                      />
                    </div>
                    <div className="mt-1 flex items-center gap-2">
                      <span className="text-[11px] text-subtle">{po.vendor}</span>
                      {pct >= 99.5 && !over && <Badge tone="warning">exhausted</Badge>}
                      {over && <Badge tone="danger">over budget</Badge>}
                    </div>
                  </div>
                ))}
          </div>
        </Card>

        {/* ---------------------------------------------------- activity */}
        <Card padded={false}>
          <div className="p-4 sm:p-5">
            <CardHeader
              title="Recent activity"
              actions={
                <Button variant="ghost" size="sm" onClick={() => onNavigate("invoices")}>
                  View all
                </Button>
              }
            />
          </div>

          {loading ? (
            <div className="px-4 pb-4 sm:px-5">
              <SkeletonList />
            </div>
          ) : recent.length === 0 ? (
            <EmptyState
              icon={<IconInvoice size={18} />}
              title="Nothing processed yet"
              description="Run your first invoice to see it here."
              action={
                <Button size="sm" variant="primary" onClick={() => onNavigate("process")}>
                  Process an invoice
                </Button>
              }
            />
          ) : (
            <ul className="divide-y divide-border border-t border-border">
              {recent.map((r) => (
                <li key={r.id}>
                  <button
                    onClick={() => onNavigate("invoices")}
                    className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-surface2 sm:px-5"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[13px] font-medium">{r.filename}</span>
                      <span className="block truncate text-[12px] text-subtle">
                        {r.vendor_name || "unknown vendor"}
                        {r.po_number ? ` · ${r.po_number}` : ""}
                      </span>
                    </span>
                    <span className="num shrink-0 text-[13px] font-semibold">{money(r.total)}</span>
                    <StatusBadge status={r.status} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </PageBody>
  );
}

function SkeletonList() {
  return (
    <div className="flex flex-col gap-3 pt-2">
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="flex items-center gap-3">
          <Skeleton className="h-3.5 flex-1" />
          <Skeleton className="h-3.5 w-16" />
          <Skeleton className="h-4 w-16 rounded-full" />
        </div>
      ))}
    </div>
  );
}

function Kpi({
  label,
  value,
  caption,
  icon,
  tone,
  hint,
  action,
}: {
  label: string;
  value: string;
  caption?: string;
  icon?: React.ReactNode;
  tone?: "success" | "warning" | "danger";
  hint?: string;
  action?: { label: string; onClick: () => void };
}) {
  return (
    <Card className="flex flex-col justify-between">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className="grid h-6 w-6 place-items-center rounded-[var(--radius-sm)] border"
            style={{
              background: tone ? `var(--${tone}-weak)` : "var(--surface-2)",
              borderColor: tone ? `var(--${tone}-line)` : "var(--border)",
              color: tone ? `var(--${tone})` : "var(--fg-subtle)",
            }}
          >
            {icon}
          </span>
          <span className="label">{label}</span>
        </div>
        {hint && (
          <Tooltip label={hint}>
            <span
              tabIndex={0}
              className="grid h-4 w-4 cursor-help place-items-center rounded-full border border-border text-[10px] text-subtle"
            >
              ?
            </span>
          </Tooltip>
        )}
      </div>

      <div className="mt-3">
        <div
          className="num text-[26px] leading-none font-semibold tracking-[-0.03em]"
          style={tone ? { color: `var(--${tone})` } : undefined}
        >
          {value}
        </div>
        {caption && <p className="mt-1.5 text-[12px] text-subtle">{caption}</p>}
      </div>

      {action && (
        <div className="mt-3">
          <Button size="sm" variant="secondary" onClick={action.onClick}>
            {action.label}
          </Button>
        </div>
      )}
    </Card>
  );
}
