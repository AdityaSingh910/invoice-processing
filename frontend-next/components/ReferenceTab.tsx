"use client";

/** The reference data every decision is made against: the PO ledger and the
 *  approved-vendor list. Read-only -- this is what the rules consult. */
import { useEffect, useState } from "react";
import { apiJson } from "@/lib/api";
import { money } from "@/lib/format";
import type { Reference } from "@/lib/types";
import { Card, StatusPill } from "./ui";

export default function ReferenceTab() {
  const [data, setData] = useState<Reference | null>(null);

  useEffect(() => {
    apiJson<Reference>("/api/reference")
      .then(setData)
      .catch(() => setData(null));
  }, []);

  return (
    <div className="grid gap-5 xl:grid-cols-2">
      <Card title="Purchase orders">
        <Table head={["PO #", "Vendor", "Amount", "Status"]} numeric={2}>
          {(data?.purchase_orders || []).map((po) => (
            <tr key={po.po_number} className="border-b border-border last:border-0">
              <td className="py-2.5 font-mono text-[13px] font-semibold">{po.po_number}</td>
              <td className="py-2.5 pr-3 text-[14px]">{po.vendor}</td>
              <td className="py-2.5 pr-3 text-right text-[14px] font-semibold whitespace-nowrap tabular-nums">{money(po.amount)}</td>
              <td className="py-2.5">
                <StatusPill status={po.status} />
              </td>
            </tr>
          ))}
        </Table>
      </Card>

      <Card title="Approved vendors">
        <Table head={["Vendor", "ID", "Status"]}>
          {(data?.vendors || []).map((v) => (
            <tr key={v.vendor_id} className="border-b border-border last:border-0">
              <td className="py-2.5 pr-3 text-[14px]">{v.vendor_name}</td>
              <td className="py-2.5 pr-3 font-mono text-[13px]">{v.vendor_id}</td>
              <td className="py-2.5">
                <StatusPill status={v.status} />
              </td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}

function Table({
  head,
  numeric,
  children,
}: {
  head: string[];
  numeric?: number;
  children: React.ReactNode;
}) {
  return (
    <div className="-mx-4 overflow-x-auto px-4">
      <table className="w-full min-w-[420px] border-collapse text-left">
        <thead>
          <tr className="border-b border-border">
            {head.map((h, i) => (
              <th key={h} className={`eyebrow py-2 pr-3 ${i === numeric ? "text-right" : ""}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
