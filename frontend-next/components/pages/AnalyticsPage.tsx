"use client";

/**
 * Analytics.
 *
 * EVERY FIGURE ON THIS SCREEN IS COMPUTED BY THE SERVER. This file formats and
 * arranges; it does not calculate a single KPI. That is the point: the rates
 * come from backend/analytics.py, which derives them from the rows an auditor
 * would query, so nothing here can put a number on screen that the database
 * would not agree with. (Overview, by contrast, summarises the runs the
 * browser already holds — a different job, kept separate on purpose.)
 *
 * THREE STATES, NOT TWO. A KPI can be a real figure, a real figure over too
 * small a sample to read as one, or genuinely undefined because its
 * denominator is zero. The API returns `null` for the last of those rather
 * than 0, and this screen renders all three differently — "—" for undefined,
 * a muted qualifier for a thin sample, and the figure itself otherwise. A
 * dashboard reading "100% automated" from two invoices, or "0% automated" on a
 * day with no invoices, is stating something untrue, and both are easy to ship
 * by accident.
 *
 * FOUR QUESTIONS, IN THIS ORDER, AND NOTHING ELSE: how much is automated (the
 * KPI row), what was decided (Decision overview), why anything stopped (Why
 * invoices need attention), and how much came through (Processing volume) —
 * then purchase-order and vendor context as a smaller closing row.
 *
 * The screen used to carry six more panels: task success, automation over
 * time, per-stage timing, the review funnel, reviewer workload and the email
 * ingestion funnel. They were removed deliberately, not lost. Every one of
 * them is still computed and still served — `/api/analytics/reviews`,
 * `/api/analytics/processing`, `/api/analytics/users`, `/api/analytics/email`
 * and `/api/analytics/trends` are untouched, as is the combined
 * `/api/analytics/dashboard` this screen reads — so nothing here is a claim
 * that those figures stopped mattering. This page stopped showing them.
 */
import { useState } from "react";
import { amount } from "@/lib/format";
import {
  bucketsToDays,
  formatCount,
  formatDuration,
  formatPercent,
  kpiState,
} from "@/lib/metrics";
import type {
  AnalyticsDashboard,
  AnalyticsOverview,
  AnalyticsProcessing,
  AnalyticsReviews,
  AnalyticsTrends,
  AnalyticsVendors,
  Kpi,
} from "@/lib/types";
import { useAnalyticsDashboard, type Async, type RangeKey } from "@/lib/useData";
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Meter,
  Panel,
  PanelHeader,
  Segmented,
  Skeleton,
  TD,
  TH,
  Tooltip,
} from "@/components/ui";
import {
  IconAlert,
  IconAnalytics,
  IconCheck,
  IconClock,
  IconInvoice,
  IconRefresh,
} from "@/components/ui/icons";
import { LegendItem, SERIES, VolumeChart } from "@/components/charts";

/** How many rows the two closing panels show. Named rather than inlined
 *  because the "showing N of M" line beneath each has to say the same number
 *  the slice used, and two literals drift. */
const VENDOR_ROWS = 5;
const PO_ROWS = 5;

const RANGES: { value: RangeKey; label: string }[] = [
  { value: "today", label: "Today" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
  { value: "month", label: "Month" },
  { value: "all", label: "All" },
];

export default function AnalyticsPage() {
  const [range, setRange] = useState<RangeKey>("30d");
  const [reloadKey, setReloadKey] = useState(0);

  /**
   * ONE REQUEST FOR SEVEN PANELS.
   *
   * This screen used to call seven endpoints in parallel. Parallel bought
   * nothing: the seven queue behind one another on the way to the database, so
   * the page cost roughly the SUM of them -- about thirteen seconds against the
   * live deployment, where running the same seven sequentially took twelve.
   * `/api/analytics/dashboard` returns the identical seven payloads from one
   * pass over the window, so the page pays one TLS round trip instead of seven
   * and the server reads the rows the panels share once instead of three times.
   *
   * The seven single endpoints still exist and are unchanged -- this is a
   * cheaper way to ask for all of them, not a replacement for asking for one.
   */
  const dashboard = useAnalyticsDashboard(range, true, reloadKey);

  /**
   * Each panel below still reads a resource of its own shape -- `data`,
   * `loading`, `error` -- so nothing downstream of here had to change when the
   * seven requests became one. They now share one request's state, which is
   * simply the truth: there is one request, so the panels succeed and fail
   * together rather than seven ways.
   */
  const section = <T,>(pick: (d: AnalyticsDashboard) => T): Async<T> => ({
    data: dashboard.data ? pick(dashboard.data) : null,
    loading: dashboard.loading,
    error: dashboard.error,
    refresh: dashboard.refresh,
  });

  const overview = section<AnalyticsOverview>((d) => d.overview);
  const trends = section<AnalyticsTrends>((d) => d.trends);
  const processing = section<AnalyticsProcessing>((d) => d.processing);
  const reviews = section<AnalyticsReviews>((d) => d.reviews);
  const vendors = section<AnalyticsVendors>((d) => d.vendors);
  // `users` and `email` are still in the payload -- the request is unchanged --
  // but this screen no longer renders a reviewer-workload or email-ingestion
  // panel, so nothing reads them here.

  /**
   * One press of this button is now ONE request rather than seven, but the
   * guard stays, because the reason for it has not gone away.
   *
   * `useResource` coalesces repeat presses per resource (see lib/useData.ts),
   * and this screen still does not call `resource.refresh()` -- it changes a
   * reloadKey the hook depends on, so a press restarts the request regardless
   * of what it is doing. These endpoints are metered per user AND per IP
   * (§7e.4) and hold a database connection, and a burst of them is what used
   * to exhaust the connection pool and turn neighbouring reads into 500s.
   *
   * So while the screen is still loading the button is disabled and this is a
   * no-op. Disabled rather than silently ignored: a control that looks
   * pressable and does nothing reads as a broken button, and the reason it is
   * unavailable -- something is already loading -- is exactly what the reader
   * needs to know.
   */
  const anyLoading = dashboard.loading;

  const refresh = () => {
    if (anyLoading) return;
    setReloadKey((k) => k + 1);
  };

  // The overview is the one request the whole screen depends on: if it failed,
  // every panel below it would render its own identical error, which reads as
  // six failures rather than one.
  if (overview.error) {
    return (
      <>
        <PageHeader title="Analytics" />
        <PageBody>
          <Panel>
            <ErrorState description={overview.error} onRetry={refresh} />
          </Panel>
        </PageBody>
      </>
    );
  }

  const o = overview.data;
  const loading = overview.loading && !o;

  return (
    <>
      <PageHeader
        title="Analytics"
        description="Every figure below is computed by the server from the runs on file."
        actions={
          <>
            <Segmented
              value={range}
              options={RANGES}
              onChange={(v) => setRange(v)}
              ariaLabel="Reporting period"
            />
            <Button
              size="sm"
              onClick={refresh}
              disabled={anyLoading}
              icon={<IconRefresh size={13} />}
            >
              {anyLoading ? "Refreshing…" : "Refresh"}
            </Button>
          </>
        }
      />

      <PageBody>
        {/* ------------------------------------------------------------- KPIs */}
        {loading ? (
          <Skeleton className="h-[124px]" />
        ) : !o ? null : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <KpiCell
              label="Automation rate"
              kpi={o.kpis.automation_rate}
              caption={`${formatCount(o.volume.automated)} of ${formatCount(o.volume.runs)} decided by rules`}
              icon={<IconCheck size={12} />}
              tone="ok"
            />
            <KpiCell
              label="Processing success"
              kpi={o.kpis.processing_success_rate}
              caption={`${formatCount(o.volume.extraction_failures)} unreadable`}
              icon={<IconInvoice size={12} />}
              tone={o.volume.extraction_failures > 0 ? "bad" : undefined}
            />
            <KpiCell
              label="Human review rate"
              kpi={o.kpis.human_review_rate}
              caption={`${formatCount(o.backlog.awaiting_review)} still awaiting`}
              icon={<IconAlert size={12} />}
              tone={o.backlog.awaiting_review > 0 ? "warn" : undefined}
            />
            <KpiCell
              label="Avg run time"
              literal={formatDuration(processing.data?.run_time_ms.average ?? null)}
              caption={
                processing.data
                  ? `median ${formatDuration(processing.data.run_time_ms.median)} · ${formatCount(
                      processing.data.run_time_ms.samples
                    )} timed`
                  : "loading"
              }
              hint="Mean of the summed per-stage timings the pipeline recorded. Runs with no timings are excluded rather than counted as zero."
              icon={<IconClock size={12} />}
            />
          </div>
        )}

        {/* ------------------------------------------------- decision overview */}
        <Panel>
          <PanelHeader
            title="Decision overview"
            // WHAT THE RULES DECIDED, not what the ledger currently reads.
            // The two differ exactly where a person later moved a run, and the
            // three proportion bars that used to sit under these counts were
            // where that difference was visible. They were removed on request;
            // the distinction still exists in the data and on the Invoices
            // screen, so this description has to be the thing that says which
            // of the two these numbers are.
            description="What the deterministic rules concluded, in this period"
          />
          {loading || !o ? (
            <Skeleton className="mt-4 h-[92px]" />
          ) : o.volume.runs === 0 ? (
            <EmptyState
              compact
              icon={<IconAnalytics size={16} />}
              title="No invoices in this period"
              description="Choose a wider range, or process an invoice."
            />
          ) : (
            <div className="mt-4 grid grid-cols-3 gap-4">
              <HeadlineCount
                label="Approved"
                value={o.decisions.automated.APPROVED}
                total={o.volume.runs}
                color={SERIES.approved}
              />
              <HeadlineCount
                label="Review"
                value={o.decisions.automated.NEEDS_REVIEW}
                total={o.volume.runs}
                color={SERIES.needsReview}
              />
              <HeadlineCount
                label="Rejected"
                value={o.decisions.automated.REJECTED}
                total={o.volume.runs}
                color={SERIES.rejected}
              />
            </div>
          )}
        </Panel>

        {/* ------------------------------------------ why invoices need attention */}
        <Panel flush>
          <PanelHeader
            bordered
            title="Why invoices need attention"
            description="Grouped by the rule that failed, not by the reason sentence"
          />
          {reviews.error ? (
            <div className="p-4">
              <ErrorState description={reviews.error} onRetry={refresh} />
            </div>
          ) : reviews.loading && !reviews.data ? (
            <div className="p-4">
              <Skeleton className="h-[180px]" />
            </div>
          ) : !reviews.data?.reasons.length ? (
            <EmptyState
              compact
              icon={<IconCheck size={16} />}
              title="No rule failed in this period"
              description="Nothing was held or rejected."
            />
          ) : (
            <ul className="divide-line">
              {reviews.data.reasons.slice(0, 8).map((r) => (
                <li key={r.rule} className="flex items-center gap-3 px-4 py-2.5">
                  <span className="min-w-0 flex-1 truncate text-[13.5px]">{r.rule}</span>
                  <span className="w-[120px] shrink-0">
                    <Meter
                      value={r.share_of_runs ?? 0}
                      max={1}
                      tone="warn"
                      ariaLabel={`${r.rule}: ${formatPercent(r.share_of_runs)} of runs`}
                    />
                  </span>
                  <span className="tnum w-14 shrink-0 text-right text-[13.5px] font-semibold">
                    {formatCount(r.runs)}
                  </span>
                </li>
              ))}
              {/* Said explicitly, because a table of these looks like it
                  should sum to the run count and does not. */}
              <li className="t-meta px-4 py-2 text-[12px]">
                An invoice failing several rules appears in several rows.
              </li>
            </ul>
          )}
        </Panel>

        {/* -------------------------------------------------- processing volume */}
        <Panel>
          <PanelHeader
            title="Processing volume"
            // "By outcome" would be ambiguous here: Overview's chart of the
            // same name is keyed on the LEDGER STATUS, while this whole
            // screen is framed on what the RULES decided (which no later
            // ruling rewrites). Same word, different numbers — so this one
            // says which it means.
            description={`By the rules' verdict, per UTC day${
              trends.data ? ` · ${trends.data.buckets.length} days` : ""
            }`}
            actions={
              o && (
                <div className="flex flex-wrap gap-3">
                  <LegendItem color={SERIES.approved} label="Approved" value={o.decisions.automated.APPROVED} />
                  <LegendItem color={SERIES.needsReview} label="Review" value={o.decisions.automated.NEEDS_REVIEW} />
                  <LegendItem color={SERIES.rejected} label="Rejected" value={o.decisions.automated.REJECTED} />
                </div>
              )
            }
          />
          <div className="mt-4">
            {trends.loading && !trends.data ? (
              <Skeleton className="h-[132px] w-full" />
            ) : trends.error ? (
              <ErrorState description={trends.error} onRetry={refresh} />
            ) : (
              <VolumeChart data={bucketsToDays(trends.data?.buckets ?? [])} />
            )}
          </div>
        </Panel>

        {/* ------------------------------------ purchase order / vendor insights */}
        {/* The closing row, and deliberately the smallest thing on the page.
            The full tables these replace — every vendor with five rates each,
            every PO with four money columns — were more detail than a summary
            screen can carry, and both already exist in full at
            /api/analytics/vendors. What is left is the part somebody scanning
            this page can act on: who sends the most invoices, and which
            purchase orders are close to spent. */}
        <div className="grid items-start gap-4 xl:grid-cols-2">
          <Panel flush>
            <PanelHeader
              bordered
              title="Top vendors"
              description="Busiest in this period, and how often they are held"
            />
            {vendors.error ? (
              <div className="p-4">
                <ErrorState description={vendors.error} onRetry={refresh} />
              </div>
            ) : vendors.loading && !vendors.data ? (
              <div className="p-4">
                <Skeleton className="h-[140px]" />
              </div>
            ) : !vendors.data?.vendors.length ? (
              <EmptyState
                compact
                icon={<IconInvoice size={16} />}
                title="No invoices in this period"
              />
            ) : (
              <>
                <DataTable minWidth={320}>
                  <thead>
                    <tr>
                      <TH>Vendor</TH>
                      <TH align="right">Invoices</TH>
                      <TH align="right">Held</TH>
                    </tr>
                  </thead>
                  <tbody>
                    {vendors.data.vendors.slice(0, VENDOR_ROWS).map((v) => (
                      <tr key={v.vendor}>
                        <TD className="max-w-[200px] truncate font-medium">{v.vendor}</TD>
                        <TD align="right">{formatCount(v.runs)}</TD>
                        <TD align="right">{formatPercent(v.hold_rate)}</TD>
                      </tr>
                    ))}
                  </tbody>
                </DataTable>
                {/* Never a silently short list: a reader who cannot tell five
                    vendors from five of forty cannot read the table above it. */}
                {vendors.data.vendors.length > VENDOR_ROWS && (
                  <p className="t-meta border-t border-line px-4 py-2.5 text-[12px]">
                    Showing {VENDOR_ROWS} of {formatCount(vendors.data.vendors.length)} vendors.
                  </p>
                )}
              </>
            )}
          </Panel>

          <Panel flush>
            <PanelHeader
              bordered
              title="Purchase order budgets"
              description="Balances are the ledger's own, all-time"
            />
            {vendors.error ? (
              <div className="p-4">
                <ErrorState description={vendors.error} onRetry={refresh} />
              </div>
            ) : vendors.loading && !vendors.data ? (
              <div className="p-4">
                <Skeleton className="h-[140px]" />
              </div>
            ) : !vendors.data?.purchase_orders.length ? (
              <EmptyState compact icon={<IconInvoice size={16} />} title="No purchase orders on file" />
            ) : (
              <>
                <DataTable minWidth={360}>
                  <thead>
                    <tr>
                      <TH>PO</TH>
                      <TH>Used</TH>
                      <TH align="right">Remaining</TH>
                    </tr>
                  </thead>
                  <tbody>
                    {vendors.data.purchase_orders.slice(0, PO_ROWS).map((p) => (
                      <tr key={p.po_number}>
                        <TD>
                          <span className="tnum font-medium">{p.po_number}</span>
                          <span className="t-meta block max-w-[150px] truncate text-[12px]">
                            {p.vendor}
                          </span>
                        </TD>
                        <TD className="w-[110px]">
                          <div className="flex items-center gap-2">
                            <Meter
                              value={p.consumed}
                              max={p.amount}
                              tone={
                                p.over_budget
                                  ? "bad"
                                  : p.utilisation && p.utilisation > 0.8
                                    ? "warn"
                                    : "accent"
                              }
                              ariaLabel={`${p.po_number} budget used`}
                            />
                            <span className="tnum t-meta w-8 shrink-0 text-right text-[12px]">
                              {formatPercent(p.utilisation)}
                            </span>
                          </div>
                        </TD>
                        <TD align="right">
                          <span className={p.over_budget ? "text-bad" : undefined}>
                            {amount(p.remaining, p.currency)}
                          </span>
                        </TD>
                      </tr>
                    ))}
                  </tbody>
                </DataTable>
                {vendors.data.purchase_orders.length > PO_ROWS && (
                  <p className="t-meta border-t border-line px-4 py-2.5 text-[12px]">
                    Showing {PO_ROWS} of {formatCount(vendors.data.purchase_orders.length)} purchase
                    orders.
                  </p>
                )}
              </>
            )}
          </Panel>
        </div>

        {/* ------------------------------------------------------ data quality */}
        {o && (o.data_quality.malformed_total > 0 || o.volume.runs > 0) && (
          <Panel>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="t-meta text-[12.5px]">
                Computed from {formatCount(o.data_quality.runs_scanned)} runs
                {o.data_quality.runs_with_timing !== o.data_quality.runs_scanned && (
                  <> · {formatCount(o.data_quality.runs_with_timing)} carried stage timings</>
                )}
                {" · "}
                {o.range.label} in {o.range.timezone}
              </p>
              {o.data_quality.malformed_total > 0 && (
                // Surfaced rather than swallowed: a reader comparing two
                // figures deserves to know some rows contributed to neither.
                <Badge tone="warn">
                  {formatCount(o.data_quality.malformed_total)} unreadable record
                  {o.data_quality.malformed_total === 1 ? "" : "s"} skipped
                </Badge>
              )}
            </div>
          </Panel>
        )}
      </PageBody>
    </>
  );
}

/* ------------------------------------------------------------- components */

/**
 * One KPI cell.
 *
 * `kpi` renders a server-computed rate through the three-state rule described
 * at the top of this file; `literal` is for the one tile (average run time)
 * that is a duration rather than a rate. Exactly one of the two is passed.
 */
function KpiCell({
  label,
  kpi,
  literal,
  caption,
  icon,
  tone,
  hint,
}: {
  label: string;
  kpi?: Kpi;
  literal?: string;
  caption: string;
  icon?: React.ReactNode;
  tone?: "ok" | "warn" | "bad";
  hint?: string;
}) {
  const state = kpi ? kpiState(kpi) : "ok";
  const help = hint ?? kpi?.definition;

  const toneClass =
    tone === "ok" ? "text-ok" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "text-faint";

  return (
    <Panel className="flex h-full flex-col justify-between">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5">
          <span className={toneClass}>{icon}</span>
          <span className="t-caption">{label}</span>
        </span>
        {help && (
          <Tooltip label={help}>
            <span
              tabIndex={0}
              className="grid h-3.5 w-3.5 cursor-help place-items-center rounded-full border border-line text-[10px] text-faint"
            >
              ?
            </span>
          </Tooltip>
        )}
      </div>

      <div className="mt-2.5">
        <div className="t-metric tnum">
          {literal !== undefined ? literal : state === "unavailable" ? "—" : formatPercent(kpi!.value)}
        </div>
        <p className="t-meta mt-1 text-[12px] leading-snug">
          {state === "unavailable" && kpi ? (
            // NOT "0%". There is no rate here to report.
            <span>No invoices in this period</span>
          ) : state === "insufficient" && kpi ? (
            <span className="text-warn">
              Only {kpi.denominator} invoice{kpi.denominator === 1 ? "" : "s"} — too few to read as
              a rate
            </span>
          ) : (
            caption
          )}
        </p>
      </div>
    </Panel>
  );
}

/**
 * One of the three headline decision counts.
 *
 * The share is shown beside the count rather than instead of it: "42" and
 * "79%" answer different questions, and a period with four invoices in it
 * makes the percentage the more misleading of the two on its own.
 */
function HeadlineCount({
  label,
  value,
  total,
  color,
}: {
  label: string;
  value: number;
  total: number;
  color: string;
}) {
  return (
    <div>
      <span className="flex items-center gap-1.5">
        <span
          aria-hidden
          className="h-2 w-2 shrink-0 rounded-full"
          style={{ background: color }}
        />
        <span className="t-caption">{label}</span>
      </span>
      <div className="t-metric tnum mt-1.5">{formatCount(value)}</div>
      <p className="t-meta text-[12px]">
        {/* Null, not "0%", when nothing was processed -- there is no share of
            nothing, the same rule every rate on this screen follows. */}
        {total > 0 ? `${formatPercent(value / total)} of ${formatCount(total)}` : "—"}
      </p>
    </div>
  );
}
