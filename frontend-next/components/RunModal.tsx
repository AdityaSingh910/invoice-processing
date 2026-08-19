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
      className="fixed inset-0 z-40 overflow-y-auto bg-black/60 p-4 backdrop-blur-md"
    >
      <div className="card mx-auto my-8 w-full max-w-3xl p-6 shadow-[var(--shadow-float)]">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2.5">
              <h2 className="text-[19px] font-bold tracking-[-0.02em] break-words">{run.filename}</h2>
              <StatusPill status={run.status} />
            </div>
            <div className="mt-1 text-[14px] text-dim">
              run #{run.id} · {run.vendor_name || "unknown vendor"} · {money(run.total)} ·{" "}
              {when(run.created_at)}
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="btn btn-ghost shrink-0 px-2.5 py-1.5"
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
  <div className="eyebrow mt-6 mb-3 border-t border-border pt-5">{title}</div>
);
