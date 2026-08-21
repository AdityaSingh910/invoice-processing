"use client";

/**
 * Purchase orders and approved vendors — the reference data the rules check
 * against. Read-only, and labelled as such: this is not somewhere an operator
 * edits the ledger.
 *
 * Consumption is joined in from run history so a PO's remaining balance sits
 * beside its limit, derived exactly as the backend derives it: only APPROVED
 * runs consume budget.
 */
import { useEffect, useMemo, useState } from "react";
import { money } from "@/lib/format";
import { poUsage } from "@/lib/metrics";
import type { Reference, RunRecord } from "@/lib/types";
import type { Async } from "@/lib/useData";
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
  SearchInput,
  Segmented,
  SkeletonRows,
  StatusBadge,
  TD,
  TH,
} from "@/components/ui";
import { IconLedger, IconShield } from "@/components/ui/icons";

type Tab = "orders" | "vendors";

export default function ReferencePage({
  reference,
  runs,
  initialTab,
}: {
  reference: Async<Reference>;
  runs: Async<RunRecord[]>;
  /** Set once, from how the page was navigated to — the sidebar's "Approved
   *  vendors" entry lands here with that tab preselected rather than making
   *  the user reselect it. */
  initialTab?: Tab;
}) {
  const [tab, setTab] = useState<Tab>(initialTab ?? "orders");
  useEffect(() => {
    if (initialTab) setTab(initialTab);
  }, [initialTab]);
  const [query, setQuery] = useState("");

  const usage = useMemo(
    () => poUsage(runs.data ?? [], reference.data?.purchase_orders ?? []),
    [runs.data, reference.data]
  );

  const q = query.trim().toLowerCase();
  const pos = usage.filter(
    (u) => !q || u.po.po_number.toLowerCase().includes(q) || u.po.vendor.toLowerCase().includes(q)
  );
  const vendors = (reference.data?.vendors ?? []).filter(
    (v) => !q || v.vendor_name.toLowerCase().includes(q) || v.vendor_id.toLowerCase().includes(q)
  );

  const loading = reference.loading && !reference.data;

  // Committed = what approved invoices have already drawn against every PO.
  const committed = usage.reduce((a, u) => a + u.consumed, 0);
  const authorised = usage.reduce((a, u) => a + u.po.amount, 0);

  return (
    <>
      <PageHeader
        title="Purchase orders"
        description="Reference data every decision is checked against. Read-only."
        actions={
          <SearchInput
            className="w-full sm:w-60"
            placeholder="Search POs and vendors…"
            value={query}
            onChange={(e) => setQuery(e.currentTarget.value)}
            aria-label="Search reference data"
          />
        }
      />

      <PageBody>
        {reference.error ? (
          <Panel>
            <ErrorState description={reference.error} onRetry={reference.refresh} />
          </Panel>
        ) : (
          <>
            {/* A single summary strip rather than a row of tiles: three related
                figures about one ledger belong together. */}
            {!loading && (
              <Panel flush>
                <div className="grid grid-cols-1 divide-line sm:grid-cols-3 sm:divide-x sm:divide-y-0">
                  <Summary
                    label="Total authorised"
                    value={money(authorised)}
                    caption={`Across ${usage.length} purchase orders`}
                  />
                  <Summary
                    label="Committed"
                    value={money(committed)}
                    caption={
                      authorised > 0
                        ? `${((committed / authorised) * 100).toFixed(0)}% of budget`
                        : undefined
                    }
                  />
                  <Summary
                    label="Available"
                    value={money(authorised - committed)}
                    caption="Still open to invoice against"
                    tone="ok"
                  />
                </div>
              </Panel>
            )}

            <Segmented<Tab>
              ariaLabel="Reference data"
              value={tab}
              onChange={setTab}
              options={[
                { value: "orders", label: "Purchase orders", count: pos.length },
                { value: "vendors", label: "Approved vendors", count: vendors.length },
              ]}
            />

            {tab === "orders" ? (
              <Panel flush>
                <PanelHeader
                  bordered
                  title="Purchase order ledger"
                  description="Remaining balance reflects approved invoices only"
                />
                {loading ? (
                  <SkeletonRows rows={5} cols={5} />
                ) : pos.length === 0 ? (
                  <EmptyState
                    icon={<IconLedger size={16} />}
                    title="No purchase orders match"
                    description="Try a different search term."
                    action={
                      query && (
                        <Button size="sm" onClick={() => setQuery("")}>
                          Clear search
                        </Button>
                      )
                    }
                  />
                ) : (
                  <DataTable minWidth={800}>
                    <thead>
                      <tr>
                        <TH>PO number</TH>
                        <TH>Vendor</TH>
                        <TH align="right">Authorised</TH>
                        <TH align="right">Consumed</TH>
                        <TH align="right">Remaining</TH>
                        <TH className="w-[150px]">Utilisation</TH>
                        <TH>Status</TH>
                      </tr>
                    </thead>
                    <tbody>
                      {pos.map(({ po, consumed, remaining, pct, over }) => {
                        const tone = over ? "bad" : pct >= 99.5 ? "warn" : "ok";
                        return (
                          <tr key={po.po_number}>
                            <TD className="tnum text-[12.5px] font-medium">{po.po_number}</TD>
                            <TD className="text-[12.5px]">{po.vendor}</TD>
                            <TD align="right" className="text-[12.5px] text-muted">
                              {money(po.amount)}
                            </TD>
                            <TD align="right" className="text-[12.5px] text-muted">
                              {money(consumed)}
                            </TD>
                            <TD align="right" className="text-[12.5px] font-semibold">
                              {money(remaining)}
                            </TD>
                            <TD>
                              <div className="flex items-center gap-2">
                                <Meter
                                  value={consumed}
                                  max={po.amount}
                                  tone={tone}
                                  height={4}
                                  ariaLabel={`${pct.toFixed(0)} percent consumed`}
                                />
                                <span className="tnum w-8 shrink-0 text-right text-[11px] text-faint">
                                  {pct.toFixed(0)}%
                                </span>
                              </div>
                            </TD>
                            <TD>
                              {/* One badge, not two. "open" and "exhausted"
                                  side by side describe different things (the
                                  PO's own status vs. what invoices have drawn
                                  against it) and read as contradictory. The
                                  drawn-down state is the more actionable of
                                  the two, so it wins the badge and the PO's
                                  own status becomes its tooltip. */}
                              {over ? (
                                <Badge tone="bad" dot title={`PO status: ${po.status}`}>
                                  over-consumed
                                </Badge>
                              ) : pct >= 99.5 ? (
                                <Badge tone="warn" dot title={`PO status: ${po.status}`}>
                                  exhausted
                                </Badge>
                              ) : (
                                <StatusBadge status={po.status} />
                              )}
                            </TD>
                          </tr>
                        );
                      })}
                    </tbody>
                  </DataTable>
                )}
              </Panel>
            ) : (
              <Panel flush>
                <PanelHeader
                  bordered
                  title="Approved vendors"
                  description="An invoice from a vendor on file but not approved is rejected outright"
                />
                {loading ? (
                  <SkeletonRows rows={5} cols={3} />
                ) : vendors.length === 0 ? (
                  <EmptyState
                    icon={<IconShield size={16} />}
                    title="No vendors match"
                    description="Try a different search term."
                  />
                ) : (
                  <DataTable minWidth={420}>
                    <thead>
                      <tr>
                        <TH>Vendor</TH>
                        <TH>Vendor ID</TH>
                        <TH>Status</TH>
                      </tr>
                    </thead>
                    <tbody>
                      {vendors.map((v) => (
                        <tr key={v.vendor_id}>
                          <TD className="text-[12.5px] font-medium">{v.vendor_name}</TD>
                          <TD className="tnum text-[12px] text-muted">{v.vendor_id}</TD>
                          <TD>
                            <Badge tone={v.status === "approved" ? "ok" : "neutral"} dot>
                              {v.status}
                            </Badge>
                          </TD>
                        </tr>
                      ))}
                    </tbody>
                  </DataTable>
                )}
              </Panel>
            )}
          </>
        )}
      </PageBody>
    </>
  );
}

/**
 * One figure in the ledger summary strip.
 *
 * Set at `t-metric-sm` rather than `t-metric`: three six-figure amounts at the
 * larger size filled the strip edge to edge and made the reference screen shout
 * louder than the dashboard, which is the screen that actually has news on it.
 */
function Summary({
  label,
  value,
  caption,
  tone,
}: {
  label: string;
  value: string;
  caption?: string;
  tone?: "ok";
}) {
  return (
    <div className="p-4">
      <p className="t-caption">{label}</p>
      <p
        className="t-metric-sm tnum mt-1.5"
        style={tone === "ok" ? { color: "var(--ok)" } : undefined}
      >
        {value}
      </p>
      <p className="t-meta mt-1 text-[11px]">{caption ?? "\u00A0"}</p>
    </div>
  );
}
