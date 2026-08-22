"use client";

/**
 * The supplier's own purchase orders, and how much of each is left to bill.
 *
 * This is the one screen here that answers a question BEFORE an invoice is
 * sent rather than after: "how much can I still invoice against this order".
 * Billing over the remaining balance is the single most common reason an
 * invoice is held (the tolerance check is one-sided on purpose — billing under
 * a balance is an ordinary partial invoice), so showing the balance is the
 * cheapest way to prevent the hold rather than explain it afterwards.
 *
 * `remaining` is the ledger's own derived figure, computed server-side from
 * the same allocations the buyer's own screens read. There is no per-client
 * copy of a balance and nothing is recomputed here.
 */
import { useCallback, useEffect, useState } from "react";
import { apiJson } from "@/lib/api";
import { amount } from "@/lib/format";
import { useT } from "@/lib/i18n";
import {
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  Meter,
  Panel,
  PanelHeader,
  SkeletonRows,
  TD,
  TH,
} from "@/components/ui";
import { IconLedger } from "@/components/ui/icons";
import type { PortalIdentity, PortalOrders as Orders } from "@/lib/types";
import { PortalPage } from "./PortalApp";

export default function PortalOrders({ identity }: { identity: PortalIdentity }) {
  const t = useT();
  const [data, setData] = useState<Orders | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    apiJson<Orders>("/api/portal/purchase-orders")
      .then(setData)
      .catch(() => setError(t("portal.orders.loadFailed")))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  return (
    <PortalPage
      title={t("portal.orders.title")}
      description={
        identity.vendors.length
          ? // The vendor NAMES are the supplier's own and are never translated;
            // only the sentence around them is.
            `${t("portal.orders.for").replace(/[.]$/, "")}: ${identity.vendors.join(", ")}`
          : t("portal.orders.for")
      }
    >
      <Panel flush>
        <PanelHeader
          bordered
          title={t("portal.orders.panel.title")}
          description={t("portal.orders.panel.desc")}
        />

        {error ? (
          <ErrorState description={error} onRetry={load} />
        ) : loading ? (
          <SkeletonRows rows={4} cols={5} />
        ) : !data || data.purchase_orders.length === 0 ? (
          <EmptyState
            icon={<IconLedger size={16} />}
            title={t("portal.orders.empty")}
            description={t("portal.orders.empty.desc")}
          />
        ) : (
          <DataTable minWidth={760}>
            <thead>
              <tr>
                <TH>{t("portal.orders.col.order")}</TH>
                <TH>{t("portal.orders.col.description")}</TH>
                <TH align="right">{t("portal.orders.col.value")}</TH>
                <TH align="right">{t("portal.orders.col.billed")}</TH>
                <TH align="right">{t("portal.orders.col.remaining")}</TH>
                <TH>{t("portal.invoices.col.state")}</TH>
              </tr>
            </thead>
            <tbody>
              {data.purchase_orders.map((po) => {
                const value = Number(po.amount ?? 0);
                // A fully-billed order is worth flagging quietly rather than
                // in red: it is a normal end state for an order, not a fault.
                const spent = value > 0 && po.remaining <= 0;
                return (
                  <tr key={po.po_number}>
                    <TD>
                      <span className="font-medium">{po.po_number}</span>
                    </TD>
                    <TD>
                      <span className="text-muted">{po.description ?? "—"}</span>
                    </TD>
                    <TD align="right">{amount(po.amount, po.currency)}</TD>
                    <TD align="right">{amount(po.billed, po.currency)}</TD>
                    <TD align="right">
                      <div className="flex flex-col items-end gap-1">
                        <span className={spent ? "text-muted" : "font-medium"}>
                          {amount(po.remaining, po.currency)}
                        </span>
                        <span className="w-20">
                          <Meter
                            value={Math.max(0, value - po.remaining)}
                            max={value}
                            tone={spent ? "neutral" : "accent"}
                            ariaLabel={`${po.po_number} billed`}
                          />
                        </span>
                      </div>
                    </TD>
                    <TD>
                      <Badge tone={po.status === "open" ? "ok" : "neutral"} dot>
                        {po.status === "open"
                          ? t("portal.orders.state.open")
                          : t("portal.orders.state.closed")}
                      </Badge>
                    </TD>
                  </tr>
                );
              })}
            </tbody>
          </DataTable>
        )}
      </Panel>
    </PortalPage>
  );
}
