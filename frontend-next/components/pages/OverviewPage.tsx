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
import { amount, money, whenCompact } from "@/lib/format";
import {
  byDay,
  compactMoney,
  compactMoneyIsRounded,
  poUsage,
  topExceptionReasons,
  totals,
} from "@/lib/metrics";
import type { Reference, RunRecord } from "@/lib/types";
import type { Async } from "@/lib/useData";
import type { Navigate } from "@/components/layout/AppShell";
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
  Skeleton,
  StatusBadge,
  TD,
  TH,
  toneFor,
  Tooltip,
} from "@/components/ui";
import {
  IconCheck,
  IconInvoice,
  IconRefresh,
  IconUpload,
} from "@/components/ui/icons";
import { LegendItem, SERIES, Sparkline, VolumeChart } from "@/components/charts";
import ResetDemoButton from "@/components/ResetDemoButton";

/** The vivid end of each tone, for the thin status rule on a feed row. */
const TONE_VAR: Record<string, string> = {
  ok: "ok-vivid",
  warn: "warn-vivid",
  bad: "bad-vivid",
  accent: "accent",
  neutral: "line-strong",
};

/**
 * When a run was last acted on: its human ruling if one was recorded, its
 * arrival otherwise. Used to order the activity feed -- see `recent` below.
 *
 * An unparseable or absent timestamp reads as 0 rather than NaN, which would
 * make the comparator non-transitive and shuffle the feed unpredictably.
 */
function lastTouched(r: RunRecord): number {
  const t = (v?: string | null) => {
    const ms = v ? new Date(v).getTime() : NaN;
    return Number.isFinite(ms) ? ms : 0;
  };
  return Math.max(t(r.reviewed_at), t(r.created_at));
}

export default function OverviewPage({
  runs,
  reference,
  onNavigate,
}: {
  runs: Async<RunRecord[]>;
  reference: Async<Reference>;
  onNavigate: Navigate;
}) {
  const rows = useMemo(() => runs.data ?? [], [runs.data]);
  const t = useMemo(() => totals(rows), [rows]);
  const days = useMemo(() => byDay(rows), [rows]);
  const valueIsRounded = compactMoneyIsRounded(t.valueProcessed);
  const reasons = useMemo(() => topExceptionReasons(rows), [rows]);
  const pos = useMemo(
    () => poUsage(rows, reference.data?.purchase_orders ?? []),
    [rows, reference.data]
  );
  /**
   * "Recent activity" means the last things that HAPPENED, not the last things
   * that ARRIVED.
   *
   * This used to sort on `created_at` alone, which is when the invoice reached
   * the pipeline. So a reviewer who accepted or rejected a held invoice from
   * last week saw nothing change here: the ruling was recorded, the register
   * showed the new status, and this panel -- the one place on the landing
   * screen that claims to show what just happened -- did not move, because the
   * invoice's arrival time had not moved. The work looked lost.
   *
   * A run's last touch is its human ruling if it has one, and its arrival
   * otherwise. Both columns are written by the same `datetime.now(timezone.utc)
   * .isoformat()` call every timestamp in this application uses, so they are
   * directly comparable.
   */
  const recent = useMemo(
    () =>
      [...rows]
        .sort((a, b) => lastTouched(b) - lastTouched(a))
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
            {/* Disabled while a fetch is in flight. `useResource` already
                coalesces repeat presses into one queued refetch rather than a
                burst, so this is feedback rather than the guard -- but a
                button that looks pressable and visibly does nothing is what
                makes somebody press it five more times. */}
            <Button
              size="sm"
              onClick={runs.refresh}
              disabled={runs.loading}
              icon={<IconRefresh size={13} />}
            >
              {runs.loading ? "Refreshing…" : "Refresh"}
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
        {/* --------------------------------------------------------- value strip
            Processed volume, on its own.

            The four tiles that used to sit beside it -- the awaiting-review
            hero, "Straight through", "Average run time" and "Rejected
            outright" -- were removed at the owner's request. Nothing about
            what they measured was wrong; the outcome split they carried is
            still on the Volume chart directly below, the held count is still
            the Review queue's own badge in the sidebar, and every one of the
            four figures is reported in full on the Analytics screen. So this
            removes a repetition, not a measurement. */}
        {loading ? (
          <Skeleton className="h-[116px]" />
        ) : (
          /* The tile is shortened to fit, so it says so rather than
             presenting a rounded figure as the total: $17,991.00 drawn as
             "$18.0k" is off by nine dollars, and the only way to find that out
             was to add the invoices up by hand. The exact figure is in the
             tooltip beside it.

             The hint names the OTHER approximation too. This sum adds `total`
             across runs whatever currency each invoice is in (lib/metrics.ts
             `totals()`), so a USD invoice and an AUD one land in the same
             number -- a volume indicator, not a bookkeeping total. The
             Analytics screen does not repeat it; the server reports value in a
             bucket per currency. */
          <Stat
            label="Value processed"
            value={`${valueIsRounded ? "≈" : ""}${compactMoney(t.valueProcessed)}`}
            caption={`${money(t.valueApproved)} approved`}
            icon={<IconInvoice size={12} />}
            hint={`Shortened to fit. The exact figure is ${money(
              t.valueProcessed
            )}. It adds invoice totals together across currencies, so read it as processed volume rather than as a bookkeeping total.`}
            spark={days.map((d) => d.total)}
          />
        )}

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
                  const pct = (r.count / reasons[0].count) * 100;
                  return (
                    <li key={r.reason}>
                      <button
                        onClick={() => onNavigate("invoices")}
                        title={r.reason}
                        // The magnitude bar is drawn BEHIND the row, not under
                        // the label. A hairline beneath text reads as an
                        // underline, and a column of them made this ranked
                        // list look like a list of hyperlinks.
                        className="rank-row flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-hover"
                      >
                        <span
                          aria-hidden
                          className="rank-fill"
                          style={{
                            width: `${pct}%`,
                            background: blocking ? "var(--bad-vivid)" : "var(--warn-vivid)",
                          }}
                        />
                        <span className="tnum w-3 shrink-0 text-[12px] text-faint">{i + 1}</span>
                        <span className="min-w-0 flex-1 truncate text-[13.5px]">{r.reason}</span>
                        <span className="tnum shrink-0 text-[14px] font-semibold">{r.count}</span>
                        <Badge tone={blocking ? "bad" : "warn"} className="shrink-0">
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
              <DataTable minWidth={520}>
                <thead>
                  <tr>
                    <TH>PO</TH>
                    <TH>Vendor</TH>
                    <TH align="right">Consumed</TH>
                    <TH align="right">Remaining</TH>
                    <TH className="w-[132px]">Utilisation</TH>
                  </tr>
                </thead>
                <tbody>
                  {pos.map(({ po, consumed, remaining, pct, over }) => {
                    const tone = over ? "bad" : pct >= 99.5 ? "warn" : "ok";
                    return (
                      <tr key={po.po_number}>
                        <TD className="tnum text-[13.5px] font-medium">{po.po_number}</TD>
                        <TD className="max-w-[150px] truncate text-[13.5px] text-muted">
                          {po.vendor}
                        </TD>
                        <TD align="right" className="text-[13.5px] text-muted">
                          {money(consumed)}
                        </TD>
                        <TD align="right" className="text-[13.5px] font-semibold">
                          {money(remaining)}
                        </TD>
                        <TD>
                          <div className="flex items-center gap-2">
                            <Meter
                              value={consumed}
                              max={po.amount}
                              tone={tone}
                              height={4}
                              ariaLabel={`${pct.toFixed(0)}% consumed`}
                            />
                            <span className="tnum w-8 shrink-0 text-right text-[12px] text-faint">
                              {pct.toFixed(0)}%
                            </span>
                          </div>
                        </TD>
                      </tr>
                    );
                  })}
                </tbody>
              </DataTable>
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
                      title={r.filename}
                      className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-hover"
                    >
                      <span
                        aria-hidden
                        className="h-6 w-[3px] shrink-0 rounded-full"
                        style={{ background: `var(--${TONE_VAR[toneFor(r.status)]})` }}
                      />
                      <span className="min-w-0 flex-1">
                        {/* Vendor and invoice number are how an AP clerk names
                            an invoice. The upload FILENAME is an artefact of
                            how it arrived — kept as the row's tooltip, not as
                            its headline. */}
                        <span className="block truncate text-[13.5px] font-medium">
                          {r.vendor_name || "Unknown vendor"}
                        </span>
                        <span className="t-meta tnum block truncate text-[12px]">
                          {r.invoice_number || "no invoice number"}
                          {r.po_number ? ` · ${r.po_number}` : ""}
                        </span>
                      </span>
                      <span className="tnum shrink-0 text-[13.5px] font-semibold">
                        {amount(r.total, r.audit?.invoice?.currency || "USD")}
                      </span>
                      <StatusBadge status={r.status} />
                      {/* The time this row is ORDERED by, so the clock agrees
                          with the position -- a ruling recorded at 14:02 that
                          sat at the top showing last week's arrival time read
                          as a sorting bug. Wide enough for a 12-hour clock
                          ("12:42 AM"): at w-11 it wrapped onto two lines in any
                          locale that formats time that way, which pushed every
                          feed row out of alignment. */}
                      <span className="tnum t-meta w-16 shrink-0 text-right text-[12px] whitespace-nowrap">
                        {whenCompact(r.reviewed_at || r.created_at)}
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

/** One card of the reporting strip. */
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
  tone?: "ok" | "bad";
  hint?: string;
  spark?: number[];
  sparkTone?: string;
}) {
  return (
    <Panel className="flex h-full flex-col justify-between">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5">
          <span className={tone === "ok" ? "text-ok" : tone === "bad" ? "text-bad" : "text-faint"}>
            {icon}
          </span>
          <span className="t-caption">{label}</span>
        </span>
        {hint && (
          <Tooltip label={hint}>
            <span
              tabIndex={0}
              className="grid h-3.5 w-3.5 cursor-help place-items-center rounded-full border border-line text-[10px] text-faint"
            >
              ?
            </span>
          </Tooltip>
        )}
      </div>

      <div className="mt-2.5 flex items-end justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="t-metric tnum">{value}</div>
          <p className="t-meta mt-1 text-[12px] leading-snug">{caption}</p>
        </div>
        {/* The sparkline used to be hidden below 2xl: with four cards across
            a laptop viewport the cell narrowed until the line overlapped the
            caption beside it. The strip is one full-width card now, so there
            is room for it at every width, and it can be drawn wider. */}
        {spark && spark.some((v) => v > 0) && (
          <div className="hidden shrink-0 sm:block">
            <Sparkline values={spark} tone={sparkTone ?? "var(--accent)"} width={96} />
          </div>
        )}
      </div>
    </Panel>
  );
}
