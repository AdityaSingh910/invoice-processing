"use client";

/**
 * The invoice register.
 *
 * Filtering, searching, sorting and paging happen client-side. GET /api/runs
 * returns the full list in one response and takes no query parameters, so doing
 * this in the browser needs no API change — the contract is untouched. At this
 * volume that is the right trade; past a few thousand rows the work moves
 * server-side and this component keeps its shape.
 */
import { useEffect, useMemo, useState } from "react";
import { apiJson } from "@/lib/api";
import { amount, money, when } from "@/lib/format";
import type { RunRecord, Verdict } from "@/lib/types";
import type { Async } from "@/lib/useData";
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Panel,
  SearchInput,
  Segmented,
  Select,
  SkeletonRows,
  StatusBadge,
  Tooltip,
} from "@/components/ui";
import {
  IconChevronLeft,
  IconChevronRight,
  IconInvoice,
  IconRefresh,
  IconUser,
} from "@/components/ui/icons";
import Modal from "@/components/ui/Modal";
import RunDetail from "@/components/invoice/RunDetail";
import { VerdictHeader } from "@/components/invoice/Panels";

type Filter = "ALL" | Verdict | "EXCEPTIONS";
type SortKey = "created_at" | "total" | "vendor_name" | "status";

const PAGE_SIZE = 14;

export default function InvoicesPage({ runs }: { runs: Async<RunRecord[]> }) {
  const [filter, setFilter] = useState<Filter>("ALL");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("created_at");
  const [asc, setAsc] = useState(false);
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState<number | null>(null);
  const [detail, setDetail] = useState<RunRecord | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const rows = useMemo(() => runs.data ?? [], [runs.data]);

  // Any change to the query shape returns to page one, or a filter can strand
  // you on a page that no longer exists.
  useEffect(() => setPage(0), [filter, query, sort, asc]);

  /* The dialog refetches the single run: the list payload may omit the audit
     trail, and a detail view built from a summary row would show less evidence
     than the screen claims to show. */
  useEffect(() => {
    if (openId === null) return;
    setDetail(null);
    setDetailError(null);
    apiJson<RunRecord>(`/api/runs/${openId}`)
      .then(setDetail)
      .catch(() => setDetailError("This invoice could not be loaded."));
  }, [openId]);

  const counts = useMemo(() => {
    const c = { ALL: rows.length, APPROVED: 0, NEEDS_REVIEW: 0, REJECTED: 0, EXCEPTIONS: 0 };
    for (const r of rows) {
      if (r.status in c) (c as Record<string, number>)[r.status]++;
      if ((r.automated_decision ?? r.status) === "NEEDS_REVIEW" && !r.human_decision) c.EXCEPTIONS++;
    }
    return c;
  }, [rows]);

  const filtered = useMemo(() => {
    let out = rows;

    if (filter === "EXCEPTIONS") {
      out = out.filter(
        (r) => (r.automated_decision ?? r.status) === "NEEDS_REVIEW" && !r.human_decision
      );
    } else if (filter !== "ALL") {
      out = out.filter((r) => r.status === filter);
    }

    const q = query.trim().toLowerCase();
    if (q) {
      out = out.filter((r) =>
        [r.filename, r.vendor_name, r.invoice_number, r.po_number, String(r.id)]
          .filter(Boolean)
          .some((v) => String(v).toLowerCase().includes(q))
      );
    }

    const dir = asc ? 1 : -1;
    return [...out].sort((a, b) => {
      switch (sort) {
        case "total":
          return ((a.total ?? -Infinity) - (b.total ?? -Infinity)) * dir;
        case "vendor_name":
          return String(a.vendor_name || "").localeCompare(String(b.vendor_name || "")) * dir;
        case "status":
          return String(a.status).localeCompare(String(b.status)) * dir;
        default:
          return (new Date(a.created_at).getTime() - new Date(b.created_at).getTime()) * dir;
      }
    });
  }, [rows, filter, query, sort, asc]);

  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visible = filtered.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);

  function toggleSort(key: SortKey) {
    if (sort === key) setAsc((a) => !a);
    else {
      setSort(key);
      setAsc(key === "vendor_name");   // names read A–Z, figures high-first
    }
  }

  const SortHead = ({
    k,
    label,
    align = "left",
    className = "",
  }: {
    k: SortKey;
    label: string;
    align?: "left" | "right";
    className?: string;
  }) => (
    <th
      scope="col"
      className={`t-caption border-b border-line px-3 py-2 ${
        align === "right" ? "text-right" : "text-left"
      } ${className}`}
    >
      <button
        onClick={() => toggleSort(k)}
        aria-label={`Sort by ${label}`}
        className={`inline-flex items-center gap-1 transition-colors hover:text-secondary ${
          align === "right" ? "flex-row-reverse" : ""
        }`}
      >
        {label}
        <span className={sort === k ? "text-accent" : "opacity-0"}>{asc ? "↑" : "↓"}</span>
      </button>
    </th>
  );

  return (
    <>
      <PageHeader
        title="Invoices"
        description={
          runs.data
            ? `${rows.length} processed · ${counts.EXCEPTIONS} awaiting review`
            : "Every invoice this process has handled."
        }
        actions={
          <Button size="sm" onClick={runs.refresh} icon={<IconRefresh size={13} />}>
            Refresh
          </Button>
        }
      />

      <PageBody>
        <div className="flex flex-wrap items-center gap-2">
          <Segmented<Filter>
            ariaLabel="Filter invoices by status"
            value={filter}
            onChange={setFilter}
            options={[
              { value: "ALL", label: "All", count: counts.ALL },
              { value: "EXCEPTIONS", label: "Needs action", count: counts.EXCEPTIONS },
              { value: "APPROVED", label: "Approved", count: counts.APPROVED },
              { value: "REJECTED", label: "Rejected", count: counts.REJECTED },
            ]}
          />
          <SearchInput
            className="min-w-[180px] flex-1 sm:max-w-[280px]"
            placeholder="Search vendor, invoice, PO…"
            value={query}
            onChange={(e) => setQuery(e.currentTarget.value)}
            aria-label="Search invoices"
          />
          <Select
            value={sort}
            onChange={(e) => setSort(e.currentTarget.value as SortKey)}
            aria-label="Sort by"
          >
            <option value="created_at">Newest first</option>
            <option value="total">Amount</option>
            <option value="vendor_name">Vendor</option>
            <option value="status">Status</option>
          </Select>
        </div>

        <Panel flush>
          {runs.error ? (
            <ErrorState description={runs.error} onRetry={runs.refresh} />
          ) : runs.loading && !runs.data ? (
            <SkeletonRows rows={10} cols={6} />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={<IconInvoice size={16} />}
              title={rows.length === 0 ? "No invoices yet" : "Nothing matches those filters"}
              description={
                rows.length === 0
                  ? "Process an invoice and it will appear here with its full decision trail."
                  : "Try a different status, or clear the search."
              }
              action={
                rows.length > 0 && (
                  <Button
                    size="sm"
                    onClick={() => {
                      setQuery("");
                      setFilter("ALL");
                    }}
                  >
                    Clear filters
                  </Button>
                )
              }
            />
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[840px] border-collapse">
                  <thead>
                    <tr>
                      <th
                        scope="col"
                        className="t-caption w-12 border-b border-line px-3 py-2 text-left"
                      >
                        ID
                      </th>
                      <th scope="col" className="t-caption border-b border-line px-3 py-2 text-left">
                        Invoice
                      </th>
                      <SortHead k="vendor_name" label="Vendor" />
                      <SortHead k="total" label="Amount" align="right" />
                      <th scope="col" className="t-caption border-b border-line px-3 py-2 text-left">
                        PO
                      </th>
                      <SortHead k="status" label="Status" />
                      <SortHead k="created_at" label="Processed" />
                    </tr>
                  </thead>
                  <tbody className="divide-line">
                    {visible.map((r) => (
                      <tr
                        key={r.id}
                        tabIndex={0}
                        onClick={() => setOpenId(r.id)}
                        onKeyDown={(e) => e.key === "Enter" && setOpenId(r.id)}
                        className="group cursor-pointer transition-colors hover:bg-hover focus:bg-hover focus:outline-none"
                      >
                        <td className="tnum px-3 py-2 text-[11.5px] text-faint">{r.id}</td>
                        <td className="px-3 py-2">
                          <div className="max-w-[260px] truncate text-[12.5px] font-medium">
                            {r.filename}
                          </div>
                          {r.invoice_number && (
                            <div className="tnum t-meta text-[11px]">{r.invoice_number}</div>
                          )}
                        </td>
                        <td className="px-3 py-2 text-[12.5px] text-muted">
                          <span className="block max-w-[170px] truncate">
                            {r.vendor_name || "—"}
                          </span>
                        </td>
                        <td className="tnum px-3 py-2 text-right text-[12.5px] font-semibold whitespace-nowrap">
                          {/* Falls back to USD, matching the extractor's own
                              default -- a run with no stored audit trail must
                              not lose its currency SYMBOL, just its precision
                              about which currency it actually was. */}
                          {amount(r.total, r.audit?.invoice?.currency || "USD")}
                        </td>
                        <td className="tnum px-3 py-2 text-[12px] text-muted">
                          {r.po_number || "—"}
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex items-center gap-1.5">
                            <StatusBadge status={r.status} />
                            {r.human_decision && (
                              <Tooltip
                                label={`${String(r.final_decision || "")
                                  .replace(/_/g, " ")
                                  .toLowerCase()} by ${r.reviewed_by || "a reviewer"}`}
                              >
                                <Badge tone="neutral" icon={<IconUser size={9} />}>
                                  human
                                </Badge>
                              </Tooltip>
                            )}
                          </div>
                        </td>
                        <td className="t-meta px-3 py-2 text-[11.5px] whitespace-nowrap">
                          {when(r.created_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-3 py-2">
                <p className="t-meta text-[11.5px]">
                  <span className="tnum font-medium text-secondary">
                    {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, filtered.length)}
                  </span>{" "}
                  of <span className="tnum font-medium text-secondary">{filtered.length}</span>
                </p>
                {pages > 1 && (
                  <div className="flex items-center gap-1">
                    <Button
                      variant="ghost"
                      size="xs"
                      disabled={page === 0}
                      onClick={() => setPage((p) => p - 1)}
                      aria-label="Previous page"
                      icon={<IconChevronLeft size={14} />}
                    />
                    <span className="tnum text-[11.5px] text-muted">
                      {page + 1} / {pages}
                    </span>
                    <Button
                      variant="ghost"
                      size="xs"
                      disabled={page >= pages - 1}
                      onClick={() => setPage((p) => p + 1)}
                      aria-label="Next page"
                      icon={<IconChevronRight size={14} />}
                    />
                  </div>
                )}
              </div>
            </>
          )}
        </Panel>
      </PageBody>

      <Modal
        open={openId !== null}
        onClose={() => setOpenId(null)}
        title={detail?.filename ?? "Invoice"}
        description={
          detail
            ? `Run #${detail.id} · ${detail.vendor_name || "unknown vendor"} · ${when(detail.created_at)}`
            : undefined
        }
      >
        {detailError ? (
          <ErrorState description={detailError} />
        ) : !detail ? (
          <SkeletonRows rows={7} cols={3} />
        ) : (
          <div className="flex flex-col gap-4">
            <VerdictHeader
              status={detail.status}
              filename={detail.filename}
              runId={detail.id}
              vendor={detail.vendor_name}
              invoiceNumber={detail.invoice_number}
              total={detail.total}
              currency={detail.audit?.invoice?.currency}
              compact
            />
            <RunDetail
              run={detail}
              onReviewed={() => {
                runs.refresh();
                setOpenId(null);
              }}
            />
          </div>
        )}
      </Modal>
    </>
  );
}
