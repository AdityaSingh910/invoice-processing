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
 * Sections follow the questions an AP lead actually asks, in order: how much
 * is automated, how is it trending, where is the time going, what is happening
 * in review, who and what is driving the exceptions, and — where email
 * ingestion is switched on — whether it is delivering anything.
 */
import { useState } from "react";
import { amount } from "@/lib/format";
import {
  bucketsToDays,
  formatCount,
  formatDuration,
  formatPercent,
  formatSeconds,
  kpiState,
  MIN_MEANINGFUL_SAMPLE,
} from "@/lib/metrics";
import type {
  AnalyticsEmail,
  AnalyticsDashboard,
  AnalyticsOverview,
  AnalyticsProcessing,
  AnalyticsReviews,
  AnalyticsTrends,
  AnalyticsUsers,
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
  IconShield,
  IconUser,
} from "@/components/ui/icons";
import { LegendItem, RateTrend, SERIES, SplitBar, VolumeChart } from "@/components/charts";

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
  const users = section<AnalyticsUsers>((d) => d.users);
  const email = section<AnalyticsEmail>((d) => d.email);

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
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <KpiCell
              label="Automation rate"
              kpi={o.kpis.automation_rate}
              caption={`${formatCount(o.volume.automated)} of ${formatCount(o.volume.runs)} decided by rules`}
              icon={<IconCheck size={12} />}
              tone="ok"
            />
            <KpiCell
              label="Task success"
              kpi={o.kpis.task_success_ratio}
              caption={`${formatCount(o.volume.overridden)} overridden`}
              icon={<IconShield size={12} />}
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

        {/* ------------------------------------------------- volume + decisions */}
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
          <Panel>
            <PanelHeader
              title="Volume"
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

          <Panel>
            <PanelHeader
              title="Decision mix"
              description="What the rules said, what people said, what the ledger reads"
            />
            {loading || !o ? (
              <Skeleton className="mt-4 h-[132px]" />
            ) : o.volume.runs === 0 ? (
              <EmptyState
                compact
                icon={<IconAnalytics size={16} />}
                title="No invoices in this period"
                description="Choose a wider range, or process an invoice."
              />
            ) : (
              <div className="mt-4 flex flex-col gap-4">
                <MixRow
                  title="Automated"
                  hint="What the deterministic rules concluded. Never rewritten by a later ruling."
                  total={o.volume.runs}
                  segments={[
                    { label: "Approved", value: o.decisions.automated.APPROVED, color: SERIES.approved },
                    { label: "Held", value: o.decisions.automated.NEEDS_REVIEW, color: SERIES.needsReview },
                    { label: "Rejected", value: o.decisions.automated.REJECTED, color: SERIES.rejected },
                  ]}
                />
                <MixRow
                  title="Human rulings"
                  hint="What reviewers decided about the invoices they were handed."
                  total={o.volume.runs}
                  segments={[
                    { label: "Accepted", value: o.decisions.human.ACCEPTED, color: SERIES.approved },
                    { label: "Rejected", value: o.decisions.human.REJECTED, color: SERIES.rejected },
                    { label: "Not reviewed", value: o.decisions.human.not_reviewed, color: "var(--line-strong)" },
                  ]}
                />
                <MixRow
                  title="Ledger status"
                  hint="What the PO ledger currently reads. Differs from the automated row exactly where a person moved a run."
                  total={o.volume.runs}
                  segments={[
                    { label: "Approved", value: o.decisions.status.APPROVED, color: SERIES.approved },
                    { label: "Held", value: o.decisions.status.NEEDS_REVIEW, color: SERIES.needsReview },
                    { label: "Rejected", value: o.decisions.status.REJECTED, color: SERIES.rejected },
                  ]}
                />
              </div>
            )}
          </Panel>
        </div>

        {/* ------------------------------------------------------ rate + stages */}
        <div className="grid items-start gap-4 lg:grid-cols-2">
          <Panel>
            <PanelHeader
              title="Automation over time"
              description="Days with no invoices are gaps, not zeroes"
            />
            <div className="mt-4">
              {trends.error ? (
                <div className="p-4">
                  <ErrorState description={trends.error} onRetry={refresh} />
                </div>
              ) : trends.loading && !trends.data ? (
                <Skeleton className="h-[132px] w-full" />
              ) : (
                <RateTrend
                  label="Automation rate"
                  tone="var(--ok-vivid)"
                  points={(trends.data?.buckets ?? []).map((b) => ({
                    day: b.day,
                    label: new Date(`${b.day}T00:00:00Z`).toLocaleDateString(undefined, {
                      month: "short",
                      day: "numeric",
                      timeZone: "UTC",
                    }),
                    value: b.automation_rate,
                  }))}
                />
              )}
            </div>
          </Panel>

          <Panel flush>
            <PanelHeader
              bordered
              title="Where the time goes"
              description="Per stage, slowest first — this is the bottleneck view"
            />
            {processing.error ? (
              <div className="p-4">
                <ErrorState description={processing.error} onRetry={refresh} />
              </div>
            ) : processing.loading && !processing.data ? (
              <div className="p-4">
                <Skeleton className="h-[132px]" />
              </div>
            ) : !processing.data?.stages.length ? (
              <EmptyState
                compact
                icon={<IconClock size={16} />}
                title="No stage timings yet"
                description="The pipeline records a duration for every stage it runs."
              />
            ) : (
              <DataTable minWidth={420}>
                <thead>
                  <tr>
                    <TH>Stage</TH>
                    <TH align="right">Mean</TH>
                    <TH align="right">Median</TH>
                    <TH align="right">p95</TH>
                    <TH>Share</TH>
                  </tr>
                </thead>
                <tbody>
                  {processing.data.stages.map((s) => (
                    <tr key={s.stage}>
                      <TD>
                        <span className="font-medium">{s.stage}</span>
                        <span className="t-meta block text-[12px]">
                          {formatCount(s.runs)} runs
                        </span>
                      </TD>
                      <TD align="right">{formatDuration(s.average)}</TD>
                      <TD align="right">{formatDuration(s.median)}</TD>
                      <TD align="right">{formatDuration(s.p95)}</TD>
                      <TD className="w-[110px]">
                        <div className="flex items-center gap-2">
                          <Meter
                            value={s.share_of_time ?? 0}
                            max={1}
                            tone="accent"
                            ariaLabel={`${s.stage} share of processing time`}
                          />
                          <span className="tnum t-meta w-8 shrink-0 text-right text-[12px]">
                            {formatPercent(s.share_of_time)}
                          </span>
                        </div>
                      </TD>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
          </Panel>
        </div>

        {/* ------------------------------------------------------------ review */}
        <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)]">
          <Panel>
            <PanelHeader
              title="Review funnel"
              description="From held, to ruled on, to the decision reached"
            />
            {reviews.error ? (
              <div className="p-4">
                <ErrorState description={reviews.error} onRetry={refresh} />
              </div>
            ) : reviews.loading && !reviews.data ? (
              <Skeleton className="mt-4 h-[180px]" />
            ) : !reviews.data ? null : reviews.data.funnel.held_for_review === 0 ? (
              <EmptyState
                compact
                icon={<IconCheck size={16} />}
                title="Nothing was held in this period"
                description="Every invoice cleared the rules on its own."
              />
            ) : (
              <div className="mt-4 flex flex-col gap-3">
                <FunnelRow
                  label="Held for review"
                  value={reviews.data.funnel.held_for_review}
                  of={reviews.data.funnel.runs}
                  tone="warn"
                />
                <FunnelRow
                  label="Ruled on"
                  value={reviews.data.funnel.ruled_on}
                  of={reviews.data.funnel.held_for_review}
                  tone="accent"
                />
                <FunnelRow
                  label="Accepted"
                  value={reviews.data.funnel.accepted}
                  of={reviews.data.funnel.ruled_on}
                  tone="ok"
                />
                <FunnelRow
                  label="Rejected"
                  value={reviews.data.funnel.rejected}
                  of={reviews.data.funnel.ruled_on}
                  tone="bad"
                />
                <FunnelRow
                  label="Still awaiting"
                  value={reviews.data.funnel.still_awaiting}
                  of={reviews.data.funnel.held_for_review}
                  tone="neutral"
                />

                <div className="mt-1 grid grid-cols-2 gap-3 border-t border-line pt-3">
                  <Latency
                    label="Time to decision"
                    block={reviews.data.latency.time_to_decision}
                  />
                  <Latency label="Handling time" block={reviews.data.latency.handling_time} />
                </div>
              </div>
            )}
          </Panel>

          <Panel flush>
            <PanelHeader
              bordered
              title="Why invoices stop"
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
        </div>

        {/* ------------------------------------------------------ vendors + POs */}
        <div className="grid items-start gap-4 xl:grid-cols-2">
          <Panel flush>
            <PanelHeader
              bordered
              title="Vendor performance"
              description="Invoice behaviour by vendor, in this period"
            />
            {vendors.error ? (
              <div className="p-4">
                <ErrorState description={vendors.error} onRetry={refresh} />
              </div>
            ) : vendors.loading && !vendors.data ? (
              <div className="p-4">
                <Skeleton className="h-[180px]" />
              </div>
            ) : !vendors.data?.vendors.length ? (
              <EmptyState
                compact
                icon={<IconInvoice size={16} />}
                title="No invoices in this period"
              />
            ) : (
              <DataTable minWidth={520}>
                <thead>
                  <tr>
                    <TH>Vendor</TH>
                    <TH align="right">Invoices</TH>
                    <TH align="right">Approved</TH>
                    <TH align="right">Held</TH>
                    <TH align="right">Rejected</TH>
                    <TH align="right">Avg time</TH>
                  </tr>
                </thead>
                <tbody>
                  {vendors.data.vendors.map((v) => (
                    <tr key={v.vendor}>
                      <TD className="max-w-[180px] truncate font-medium">{v.vendor}</TD>
                      <TD align="right">{formatCount(v.runs)}</TD>
                      <TD align="right">{formatPercent(v.approval_rate)}</TD>
                      <TD align="right">{formatPercent(v.hold_rate)}</TD>
                      <TD align="right">{formatPercent(v.rejection_rate)}</TD>
                      <TD align="right">{formatDuration(v.avg_processing_ms)}</TD>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
          </Panel>

          <Panel flush>
            <PanelHeader
              bordered
              title="Purchase order budgets"
              description="Balances are the ledger's own, all-time; the counts are this period"
            />
            {vendors.error ? (
              <div className="p-4">
                <ErrorState description={vendors.error} onRetry={refresh} />
              </div>
            ) : vendors.loading && !vendors.data ? (
              <div className="p-4">
                <Skeleton className="h-[180px]" />
              </div>
            ) : !vendors.data?.purchase_orders.length ? (
              <EmptyState compact icon={<IconInvoice size={16} />} title="No purchase orders on file" />
            ) : (
              <DataTable minWidth={520}>
                <thead>
                  <tr>
                    <TH>PO</TH>
                    <TH align="right">Budget</TH>
                    <TH align="right">Consumed</TH>
                    <TH align="right">Remaining</TH>
                    <TH>Used</TH>
                    <TH align="right">Invoices</TH>
                  </tr>
                </thead>
                <tbody>
                  {vendors.data.purchase_orders.slice(0, 10).map((p) => (
                    <tr key={p.po_number}>
                      <TD>
                        <span className="tnum font-medium">{p.po_number}</span>
                        <span className="t-meta block max-w-[150px] truncate text-[12px]">
                          {p.vendor}
                        </span>
                      </TD>
                      <TD align="right">{amount(p.amount, p.currency)}</TD>
                      <TD align="right">{amount(p.consumed, p.currency)}</TD>
                      <TD align="right">
                        <span className={p.over_budget ? "text-bad" : undefined}>
                          {amount(p.remaining, p.currency)}
                        </span>
                      </TD>
                      <TD className="w-[96px]">
                        <div className="flex items-center gap-2">
                          <Meter
                            value={p.consumed}
                            max={p.amount}
                            tone={p.over_budget ? "bad" : p.utilisation && p.utilisation > 0.8 ? "warn" : "accent"}
                            ariaLabel={`${p.po_number} budget used`}
                          />
                          <span className="tnum t-meta w-8 shrink-0 text-right text-[12px]">
                            {formatPercent(p.utilisation)}
                          </span>
                        </div>
                      </TD>
                      <TD align="right">{formatCount(p.runs_in_range)}</TD>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
          </Panel>
        </div>

        {/* ------------------------------------------------------- people + mail */}
        <div className="grid items-start gap-4 xl:grid-cols-2">
          <Panel flush>
            <PanelHeader
              bordered
              title="Review workload"
              description={
                users.data?.scope === "all"
                  ? "Every reviewer, in this period"
                  : "Your own activity in this period"
              }
              actions={
                users.data && (
                  <Badge tone={users.data.scope === "all" ? "accent" : "neutral"}>
                    {users.data.scope === "all" ? "Team" : "You"}
                  </Badge>
                )
              }
            />
            {users.error ? (
              <div className="p-4">
                <ErrorState description={users.error} onRetry={refresh} />
              </div>
            ) : users.loading && !users.data ? (
              <div className="p-4">
                <Skeleton className="h-[150px]" />
              </div>
            ) : !users.data?.users.length ? (
              <EmptyState
                compact
                icon={<IconUser size={16} />}
                title="No review activity in this period"
                description={users.data?.note ?? undefined}
              />
            ) : (
              <>
                <DataTable minWidth={460}>
                  <thead>
                    <tr>
                      <TH>Reviewer</TH>
                      <TH align="right">Ruled on</TH>
                      <TH align="right">Accepted</TH>
                      <TH align="right">Rejected</TH>
                      <TH align="right">Median time</TH>
                      <TH align="right">Holding</TH>
                    </tr>
                  </thead>
                  <tbody>
                    {users.data.users.map((u) => (
                      <tr key={u.username}>
                        <TD className="max-w-[150px] truncate font-medium">{u.username}</TD>
                        <TD align="right">{formatCount(u.reviews)}</TD>
                        <TD align="right">{formatCount(u.accepted)}</TD>
                        <TD align="right">{formatCount(u.rejected)}</TD>
                        <TD align="right">
                          {formatSeconds(u.median_time_to_decision_seconds)}
                        </TD>
                        <TD align="right">
                          {u.claims_held_now > 0 ? (
                            <Badge tone="warn">{u.claims_held_now}</Badge>
                          ) : (
                            <span className="t-meta">—</span>
                          )}
                        </TD>
                      </tr>
                    ))}
                  </tbody>
                </DataTable>
                {/* The server decides this from the token, so the note is a
                    statement of what was returned, not a UI-side restriction. */}
                {users.data.note && (
                  <p className="t-meta border-t border-line px-4 py-2.5 text-[12px]">
                    {users.data.note}
                  </p>
                )}
              </>
            )}
          </Panel>

          <Panel>
            <PanelHeader
              title="Email ingestion"
              description="What arrived, what was filtered, what became an invoice"
            />
            {email.error ? (
              <div className="p-4">
                <ErrorState description={email.error} onRetry={refresh} />
              </div>
            ) : email.loading && !email.data ? (
              <Skeleton className="mt-4 h-[150px]" />
            ) : !email.data || email.data.funnel.received === 0 ? (
              <EmptyState
                compact
                icon={<IconShield size={16} />}
                title="No email received in this period"
                description="Ingestion is off by default; nothing polls a mailbox unless it is switched on."
              />
            ) : (
              <div className="mt-4 flex flex-col gap-3">
                <FunnelRow
                  label="Received"
                  value={email.data.funnel.received}
                  of={email.data.funnel.received}
                  tone="accent"
                />
                <FunnelRow
                  label="Judged relevant"
                  value={email.data.funnel.relevant}
                  of={email.data.funnel.received}
                  tone="accent"
                />
                <FunnelRow
                  label="Passed verification"
                  value={email.data.funnel.admitted}
                  of={email.data.funnel.received}
                  tone="ok"
                />
                <FunnelRow
                  label="Quarantined"
                  value={email.data.funnel.quarantined}
                  of={email.data.funnel.received}
                  tone="warn"
                />
                <FunnelRow
                  label="Invoice runs created"
                  value={email.data.funnel.runs_created}
                  of={email.data.funnel.attachments || email.data.funnel.received}
                  tone="ok"
                />
                <p className="t-meta text-[12px]">
                  One email can carry several invoices, so runs are counted from
                  attachments rather than from messages.
                </p>
              </div>
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

/** A labelled proportion bar with its own counts beneath. */
function MixRow({
  title,
  hint,
  total,
  segments,
}: {
  title: string;
  hint: string;
  total: number;
  segments: { label: string; value: number; color: string }[];
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <Tooltip label={hint}>
          <span tabIndex={0} className="t-caption cursor-help">
            {title}
          </span>
        </Tooltip>
        <span className="flex flex-wrap gap-2.5">
          {segments
            .filter((s) => s.value > 0)
            .map((s) => (
              <LegendItem key={s.label} color={s.color} label={s.label} value={s.value} />
            ))}
        </span>
      </div>
      <SplitBar segments={segments} total={total} ariaLabel={title} />
    </div>
  );
}

/** One step of a funnel: the count, its share of the step above, and a bar. */
function FunnelRow({
  label,
  value,
  of,
  tone,
}: {
  label: string;
  value: number;
  of: number;
  tone: "ok" | "warn" | "bad" | "accent" | "neutral";
}) {
  return (
    <div className="flex items-center gap-3">
      <span className="w-[132px] shrink-0 text-[13.5px]">{label}</span>
      <span className="min-w-0 flex-1">
        <Meter value={value} max={of || 1} tone={tone} ariaLabel={`${label}: ${value} of ${of}`} />
      </span>
      <span className="tnum w-10 shrink-0 text-right text-[13.5px] font-semibold">
        {formatCount(value)}
      </span>
      <span className="tnum t-meta w-9 shrink-0 text-right text-[12px]">
        {/* Null, not "0%", when the step above was empty — there is no share
            of nothing. */}
        {of > 0 ? formatPercent(value / of) : "—"}
      </span>
    </div>
  );
}

/** A latency block, with its sample count always visible. */
function Latency({
  label,
  block,
}: {
  label: string;
  block: { samples: number; median_seconds: number | null; definition: string };
}) {
  return (
    <div>
      <Tooltip label={block.definition}>
        <span tabIndex={0} className="t-caption cursor-help">
          {label}
        </span>
      </Tooltip>
      <div className="tnum mt-1 text-[16px] font-semibold">
        {formatSeconds(block.median_seconds)}
      </div>
      <p className="t-meta text-[12px]">
        {block.samples === 0
          ? "nothing measured"
          : `median of ${formatCount(block.samples)}${
              block.samples < MIN_MEANINGFUL_SAMPLE ? " — a small sample" : ""
            }`}
      </p>
    </div>
  );
}
