"use client";

/**
 * Reference data: the purchase-order ledger and the approved-vendor list.
 *
 * Read-only, and labelled as such — this is what the rules consult, not
 * something an operator edits here. Consumption is joined in from run history
 * so a PO's remaining balance is visible in the same place as its limit.
 */
import { useMemo, useState } from "react";
import { money } from "@/lib/format";
import { poUsage } from "@/lib/metrics";
import type { Reference, RunRecord } from "@/lib/types";
import type { Async } from "@/lib/useData";
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  SearchInput,
  SkeletonRows,
  StatusBadge,
  TD,
  TH,
} from "@/components/ui";
import { IconLedger, IconShield } from "@/components/ui/icons";
import { MeterBar } from "@/components/charts";

export default function ReferencePage({
  reference,
  runs,
}: {
  reference: Async<Reference>;
  runs: Async<RunRecord[]>;
}) {
  const [query, setQuery] = useState("");

  const usage = useMemo(
    () => poUsage(runs.data ?? [], reference.data?.purchase_orders ?? []),
    [runs.data, reference.data]
  );

  const q = query.trim().toLowerCase();
  const pos = usage.filter(
    (u) =>
      !q ||
      u.po.po_number.toLowerCase().includes(q) ||
      u.po.vendor.toLowerCase().includes(q)
  );
  const vendors = (reference.data?.vendors ?? []).filter(
    (v) => !q || v.vendor_name.toLowerCase().includes(q) || v.vendor_id.toLowerCase().includes(q)
  );

  const loading = reference.loading && !reference.data;

  return (
    <PageBody>
      <PageHeader
        title="Purchase orders"
        description="The reference data every decision is checked against. Read-only."
        actions={
          <SearchInput
            className="w-full sm:w-64"
            placeholder="Search POs and vendors…"
            value={query}
            onChange={(e) => setQuery(e.currentTarget.value)}
            aria-label="Search reference data"
          />
        }
      />

      {reference.error && (
        <Card>
          <ErrorState description={reference.error} onRetry={reference.refresh} />
        </Card>
      )}

      {!reference.error && (
        <>
          <Card padded={false}>
            <div className="p-4 sm:p-5">
              <CardHeader
                title="Purchase order ledger"
                description="Remaining balance reflects approved invoices only."
              />
            </div>

            {loading ? (
              <div className="px-4 pb-4 sm:px-5">
                <SkeletonRows rows={5} cols={4} />
              </div>
            ) : pos.length === 0 ? (
              <EmptyState
                icon={<IconLedger size={18} />}
                title="No purchase orders match"
                description="Try a different search term."
              />
            ) : (
              <div className="overflow-x-auto border-t border-border">
                <table className="w-full min-w-[720px] border-collapse text-[13px]">
                  <thead>
                    <tr className="border-b border-border">
                      <TH>PO number</TH>
                      <TH>Vendor</TH>
                      <TH align="right">Authorised</TH>
                      <TH align="right">Consumed</TH>
                      <TH align="right">Remaining</TH>
                      <TH className="w-[160px]">Utilisation</TH>
                      <TH>Status</TH>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {pos.map(({ po, consumed, remaining, pct, over }) => (
                      <tr key={po.po_number} className="transition-colors hover:bg-surface2">
                        <TD className="num font-medium">{po.po_number}</TD>
                        <TD className="text-muted">{po.vendor}</TD>
                        <TD align="right" className="num">
                          {money(po.amount)}
                        </TD>
                        <TD align="right" className="num">
                          {money(consumed)}
                        </TD>
                        <TD align="right" className="num font-semibold">
                          {money(remaining)}
                        </TD>
                        <TD>
                          <div className="flex items-center gap-2">
                            <div className="min-w-[70px] flex-1">
                              <MeterBar
                                height={5}
                                ariaLabel={`${pct.toFixed(0)} percent consumed`}
                                segments={[
                                  {
                                    value: consumed,
                                    color: over
                                      ? "var(--danger-solid)"
                                      : pct >= 99.5
                                        ? "var(--warning-solid)"
                                        : "var(--success-solid)",
                                  },
                                  {
                                    value: Math.max(0, po.amount - consumed),
                                    color: "transparent",
                                  },
                                ]}
                              />
                            </div>
                            <span className="num w-9 shrink-0 text-right text-[11px] text-subtle">
                              {pct.toFixed(0)}%
                            </span>
                          </div>
                        </TD>
                        <TD>
                          <StatusBadge status={po.status} />
                        </TD>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card padded={false}>
            <div className="p-4 sm:p-5">
              <CardHeader
                title="Approved vendors"
                description="An invoice from a vendor on file but not approved is rejected outright."
              />
            </div>

            {loading ? (
              <div className="px-4 pb-4 sm:px-5">
                <SkeletonRows rows={4} cols={3} />
              </div>
            ) : vendors.length === 0 ? (
              <EmptyState
                icon={<IconShield size={18} />}
                title="No vendors match"
                description="Try a different search term."
              />
            ) : (
              <div className="overflow-x-auto border-t border-border">
                <table className="w-full min-w-[420px] border-collapse text-[13px]">
                  <thead>
                    <tr className="border-b border-border">
                      <TH>Vendor</TH>
                      <TH>Vendor ID</TH>
                      <TH>Status</TH>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {vendors.map((v) => (
                      <tr key={v.vendor_id} className="transition-colors hover:bg-surface2">
                        <TD className="font-medium">{v.vendor_name}</TD>
                        <TD className="num text-muted">{v.vendor_id}</TD>
                        <TD>
                          <Badge tone={v.status === "approved" ? "success" : "neutral"}>
                            {v.status}
                          </Badge>
                        </TD>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </PageBody>
  );
}
