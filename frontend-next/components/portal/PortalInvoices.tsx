"use client";

/**
 * The supplier's own invoices, and one invoice in detail.
 *
 * The list is the answer to the only question a vendor ever opens a portal to
 * ask — "where is my invoice" — so state is the column that leads, and the
 * reason it is in that state is one click away rather than buried.
 *
 * Nothing on this screen is derived from anything but the server's projection.
 * The state word, the explanation sentences and the timeline labels were all
 * chosen server-side from frozen tables; this file arranges them.
 */
import { useCallback, useEffect, useState } from "react";
import { apiJson, portalDocumentUrl } from "@/lib/api";
import { amount, when } from "@/lib/format";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Panel,
  PanelHeader,
  Segmented,
  SkeletonRows,
  TD,
  TH,
} from "@/components/ui";
import { IconInvoice } from "@/components/ui/icons";
import type { ClientState, PortalIdentity, PortalInvoice, PortalInvoiceList } from "@/lib/types";
import { PortalPage, STATE_WORD, stateTone } from "./PortalApp";

type Filter = "ALL" | ClientState;

const FILTERS: { value: Filter; label: string }[] = [
  { value: "ALL", label: "All" },
  { value: "IN_REVIEW", label: "Being checked" },
  { value: "APPROVED", label: "Approved" },
  { value: "DECLINED", label: "Declined" },
];

export default function PortalInvoices({
  identity,
  reloadKey,
}: {
  identity: PortalIdentity;
  reloadKey: number;
}) {
  const [filter, setFilter] = useState<Filter>("ALL");
  const [data, setData] = useState<PortalInvoiceList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<PortalInvoice | null>(null);

  // The filter is sent to the SERVER rather than applied to a list already in
  // the browser. Not for security — the browser only ever holds this
  // supplier's rows either way — but because the list is paged, and filtering
  // a page client-side would show "3 of 25" while claiming to be the whole
  // set.
  const load = useCallback(() => {
    setError(null);
    const qs = filter === "ALL" ? "" : `?state=${filter}`;
    apiJson<PortalInvoiceList>(`/api/portal/invoices${qs}`)
      .then(setData)
      .catch(() => setError("We could not load your invoices. Please try again."))
      .finally(() => setLoading(false));
  }, [filter]);

  useEffect(load, [load, reloadKey]);

  return (
    <PortalPage
      title="My invoices"
      description={
        identity.vendors.length
          ? `Invoices we hold for ${identity.vendors.join(", ")}.`
          : "Invoices we hold for your company."
      }
    >
      <Panel flush>
        <PanelHeader
          bordered
          title="Invoices"
          description={
            data
              ? `${data.total} invoice${data.total === 1 ? "" : "s"} on file`
              : undefined
          }
          actions={
            <Segmented
              ariaLabel="Filter by status"
              options={FILTERS}
              value={filter}
              onChange={(v) => {
                setLoading(true);
                setFilter(v);
              }}
            />
          }
        />

        {error ? (
          <ErrorState description={error} onRetry={load} />
        ) : loading ? (
          <SkeletonRows rows={5} cols={5} />
        ) : !data || data.invoices.length === 0 ? (
          <EmptyState
            icon={<IconInvoice size={16} />}
            title={filter === "ALL" ? "No invoices yet" : "Nothing in this state"}
            description={
              filter === "ALL"
                ? "Invoices you send us — by email or through this portal — appear here."
                : "Try a different status."
            }
          />
        ) : (
          <DataTable minWidth={720}>
            <thead>
              <tr>
                <TH>Invoice</TH>
                <TH>Received</TH>
                <TH align="right">Amount</TH>
                <TH>Purchase order</TH>
                <TH>Status</TH>
              </tr>
            </thead>
            <tbody>
              {data.invoices.map((inv) => (
                <tr
                  key={inv.invoice_id}
                  onClick={() => setOpen(inv)}
                  tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && setOpen(inv)}
                  className="cursor-pointer"
                >
                  <TD>
                    <span className="font-medium">{inv.invoice_number ?? "—"}</span>
                    {inv.submitted_through_portal && (
                      <Badge tone="neutral" className="ml-2">
                        Sent here
                      </Badge>
                    )}
                  </TD>
                  <TD>{when(inv.received_at) || "—"}</TD>
                  <TD align="right">{amount(inv.total, inv.currency)}</TD>
                  <TD>{inv.purchase_orders.join(", ") || "—"}</TD>
                  <TD>
                    <Badge tone={stateTone(inv.state)} dot>
                      {STATE_WORD[inv.state] ?? inv.state}
                    </Badge>
                  </TD>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Panel>

      {open && <InvoiceDetail invoice={open} onClose={() => setOpen(null)} />}
    </PortalPage>
  );
}

/**
 * One invoice, fetched fresh by id.
 *
 * Refetched rather than rendered from the list row, because the detail
 * response is the one that carries the timeline — and because a row a supplier
 * has been looking at for ten minutes may have been decided since.
 */
function InvoiceDetail({
  invoice,
  onClose,
}: {
  invoice: PortalInvoice;
  onClose: () => void;
}) {
  const [full, setFull] = useState<PortalInvoice>(invoice);
  const [docUrl, setDocUrl] = useState<string | null>(null);
  const [docError, setDocError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiJson<PortalInvoice>(`/api/portal/invoices/${invoice.invoice_id}`)
      .then((d) => !cancelled && setFull(d))
      .catch(() => {
        /* the row we already have is still true; a failed refresh is not
           worth replacing it with an error */
      });
    return () => {
      cancelled = true;
    };
  }, [invoice.invoice_id]);

  // The blob URL is revoked on unmount. Without it every open-and-close leaks
  // a copy of the PDF into the tab's memory for as long as it lives.
  useEffect(
    () => () => {
      if (docUrl) URL.revokeObjectURL(docUrl);
    },
    [docUrl]
  );

  const openDocument = async () => {
    setDocError(null);
    try {
      setDocUrl(await portalDocumentUrl(full.invoice_id));
    } catch {
      setDocError("This document is no longer available.");
    }
  };

  return (
    <Panel>
      <PanelHeader
        title={full.invoice_number ?? "Invoice"}
        description={`Received ${when(full.received_at) || "—"}`}
        actions={
          <>
            {full.has_document && !docUrl && (
              <Button size="sm" onClick={openDocument}>
                View document
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={onClose}>
              Close
            </Button>
          </>
        }
      />

      <div className="mt-3 grid gap-4 lg:grid-cols-2">
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <Badge tone={stateTone(full.state)} dot>
              {STATE_WORD[full.state] ?? full.state}
            </Badge>
            <span className="text-[12.5px] text-muted">{full.state_headline}</span>
          </div>

          {full.state_detail.length > 0 && (
            <ul className="flex flex-col gap-1.5">
              {full.state_detail.map((line, i) => (
                <li key={i} className="text-[12.5px] leading-relaxed text-fg">
                  {line}
                </li>
              ))}
            </ul>
          )}

          <dl className="divide-line">
            {(
              [
                ["Amount", amount(full.total, full.currency)],
                ["Purchase order", full.purchase_orders.join(", ") || "—"],
                ["File", full.filename ?? "—"],
              ] as [string, string][]
            ).map(([k, v]) => (
              <div key={k} className="flex items-baseline justify-between gap-4 py-1.5">
                <dt className="t-meta shrink-0">{k}</dt>
                <dd className="min-w-0 text-right text-[12.5px] font-medium break-words">{v}</dd>
              </div>
            ))}
          </dl>

          {docError && <p className="text-[12px] text-bad">{docError}</p>}
        </div>

        <div className="flex flex-col gap-3">
          <p className="t-meta">History</p>
          {full.timeline && full.timeline.length > 0 ? (
            <ol className="flex flex-col gap-2">
              {full.timeline.map((e, i) => (
                <li key={i} className="flex items-baseline gap-2.5">
                  <span aria-hidden className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                  <span className="text-[12.5px]">{e.event}</span>
                  <span className="ml-auto shrink-0 text-[11.5px] text-faint">
                    {when(e.at)}
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-[12.5px] text-faint">Nothing recorded yet.</p>
          )}
        </div>
      </div>

      {docUrl && (
        <object
          data={docUrl}
          type="application/pdf"
          aria-label="Invoice document"
          className="mt-4 h-[60vh] w-full rounded-[var(--radius-md)] border border-line"
        >
          <p className="p-4 text-[12.5px] text-muted">
            This browser cannot display the PDF inline.{" "}
            <a className="underline" href={docUrl} download>
              Download it instead
            </a>
            .
          </p>
        </object>
      )}
    </Panel>
  );
}
