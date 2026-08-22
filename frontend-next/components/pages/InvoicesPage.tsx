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
import { amount, when, whenCompact } from "@/lib/format";
import type { RunRecord, Verdict } from "@/lib/types";
import type { Async } from "@/lib/useData";
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Panel,
  SearchInput,
  Segmented,
  Select,
  SkeletonRows,
  SortTH,
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
import ReviewWorkspace from "@/components/invoice/ReviewWorkspace";

export type Filter = "ALL" | Verdict | "EXCEPTIONS";
type SortKey = "created_at" | "total" | "vendor_name" | "status";

const PAGE_SIZE = 14;

export default function InvoicesPage({
  runs,
  initialFilter,
}: {
  runs: Async<RunRecord[]>;
  /** Set once, from how the page was navigated to -- e.g. Overview's "Open
   *  review queue" lands here pre-filtered rather than making the reviewer
   *  reselect the tab they were just told to open. */
  initialFilter?: Filter;
}) {
  const [filter, setFilter] = useState<Filter>(initialFilter ?? "ALL");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("created_at");
  const [asc, setAsc] = useState(false);
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState<number | null>(null);
  const [detail, setDetail] = useState<RunRecord | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const rows = useMemo(() => runs.data ?? [], [runs.data]);

  /* INVOICES AND REVIEW QUEUE ARE THE SAME SECTION, SO NEITHER REMOUNTS.
     `initialFilter` was read by useState, which uses its argument on the FIRST
     render and ignores it forever after. Both nav rows open section "invoices",
     so React keeps this instance mounted and merely hands it a new prop -- and
     nothing read it. Clicking "Review queue" while on "Invoices" lit the other
     row and changed nothing else; the only way to actually switch was to visit
     a different section first, because that unmounted this page and let the
     initialiser run again.

     Syncing the prop is what makes the two rows navigate. `page` goes back to
     one for the same reason the effect below does it: the new filter has its
     own row count. */
  useEffect(() => {
    setFilter(initialFilter ?? "ALL");
    setPage(0);
  }, [initialFilter]);

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

  // Previous/Next in the workspace walk the same filtered, sorted order the
  // table is showing — across page boundaries, not just within one page.
  const openIndex = openId === null ? -1 : filtered.findIndex((r) => r.id === openId);
  const goTo = (i: number) => {
    if (i < 0 || i >= filtered.length) return;
    setOpenId(filtered[i].id);
  };

  function toggleSort(key: SortKey) {
    if (sort === key) setAsc((a) => !a);
    else {
      setSort(key);
      setAsc(key === "vendor_name");   // names read A–Z, figures high-first
    }
  }

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
          <Button
            size="sm"
            onClick={runs.refresh}
            disabled={runs.loading}
            icon={<IconRefresh size={13} />}
          >
            {runs.loading ? "Refreshing…" : "Refresh"}
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
          {/* Redundant with the sortable column headers, and deliberately
              so: the headers are a pointer affordance, this works on a narrow
              screen where the table is scrolled horizontally and the header
              for the column you want is off-screen. */}
          <Select
            className="ml-auto"
            value={sort}
            onChange={(e) => setSort(e.currentTarget.value as SortKey)}
            aria-label="Sort invoices by"
          >
            <option value="created_at">Sort: newest</option>
            <option value="total">Sort: amount</option>
            <option value="vendor_name">Sort: vendor</option>
            <option value="status">Sort: status</option>
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
              <DataTable minWidth={880}>
                <thead>
                  <tr>
                    <TH className="w-[52px]">Run</TH>
                    <SortTH
                      label="Vendor"
                      active={sort === "vendor_name"}
                      ascending={asc}
                      onSort={() => toggleSort("vendor_name")}
                    />
                    <TH>Invoice</TH>
                    <SortTH
                      label="Amount"
                      align="right"
                      active={sort === "total"}
                      ascending={asc}
                      onSort={() => toggleSort("total")}
                    />
                    <TH>Purchase order</TH>
                    <SortTH
                      label="Status"
                      active={sort === "status"}
                      ascending={asc}
                      onSort={() => toggleSort("status")}
                    />
                    <SortTH
                      label="Processed"
                      align="right"
                      active={sort === "created_at"}
                      ascending={asc}
                      onSort={() => toggleSort("created_at")}
                    />
                  </tr>
                </thead>
                <tbody>
                  {visible.map((r) => (
                    <tr
                      key={r.id}
                      tabIndex={0}
                      role="button"
                      aria-label={`Open ${r.invoice_number || r.filename}`}
                      onClick={() => setOpenId(r.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setOpenId(r.id);
                        }
                      }}
                      className="interactive"
                    >
                      <TD className="tnum text-[12.5px] text-faint">{r.id}</TD>

                      {/* Vendor leads. An AP clerk looks for "who is billing
                          me", then "which invoice" — the upload filename is an
                          artefact of transport and belongs underneath, not in
                          the identity column where it was before. */}
                      <TD>
                        <div className="max-w-[190px] truncate text-[13.5px] font-medium">
                          {r.vendor_name || "Unknown vendor"}
                        </div>
                        <div
                          className="t-meta max-w-[190px] truncate text-[12px]"
                          title={r.filename}
                        >
                          {r.filename}
                        </div>
                      </TD>

                      <TD className="tnum text-[13.5px]">
                        {r.invoice_number || <span className="text-faint">—</span>}
                      </TD>

                      <TD align="right" className="text-[13.5px] font-semibold">
                        {/* Falls back to USD, matching the extractor's own
                            default -- a run with no stored audit trail must
                            not lose its currency SYMBOL, just its precision
                            about which currency it actually was. */}
                        {amount(r.total, r.audit?.invoice?.currency || "USD")}
                      </TD>

                      <TD className="tnum text-[13px] text-muted">
                        {r.po_number || <span className="text-faint">—</span>}
                      </TD>

                      <TD>
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
                      </TD>

                      <TD align="right">
                        {/* The full timestamp is the tooltip. A register column
                            showing "21/08/2026, 00:42:23" on every row spends
                            its width on a year and a seconds field nobody is
                            scanning for. */}
                        <Tooltip label={when(r.created_at)} side="top">
                          <span className="tnum t-meta text-[12.5px]">
                            {whenCompact(r.created_at)}
                          </span>
                        </Tooltip>
                      </TD>
                    </tr>
                  ))}
                </tbody>
              </DataTable>

              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line bg-sunken px-4 py-2.5">
                <p className="t-meta text-[12.5px]">
                  Showing{" "}
                  <span className="tnum font-semibold text-secondary">
                    {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, filtered.length)}
                  </span>{" "}
                  of <span className="tnum font-semibold text-secondary">{filtered.length}</span>
                  {filtered.length !== rows.length && (
                    <span className="text-faint"> (filtered from {rows.length})</span>
                  )}
                </p>
                {pages > 1 && (
                  <div className="flex items-center gap-1.5">
                    <Button
                      variant="secondary"
                      size="xs"
                      disabled={page === 0}
                      onClick={() => setPage((p) => p - 1)}
                      aria-label="Previous page"
                      icon={<IconChevronLeft size={14} />}
                    />
                    <span className="tnum px-1 text-[12.5px] text-muted">
                      Page <span className="font-semibold text-secondary">{page + 1}</span> of{" "}
                      {pages}
                    </span>
                    <Button
                      variant="secondary"
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

      {openId !== null && detailError ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-canvas p-6">
          <Panel className="w-full max-w-sm">
            <ErrorState description={detailError} onRetry={() => setOpenId(openId)} />
            <div className="mt-2 flex justify-center">
              <Button size="sm" onClick={() => setOpenId(null)}>
                Back to invoices
              </Button>
            </div>
          </Panel>
        </div>
      ) : openId !== null && !detail ? (
        <div className="fixed inset-0 z-50 bg-canvas p-6">
          <SkeletonRows rows={9} cols={4} />
        </div>
      ) : (
        <ReviewWorkspace
          // `detail` deliberately is NOT cleared the instant openId resets to
          // null (see the fetch effect above) -- it would otherwise blank the
          // workspace for a frame on close. Gating on openId here, rather than
          // on `detail` alone, is what actually closes the overlay: passing
          // stale `detail` through while openId is null would keep the
          // full-screen overlay mounted and block the rest of the app.
          run={openId !== null ? detail : null}
          onClose={() => setOpenId(null)}
          onReviewed={() => {
            runs.refresh();
            setOpenId(null);
          }}
          onPrev={openIndex > 0 ? () => goTo(openIndex - 1) : undefined}
          onNext={openIndex >= 0 && openIndex < filtered.length - 1 ? () => goTo(openIndex + 1) : undefined}
        />
      )}
    </>
  );
}
