"use client";

/**
 * The review workspace: document on the left, decision on the right.
 *
 * This is a layout, not a new source of truth. Every panel on the right is the
 * existing component from Panels.tsx / PoMatchPanel.tsx / AuditTrail.tsx /
 * ReviewBar.tsx, laid out in one place instead of stacked in a narrow dialog —
 * so a reviewer sees the document and the evidence for it at the same time.
 *
 * `ReviewWorkspaceBody` has no opinion about how it is presented; it is used
 * both inline (the Process page, right after a run finishes, where the page
 * chrome already exists) and inside `ReviewWorkspace`, the full-screen overlay
 * opened from the invoice register.
 */
import { useEffect, useState } from "react";
import { amount, STAGE_ORDER } from "@/lib/format";
import type { Audit, Extracted, PoMatch, Reason, Stage } from "@/lib/types";
import { Badge, Button, Panel, PanelHeader, StatusBadge } from "@/components/ui";
import { IconChevronLeft, IconChevronRight, IconFile, IconX } from "@/components/ui/icons";
import { VERDICT } from "./Panels";
import { ExtractedFields, ExtractionSummary, ReasonList, ReviewerBrief } from "./Panels";
import { MatchTable, PoBudget } from "./PoMatchPanel";
import AuditTrail from "./AuditTrail";
import StageList from "./StageList";
import AuditExportButtons from "./AuditExportButtons";
import RejectionNotice from "./RejectionNotice";
import ReviewBar from "./ReviewBar";
import DocumentPreview from "./DocumentPreview";
import type { ResolvedDocument } from "./DocumentPreview";
import DocumentDownloadButton from "./DocumentDownloadButton";

export interface RunLike {
  id: number;
  filename: string;
  status: string;
  reasons: Reason[];
  stages: Stage[];
  extracted?: Extracted;
  po_match?: PoMatch;
  audit?: Audit;
  automated_decision?: string | null;
  human_decision?: string | null;
  final_decision?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  review_note?: string | null;
}

/** The verdict banner at the top of the decision pane — one line naming the
 *  outcome and the invoice total, in the same tone system as everywhere else. */
function VerdictBanner({ run }: { run: RunLike }) {
  const v = VERDICT[run.status] ?? { headline: run.status, blurb: "", tone: "neutral" as const };
  const cur = run.audit?.invoice?.currency || run.extracted?.currency || "USD";
  const total = run.audit?.invoice?.total ?? run.extracted?.total;

  return (
    <div
      className="panel flex flex-wrap items-center justify-between gap-4 p-4"
      style={{ borderLeft: `3px solid var(--${v.tone}-vivid)` }}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="t-metric-sm" style={{ color: `var(--${v.tone})` }}>
            {v.headline}
          </h2>
          <span className="tnum t-meta text-[12px]">run #{run.id}</span>
        </div>
        {v.blurb && <p className="t-meta mt-1 max-w-lg">{v.blurb}</p>}
      </div>
      <div className="text-right">
        <div className="t-caption">Invoice total</div>
        <div className="t-metric-sm tnum mt-0.5">{amount(total, cur)}</div>
      </div>
    </div>
  );
}

/** The two-pane content: document at left, decision evidence at right. No
 *  positioning of its own — the caller decides whether this is a full-screen
 *  overlay or an inline block on the page. */
export function ReviewWorkspaceBody({
  run,
  file,
  onReviewed,
  showPipeline = false,
}: {
  run: RunLike;
  /** The in-memory File, when this is the run just processed in this tab. */
  file?: File | null;
  onReviewed?: () => void;
  /**
   * Keep the stage list on screen after the run finishes.
   *
   * Set by the Process page, where the reader has just watched those nine
   * stages arrive one at a time -- and where, until this existed, the moment
   * the verdict landed this view replaced the pipeline panel and the whole
   * account of how the answer was reached disappeared from the screen.
   *
   * Off everywhere else, which today means the register's overlay: someone
   * opening an invoice from a list did not watch it run, and the audit trail
   * below is the better answer to "how was this decided" for them.
   */
  showPipeline?: boolean;
}) {
  const pm = run.po_match;
  const hasPo = !!pm?.po_number;

  // What the preview actually resolved, so the Download control offers the
  // bytes on screen instead of a link that might 404.
  const [doc, setDoc] = useState<ResolvedDocument>({ source: "none", url: null });

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)] xl:items-start">
      {/* --------------------------------------------------------- document */}
      <div className="xl:sticky xl:top-4">
        <Panel flush className="overflow-hidden">
          <div className="flex items-center gap-2 border-b border-line bg-sunken px-3 py-2">
            <IconFile size={12} className="shrink-0 text-faint" />
            <span className="t-caption">Source document</span>
            <span
              className="min-w-0 flex-1 truncate text-right text-[12px] text-faint"
              title={run.filename}
            >
              {run.filename}
            </span>
            <DocumentDownloadButton doc={doc} runId={run.id} filename={run.filename} />
          </div>
          <div className="h-[70vh] min-h-[420px] xl:h-[calc(100vh-180px)]">
            <DocumentPreview
              file={file}
              filename={run.filename}
              runId={run.id}
              onResolved={setDoc}
            />
          </div>
        </Panel>
      </div>

      {/* --------------------------------------------------------- decision */}
      <div className="flex flex-col gap-4">
        <VerdictBanner run={run} />

        <div className="flex items-center justify-end">
          <AuditExportButtons runId={run.id} />
        </div>

        {run.status === "REJECTED" && <RejectionNotice runId={run.id} />}

        {run.audit && run.audit.automated_decision !== "APPROVED" && (
          <Panel>
            <PanelHeader
              title="Why this needs attention"
              description="What to check, and where to look, before you decide."
            />
            <div className="mt-3.5">
              <ReviewerBrief audit={run.audit} extracted={run.extracted} />
            </div>
          </Panel>
        )}

        {/* The nine stages, exactly as they streamed. Placed after the verdict
            and whatever has to be acted on, and before the detail panels: it
            answers "what did it actually do", which is a different question
            from the audit trail's "what was the decision computed from". */}
        {showPipeline && (
          <Panel>
            <PanelHeader
              title="Pipeline"
              description="Every stage this invoice went through, with what each one found."
              actions={
                /* The count against the pipeline's own length, the way the
                   live panel reads it -- a run that stopped early says so,
                   rather than reporting "7 of 7" and sounding complete. */
                <Badge tone="neutral">
                  {run.stages.length} of {STAGE_ORDER.length}
                </Badge>
              }
            />
            <div className="mt-3.5">
              <StageList stages={run.stages} running={false} />
            </div>
          </Panel>
        )}

        {hasPo && (
          <Panel>
            <PanelHeader
              title="Purchase order match"
              description="Each row is decided by the rule engine, not compared in the browser."
            />
            <div className="mt-3.5">
              <MatchTable pm={pm!} audit={run.audit} />
            </div>
            <div className="mt-4 border-t border-line pt-4">
              <PoBudget pm={pm!} />
            </div>
          </Panel>
        )}

        {run.extracted && (
          <Panel>
            <PanelHeader title="Extracted values" />
            <div className="mt-3.5 flex flex-col gap-3.5">
              <ExtractionSummary e={run.extracted} />
              <div className="border-t border-line pt-1">
                <ExtractedFields e={run.extracted} audit={run.audit} />
              </div>
            </div>
          </Panel>
        )}

        <Panel>
          <PanelHeader title="Validation" description="Every check the pipeline ran, in order." />
          <div className="mt-3.5">
            <ReasonList reasons={run.reasons} />
          </div>
        </Panel>

        <Panel>
          <PanelHeader title="Audit trail" description="Exactly what the decision was computed from." />
          <div className="mt-3.5">
            <AuditTrail audit={run.audit} run={run} />
          </div>
        </Panel>

        {/* Sticky so the decision is reachable without hunting for it after
            scrolling through a long trail — the one control on this screen
            that must never require a scroll to find.

            Deliberately NO negative horizontal margin: this block renders both
            inside the overlay and inline on the Process page, whose <main> has
            no padding to pull back into. Widening it there is exactly what put
            a horizontal scrollbar on the whole document once before. */}
        <div className="sticky bottom-0 z-10 border-t border-line bg-canvas/95 pt-3 pb-2 backdrop-blur-sm">
          <ReviewBar
            runId={run.id}
            filename={run.filename}
            automatedDecision={run.automated_decision ?? run.status}
            humanDecision={run.human_decision}
            onReviewed={onReviewed}
          />
        </div>
      </div>
    </div>
  );
}

/**
 * Full-screen overlay: the workspace opened from the invoice register.
 *
 * Deliberately not a centered dialog (Modal) — a split document/decision view
 * needs the whole viewport, not a card in the middle of one. Escape and the
 * close button both return to the register; Previous/Next are optional and
 * only appear when the caller has a list to walk (the register does; a
 * single freshly-processed run does not).
 */
export default function ReviewWorkspace({
  run,
  file,
  onClose,
  onReviewed,
  onPrev,
  onNext,
}: {
  run: RunLike | null;
  file?: File | null;
  onClose: () => void;
  onReviewed?: () => void;
  onPrev?: () => void;
  onNext?: () => void;
}) {
  useEffect(() => {
    if (!run) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
    };
  }, [run, onClose]);

  if (!run) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Review ${run.filename}`}
      className="rise fixed inset-0 z-50 flex flex-col bg-canvas"
    >
      <div className="flex shrink-0 items-center gap-3 border-b border-line bg-surface px-4 py-2.5">
        <Button
          variant="ghost"
          size="sm"
          onClick={onClose}
          aria-label="Close review workspace"
          icon={<IconX size={15} />}
        />
        <span className="t-meta hidden sm:inline">Invoices</span>
        <span className="t-meta hidden text-faint sm:inline">/</span>
        {/* The vendor and the invoice number are how this document is referred
            to in an AP conversation. The upload filename is how it arrived —
            still on screen, on the document pane below, but not the headline. */}
        <span className="min-w-0 leading-tight">
          <span className="block truncate text-[14px] font-semibold">
            {run.audit?.invoice?.vendor || run.extracted?.vendor_name || run.filename}
          </span>
          <span className="tnum block truncate text-[12px] text-faint">
            {run.audit?.invoice?.invoice_number || run.extracted?.invoice_number || "no invoice number"}
            {" · run #"}
            {run.id}
          </span>
        </span>
        <StatusBadge status={run.status} />
        <div className="flex-1" />
        {(onPrev || onNext) && (
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={onPrev}
              disabled={!onPrev}
              aria-label="Previous invoice"
              icon={<IconChevronLeft size={14} />}
            />
            <Button
              variant="ghost"
              size="sm"
              onClick={onNext}
              disabled={!onNext}
              aria-label="Next invoice"
              icon={<IconChevronRight size={14} />}
            />
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
        <div className="mx-auto max-w-[1400px]">
          <ReviewWorkspaceBody run={run} file={file} onReviewed={onReviewed} />
        </div>
      </div>
    </div>
  );
}
