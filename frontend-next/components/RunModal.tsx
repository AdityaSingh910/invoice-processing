"use client";

/** A stored run, opened from the dashboard: the same evidence the run view
 *  shows, plus the stages as they were recorded at the time. */
import { useEffect } from "react";
import { money, when } from "@/lib/format";
import type { RunRecord } from "@/lib/types";
import { StatusPill } from "./ui";
import { StageRow } from "./StageList";
import ReasonList from "./ReasonList";
import AuditTrail from "./AuditTrail";
import ReviewBar from "./ReviewBar";

export default function RunModal({
  run,
  onClose,
  onReviewed,
}: {
  run: RunRecord;
  onClose: () => void;
  onReviewed?: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      onClick={(e) => e.target === e.currentTarget && onClose()}
      className="fixed inset-0 z-40 overflow-y-auto bg-black/50 p-4 backdrop-blur-sm"
    >
      <div className="mx-auto my-8 w-full max-w-3xl rounded-[var(--radius-card)] border border-border bg-panel p-5 shadow-[var(--shadow-card)]">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2.5">
              <h2 className="text-lg font-semibold break-words">{run.filename}</h2>
              <StatusPill status={run.status} />
            </div>
            <div className="mt-0.5 text-dim">
              run #{run.id} · {run.vendor_name || "unknown vendor"} · {money(run.total)} ·{" "}
              {when(run.created_at)}
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 rounded-lg border border-border px-2.5 py-1 text-dim transition hover:border-border-strong"
          >
            ✕
          </button>
        </div>

        <Section title="Reasoning" />
        <ReasonList reasons={run.reasons} />

        <Section title="Decision details" />
        <AuditTrail audit={run.audit} run={run} />
        <ReviewBar
          runId={run.id}
          automatedDecision={run.automated_decision ?? run.status}
          humanDecision={run.human_decision}
          onReviewed={() => {
            onReviewed?.();
            onClose();
          }}
        />

        <Section title="Stages" />
        <div>
          {run.stages.map((s, i) => (
            <StageRow key={`${s.name}-${i}`} stage={s} index={i} />
          ))}
        </div>
      </div>
    </div>
  );
}

const Section = ({ title }: { title: string }) => (
  <div className="mt-5 mb-2 border-t border-border pt-4 text-[11px] font-semibold tracking-wider text-faint uppercase">
    {title}
  </div>
);
