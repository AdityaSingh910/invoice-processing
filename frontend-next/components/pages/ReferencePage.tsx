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
import { useMemo, useState } from "react";
import { money } from "@/lib/format";
import { poUsage } from "@/lib/metrics";
import type { Reference, RunRecord } from "@/lib/types";
import type { Async } from "@/lib/useData";
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Meter,
  Panel,
  PanelHeader,
  SearchInput,
  Segmented,
  SkeletonRows,
  StatusBadge,
} from "@/components/ui";
import { IconLedger, IconShield } from "@/components/ui/icons";

type Tab = "orders" | "vendors";

export default function ReferencePage({
  reference,
  runs,
}: {
  reference: Async<Reference>;
  runs: Async<RunRecord[]>;
}) {
  const [tab, setTab] = useState<Tab>("orders");
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
                  <Summary label="Total authorised" value={money(authorised)} />
                  <Summary
                    label="Committed"
                    value={money(committed)}
                    caption={
                      authorised > 0
                        ? `${((committed / authorised) * 100).toFixed(0)}% of budget`
                        : undefined
                    }
                  />
                  <Summary label="Available" value={money(authorised - committed)} />
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
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[760px] border-collapse">
                      <thead>
                        <tr>
                          <Th>PO number</Th>
                          <Th>Vendor</Th>
                          <Th align="right">Authorised</Th>
                          <Th align="right">Consumed</Th>
                          <Th align="right">Remaining</Th>
                          <Th className="w-[150px]">Utilisation</Th>
                          <Th>Status</Th>
                        </tr>
                      </thead>
                      <tbody className="divide-line">
                        {pos.map(({ po, consumed, remaining, pct, over }) => {
                          const tone = over ? "bad" : pct >= 99.5 ? "warn" : "ok";
                          return (
                            <tr key={po.po_number} className="transition-colors hover:bg-hover">
                              <td className="tnum px-3 py-2 text-[12.5px] font-medium">
                                {po.po_number}
                              </td>
                              <td className="px-3 py-2 text-[12.5px] text-muted">{po.vendor}</td>
                              <td className="tnum px-3 py-2 text-right text-[12.5px]">
                                {money(po.amount)}
                              </td>
                              <td className="tnum px-3 py-2 text-right text-[12.5px]">
                                {money(consumed)}
                              </td>
                              <td className="tnum px-3 py-2 text-right text-[12.5px] font-semibold">
                                {money(remaining)}
                              </td>
                              <td className="px-3 py-2">
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
                              </td>
                              <td className="px-3 py-2">
                                <div className="flex items-center gap-1.5">
                                  <StatusBadge status={po.status} />
                                  {over && <Badge tone="bad">over</Badge>}
                                  {!over && pct >= 99.5 && <Badge tone="warn">exhausted</Badge>}
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
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[420px] border-collapse">
                      <thead>
                        <tr>
                          <Th>Vendor</Th>
                          <Th>Vendor ID</Th>
                          <Th>Status</Th>
                        </tr>
                      </thead>
                      <tbody className="divide-line">
                        {vendors.map((v) => (
                          <tr key={v.vendor_id} className="transition-colors hover:bg-hover">
                            <td className="px-3 py-2 text-[12.5px] font-medium">{v.vendor_name}</td>
                            <td className="tnum px-3 py-2 text-[12px] text-muted">{v.vendor_id}</td>
                            <td className="px-3 py-2">
                              <Badge tone={v.status === "approved" ? "ok" : "neutral"} dot>
                                {v.status}
                              </Badge>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>
            )}
          </>
        )}
      </PageBody>
    </>
  );
}

function Th({
  children,
  align = "left",
  className = "",
}: {
  children: React.ReactNode;
  align?: "left" | "right";
  className?: string;
}) {
  return (
    <th
      scope="col"
      className={`t-caption border-b border-line px-3 py-2 ${
        align === "right" ? "text-right" : "text-left"
      } ${className}`}
    >
      {children}
    </th>
  );
}

function Summary({
  label,
  value,
  caption,
}: {
  label: string;
  value: string;
  caption?: string;
}) {
  return (
    <div className="p-4">
      <p className="t-caption">{label}</p>
      <p className="t-metric tnum mt-2">{value}</p>
      {caption && <p className="t-meta mt-1 text-[11px]">{caption}</p>}
    </div>
  );
}
