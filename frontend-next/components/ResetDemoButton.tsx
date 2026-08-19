"use client";

/**
 * Clear processed-run history.
 *
 * WHY THE APP NEEDS THIS AT ALL
 *
 * The sample invoices are deliberately history-dependent — the split-PO story
 * only works as 02 → 03 → 03b, and 06 is only a duplicate because 01 ran first.
 * Every run is recorded, so a second pass turns the happy path into a duplicate
 * of itself and leaves PO-1001 with no budget. The engine is still right; the
 * samples just stop demonstrating what they were written to demonstrate. Before
 * this control, recovering meant deleting a file on the server.
 *
 * Admin only, and it confirms first: it is destructive, even though what it
 * destroys is reproducible by re-running an invoice.
 */
import { useState } from "react";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Button, Callout } from "@/components/ui";
import Modal from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { IconAlert, IconRefresh } from "@/components/ui/icons";

export default function ResetDemoButton({
  onReset,
  size = "sm",
}: {
  onReset?: () => void;
  size?: "xs" | "sm" | "md";
}) {
  const { can } = useAuth();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The server enforces the same scope; hiding it only spares a 403.
  if (!can("invoice:admin")) return null;

  async function reset() {
    setBusy(true);
    setError(null);
    try {
      const res = await apiFetch("/api/admin/reset-demo", { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        setError(body?.detail || `The reset failed (HTTP ${res.status}).`);
        return;
      }
      setOpen(false);
      toast.push({
        tone: "ok",
        title: "Run history cleared",
        detail:
          body.deleted === 0
            ? "There was nothing to clear."
            : `${body.deleted} run${body.deleted === 1 ? "" : "s"} removed. The samples will behave as documented again.`,
      });
      onReset?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The server could not be reached.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button size={size} onClick={() => setOpen(true)} icon={<IconRefresh size={13} />}>
        Reset demo data
      </Button>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        size="sm"
        title="Clear processed-run history?"
        footer={
          <>
            <Button variant="secondary" size="sm" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button variant="danger" size="sm" onClick={reset} loading={busy}>
              Clear history
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <p className="t-body text-muted">
            This removes every processed invoice from the dashboard and returns each purchase
            order to its full authorised balance, so the sample invoices can be run through from
            the beginning again.
          </p>
          <Callout tone="neutral">
            Purchase orders, vendors and user accounts are not touched — they are seed data and
            are reloaded on every start. Nothing here is lost that re-running an invoice cannot
            rebuild.
          </Callout>
          {error && (
            <Callout tone="bad" icon={<IconAlert size={13} />} title="Not cleared">
              {error}
            </Callout>
          )}
        </div>
      </Modal>
    </>
  );
}
