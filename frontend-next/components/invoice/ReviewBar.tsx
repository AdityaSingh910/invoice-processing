"use client";

/**
 * Human accept / reject.
 *
 * Offered only for a run the rules HELD and that nobody has ruled on yet, and
 * only to a token carrying invoice:review — matching exactly what storage.py
 * will allow. Hiding it spares a pointless 403; it grants nothing, because the
 * server re-checks all three conditions.
 *
 * Reviewer identity is never collected here. The server takes it from the
 * bearer token and ignores anything the client claims about who is acting, so a
 * name field would be a control that silently does nothing.
 *
 * Rejecting asks for confirmation. Accepting does not: accepting is the
 * expected outcome of a review queue, and confirming every routine action
 * trains people to dismiss dialogs without reading them.
 */
import { useState } from "react";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Button, Callout } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import Modal from "@/components/ui/Modal";
import { IconAlert, IconCheck, IconX } from "@/components/ui/icons";

export default function ReviewBar({
  runId,
  automatedDecision,
  humanDecision,
  filename,
  onReviewed,
}: {
  runId: number;
  automatedDecision?: string | null;
  humanDecision?: string | null;
  filename?: string;
  onReviewed?: () => void;
}) {
  const { can } = useAuth();
  const toast = useToast();
  const [busy, setBusy] = useState<"ACCEPTED" | "REJECTED" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  if (automatedDecision !== "NEEDS_REVIEW" || humanDecision) return null;
  if (!can("invoice:review")) return null;

  async function rule(decision: "ACCEPTED" | "REJECTED") {
    setBusy(decision);
    setError(null);
    try {
      const res = await apiFetch(`/api/runs/${runId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        setError(body?.error || `The review could not be recorded (HTTP ${res.status}).`);
        return;
      }
      setDone(body.final_decision);
      setConfirming(false);
      toast.push({
        tone: decision === "ACCEPTED" ? "ok" : "bad",
        title: decision === "ACCEPTED" ? "Invoice accepted" : "Invoice rejected",
        detail: `Recorded against run #${runId}. The automated verdict is unchanged.`,
      });
      onReviewed?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The server could not be reached.");
    } finally {
      setBusy(null);
    }
  }

  if (done) {
    return (
      <Callout
        tone={done === "HUMAN_APPROVED" ? "ok" : "bad"}
        icon={<IconCheck size={13} />}
        title={`Recorded — ${done.replace(/_/g, " ").toLowerCase()}`}
      >
        The automated verdict is unchanged and kept on record beside your decision.
      </Callout>
    );
  }

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-md)] border border-line bg-sunken p-3">
        <div className="min-w-0">
          <p className="text-[12.5px] font-semibold">Your decision</p>
          <p className="t-meta">
            Review the evidence above, then accept or reject. The automated verdict is kept either
            way.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            variant="danger"
            size="sm"
            onClick={() => setConfirming(true)}
            disabled={!!busy}
            icon={<IconX size={13} />}
          >
            Reject
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => rule("ACCEPTED")}
            loading={busy === "ACCEPTED"}
            icon={busy === "ACCEPTED" ? undefined : <IconCheck size={13} />}
          >
            Accept
          </Button>
        </div>
      </div>

      {error && (
        <div className="mt-2">
          <Callout tone="bad" icon={<IconAlert size={13} />} title="Not recorded">
            {error}
          </Callout>
        </div>
      )}

      <Modal
        open={confirming}
        onClose={() => setConfirming(false)}
        size="sm"
        title="Reject this invoice?"
        description={filename}
        footer={
          <>
            <Button variant="secondary" size="sm" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              size="sm"
              onClick={() => rule("REJECTED")}
              loading={busy === "REJECTED"}
            >
              Reject invoice
            </Button>
          </>
        }
      >
        <p className="t-meta">
          This records your rejection against run #{runId} and releases any purchase-order budget it
          was holding. The automated verdict stays on record, and this decision cannot be changed
          afterwards — reversing it needs an administrator.
        </p>
      </Modal>
    </>
  );
}
