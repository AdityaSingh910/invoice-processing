"use client";

/**
 * ACCEPT / REJECT, offered only for a run the rules held for review and that
 * nobody has ruled on yet -- matching what the API will actually allow.
 *
 * Reviewer identity is NOT collected here. The server takes it from the bearer
 * token and ignores anything the client claims about who is acting, so a name
 * box would be a field that silently does nothing.
 */
import { useState } from "react";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { humanise } from "@/lib/format";

interface Props {
  runId: number;
  automatedDecision?: string | null;
  humanDecision?: string | null;
  onReviewed?: () => void;
}

export default function ReviewBar({ runId, automatedDecision, humanDecision, onReviewed }: Props) {
  const { can } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  // Only runs the rules HELD are eligible, only once, and only for a token that
  // carries the scope. Storage enforces all three; this just avoids a pointless
  // 403 and an action that would be refused.
  if (automatedDecision !== "NEEDS_REVIEW" || humanDecision) return null;
  if (!can("invoice:review")) return null;

  if (done) {
    return (
      <div
        className="mt-5 rounded-[var(--radius-inner)] border px-4 py-3 text-[14px]"
        style={{ borderColor: "var(--ok-border)", background: "var(--ok-soft)", color: "var(--ok)" }}
      >
        Recorded: <b>{humanise(done)}</b>
      </div>
    );
  }

  async function rule(decision: "ACCEPTED" | "REJECTED") {
    setBusy(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/runs/${runId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      const body = await res.json();
      if (!res.ok || !body.ok) {
        setError(body?.error || `review failed (HTTP ${res.status})`);
        return;
      }
      setDone(body.final_decision);
      onReviewed?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-5 rounded-[var(--radius-inner)] border border-border bg-panel2 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <b>Human review</b>
          <div className="text-[14px] text-dim">
            Check the evidence above, then record a decision. The automated verdict is kept either
            way.
          </div>
        </div>
        <div className="flex gap-2">
          <button
            disabled={busy}
            onClick={() => rule("ACCEPTED")}
            className="btn px-4 py-2 text-white disabled:opacity-50"
            style={{ background: "var(--ok-solid)" }}
          >
            Accept
          </button>
          <button
            disabled={busy}
            onClick={() => rule("REJECTED")}
            className="btn px-4 py-2 text-white disabled:opacity-50"
            style={{ background: "var(--fail-solid)" }}
          >
            Reject
          </button>
        </div>
      </div>
      {error && (
        <div className="mt-2" style={{ color: "var(--fail)" }}>
          {error}
        </div>
      )}
    </div>
  );
}
