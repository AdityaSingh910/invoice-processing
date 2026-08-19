"use client";

/**
 * The full read-out for one run.
 *
 * Shared by the live processing screen and the invoice detail dialog so a run
 * looks identical whether you are watching it happen or opening it a week
 * later. Two renderings of the same evidence would eventually disagree.
 */
import { Panel, PanelHeader } from "@/components/ui";
import type { Audit, Extracted, PoMatch, Reason, Stage } from "@/lib/types";
import AuditTrail from "./AuditTrail";
import ReviewBar from "./ReviewBar";
import StageList from "./StageList";
import { MatchTable, PoBudget } from "./PoMatchPanel";
import { ExtractedFields, ExtractionSummary, ReasonList } from "./Panels";

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

export default function RunDetail({
  run,
  onReviewed,
  showStages = true,
}: {
  run: RunLike;
  onReviewed?: () => void;
  showStages?: boolean;
}) {
  const pm = run.po_match;
  const hasPo = !!pm?.po_number;

  return (
    <div className="flex flex-col gap-4">
      {hasPo && (
        <Panel>
          <PanelHeader
            title="Three-way match"
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

      <div className="grid items-start gap-4 xl:grid-cols-2">
        <Panel>
          <PanelHeader title="Why this outcome" />
          <div className="mt-3.5">
            <ReasonList reasons={run.reasons} />
          </div>
        </Panel>

        {run.extracted && (
          <Panel>
            <PanelHeader title="Extracted data" />
            <div className="mt-3.5 flex flex-col gap-3.5">
              <ExtractionSummary e={run.extracted} />
              <div className="border-t border-line pt-1">
                <ExtractedFields e={run.extracted} />
              </div>
            </div>
          </Panel>
        )}
      </div>

      {showStages && (
        <Panel>
          <PanelHeader
            title="Pipeline"
            description="Stages never short-circuit — every check runs, and only the last one judges."
          />
          <div className="mt-3.5">
            <StageList stages={run.stages} running={false} />
          </div>
        </Panel>
      )}

      <Panel>
        <PanelHeader title="Audit trail" description="Exactly what the decision was computed from." />
        <div className="mt-3.5">
          <AuditTrail audit={run.audit} run={run} />
        </div>
        <div className="mt-4">
          <ReviewBar
            runId={run.id}
            filename={run.filename}
            automatedDecision={run.automated_decision ?? run.status}
            humanDecision={run.human_decision}
            onReviewed={onReviewed}
          />
        </div>
      </Panel>
    </div>
  );
}
