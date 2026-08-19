"use client";

/**
 * The invoice register.
 *
 * Filtering, searching, sorting and paging all happen client-side. GET /api/runs
 * returns the full list in one response and has no query parameters, so doing
 * this in the browser needs no API change — the contract is untouched. At the
 * volume this process handles that is the right trade; if the register grew past
 * a few thousand rows the work would move server-side and this component would
 * keep the same shape.
 */
import { useEffect, useMemo, useState } from "react";
import { apiJson } from "@/lib/api";
import { money, when } from "@/lib/format";
import type { RunRecord, Verdict } from "@/lib/types";
import { useAuth } from "@/lib/auth";
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  SearchInput,
  SegmentedControl,
  Select,
  SkeletonRows,
  StatusBadge,
  TD,
  TH,
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
import type { Async } from "@/lib/useData";

type Filter = "ALL" | Verdict | "EXCEPTIONS";
type SortKey = "created_at" | "total" | "vendor_name" | "status";

const PAGE_SIZE = 12;

export default function InvoicesPage({ runs }: { runs: Async<RunRecord[]> }) {
  const { can } = useAuth();
  const [filter, setFilter] = useState<Filter>("ALL");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("created_at");
  const [asc, setAsc] = useState(false);
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState<number | null>(null);
  const [detail, setDetail] = useState<RunRecord | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const rows = runs.data ?? [];

  // Any change to the query shape must return to the first page, or a filter
  // can leave you stranded on a page that no longer exists.
  useEffect(() => setPage(0), [filter, query, sort, asc]);

  /* The detail dialog refetches the single run: the list payload may omit the
     audit trail, and a detail view built from a summary row would be showing
     less evidence than the page claims to show. */
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
          return (
            (new Date(a.created_at).getTime() - new Date(b.created_at).getTime()) * dir
          );
      }
    });
  }, [rows, filter, query, sort, asc]);

  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visible = filtered.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);

  function toggleSort(key: SortKey) {
    if (sort === key) setAsc((a) => !a);
    else {
      setSort(key);
      setAsc(key === "vendor_name");   // names read better A–Z, figures high-first
    }
  }

  const SortHead = ({ k, label, align }: { k: SortKey; label: string; align?: "right" }) => (
    <TH align={align}>
      <button
        onClick={() => toggleSort(k)}
        className="inline-flex items-center gap-1 transition-colors hover:text-fg"
        aria-label={`Sort by ${label}`}
      >
        {label}
        <span className={sort === k ? "text-accent" : "text-transparent"}>{asc ? "↑" : "↓"}</span>
      </button>
    </TH>
  );

  return (
    <PageBody>
      <PageHeader
        title="Invoices"
        description={
          runs.data
            ? `${rows.length} processed · ${counts.EXCEPTIONS} awaiting review`
            : "Every invoice this process has handled."
        }
        actions={
          <Button
            variant="secondary"
            size="sm"
            onClick={runs.refresh}
            icon={<IconRefresh size={14} />}
          >
            Refresh
          </Button>
        }
      />

      <div className="flex flex-wrap items-center gap-3">
        <SegmentedControl<Filter>
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
          className="min-w-[200px] flex-1 sm:max-w-xs"
          placeholder="Search vendor, invoice, PO…"
          value={query}
          onChange={(e) => setQuery(e.currentTarget.value)}
          aria-label="Search invoices"
        />
        <Select
          className="w-auto"
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

      <div className="overflow-hidden rounded-[var(--radius-lg)] border border-border bg-surface shadow-[var(--shadow-xs)]">
        {runs.error ? (
          <ErrorState description={runs.error} onRetry={runs.refresh} />
        ) : runs.loading && !runs.data ? (
          <div className="p-4">
            <SkeletonRows rows={8} cols={5} />
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={<IconInvoice size={18} />}
            title={rows.length === 0 ? "No invoices yet" : "Nothing matches those filters"}
            description={
              rows.length === 0
                ? "Process an invoice and it will appear here with its full decision trail."
                : "Try a different status or clear the search."
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
              <table className="w-full min-w-[820px] border-collapse text-[13px]">
                <thead>
                  <tr className="border-b border-border">
                    <TH className="w-14">ID</TH>
                    <TH>Invoice</TH>
                    <SortHead k="vendor_name" label="Vendor" />
                    <SortHead k="total" label="Amount" align="right" />
                    <TH>PO</TH>
                    <SortHead k="status" label="Status" />
                    <SortHead k="created_at" label="Processed" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {visible.map((r) => (
                    <tr
                      key={r.id}
                      tabIndex={0}
                      onClick={() => setOpenId(r.id)}
                      onKeyDown={(e) => e.key === "Enter" && setOpenId(r.id)}
                      className="cursor-pointer transition-colors hover:bg-surface2 focus:bg-surface2 focus:outline-none"
                    >
                      <TD className="num text-subtle">#{r.id}</TD>
                      <TD>
                        <div className="max-w-[240px] truncate font-medium">{r.filename}</div>
                        {r.invoice_number && (
                          <div className="num text-[12px] text-subtle">{r.invoice_number}</div>
                        )}
                      </TD>
                      <TD className="text-muted">
                        <span className="block max-w-[180px] truncate">{r.vendor_name || "—"}</span>
                      </TD>
                      <TD align="right" className="num font-semibold whitespace-nowrap">
                        {money(r.total)}
                      </TD>
                      <TD className="num text-muted">{r.po_number || "—"}</TD>
                      <TD>
                        <div className="flex items-center gap-1.5">
                          <StatusBadge status={r.status} />
                          {r.human_decision && (
                            <Tooltip
                              label={`${String(r.final_decision || "")
                                .replace(/_/g, " ")
                                .toLowerCase()} by ${r.reviewed_by || "a reviewer"}`}
                            >
                              <Badge tone="neutral" icon={<IconUser size={10} />}>
                                human
                              </Badge>
                            </Tooltip>
                          )}
                        </div>
                      </TD>
                      <TD className="whitespace-nowrap text-subtle">{when(r.created_at)}</TD>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-2.5">
              <p className="text-[12px] text-subtle">
                Showing{" "}
                <span className="num font-medium text-muted">
                  {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, filtered.length)}
                </span>{" "}
                of <span className="num font-medium text-muted">{filtered.length}</span>
              </p>
              {pages > 1 && (
                <div className="flex items-center gap-1.5">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="px-2"
                    disabled={page === 0}
                    onClick={() => setPage((p) => p - 1)}
                    aria-label="Previous page"
                    icon={<IconChevronLeft size={15} />}
                  />
                  <span className="num text-[12px] text-muted">
                    {page + 1} / {pages}
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="px-2"
                    disabled={page >= pages - 1}
                    onClick={() => setPage((p) => p + 1)}
                    aria-label="Next page"
                    icon={<IconChevronRight size={15} />}
                  />
                </div>
              )}
            </div>
          </>
        )}
      </div>

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
          <div className="flex flex-col gap-4">
            <SkeletonRows rows={6} cols={3} />
          </div>
        ) : (
          <div className="flex flex-col gap-5">
            <VerdictHeader
              status={detail.status}
              filename={detail.filename}
              runId={detail.id}
              vendor={detail.vendor_name}
              invoiceNumber={detail.invoice_number}
              total={detail.total}
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
    </PageBody>
  );
}
