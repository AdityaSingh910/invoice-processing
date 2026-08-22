"use client";

/**
 * Rejection notification -- tell the vendor why their invoice was rejected.
 *
 * Renders only for a REJECTED run (the caller checks). Nothing here sends
 * anything on its own: opening the composer fetches a PREVIEW
 * (GET .../rejection-email), and sending is a second, explicit, separately
 * confirmed step (POST .../rejection-email/send) -- the same
 * compose-then-confirm split the backend documents for the same reason
 * Phase G's email release/process split exists (a person takes
 * responsibility for the send, not a click that happens to also compose one).
 *
 * The reasons shown here are the same vendor-safe sentences
 * `portal.client_state()` already produces for the supplier portal (§7g.6) --
 * this panel is the reviewer's PREVIEW of exactly what the vendor would read,
 * not a second, differently-worded account of the rejection.
 */
import { useEffect, useState } from "react";
import { apiFetch, apiJson, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useToast } from "@/components/ui/Toast";
import type {
  RejectionEmailPreview,
  RejectionEmailSendResult,
} from "@/lib/types";
import {
  Badge,
  Button,
  Callout,
  Field,
  Input,
  Panel,
  PanelHeader,
  Spinner,
  Textarea,
} from "@/components/ui";
import Modal from "@/components/ui/Modal";
import { IconAlert, IconCheck, IconMail } from "@/components/ui/icons";
import { when } from "@/lib/format";

export default function RejectionNotice({ runId }: { runId: number }) {
  const { can } = useAuth();
  const toast = useToast();

  const [preview, setPreview] = useState<RejectionEmailPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [recipient, setRecipient] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [resendConfirm, setResendConfirm] = useState(false);

  const load = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const data = await apiJson<RejectionEmailPreview>(
        `/api/runs/${runId}/rejection-email`
      );
      setPreview(data);
    } catch {
      setLoadError("Could not load the rejection notice.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  function openComposer() {
    if (!preview) return;
    setRecipient(preview.draft.recipient ?? "");
    setSubject(preview.draft.subject);
    setBody(preview.draft.body);
    setSendError(null);
    setResendConfirm(false);
    setOpen(true);
  }

  async function send(force: boolean) {
    setSending(true);
    setSendError(null);
    try {
      const res = await apiFetch(`/api/runs/${runId}/rejection-email/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recipient, subject, body, force }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (res.status === 409 && data?.previous && !force) {
          setResendConfirm(true);
          return;
        }
        setSendError(
          (typeof data?.detail === "string" && data.detail) ||
            data?.detail?.error ||
            `The rejection email could not be sent (HTTP ${res.status}).`
        );
        return;
      }
      const result = data as RejectionEmailSendResult;
      toast.push({
        tone: "ok",
        title: "Rejection email sent",
        detail: `To ${result.recipient}, run #${runId}.`,
      });
      setOpen(false);
      await load();
    } catch (e) {
      setSendError(e instanceof Error ? e.message : "The server could not be reached.");
      toast.push({ tone: "bad", title: "Rejection email not sent" });
    } finally {
      setSending(false);
    }
  }

  if (loading) {
    return (
      <Panel>
        <div className="flex items-center gap-2 py-2 text-[13.5px] text-muted">
          <Spinner size={13} /> Loading rejection details…
        </div>
      </Panel>
    );
  }

  if (loadError || !preview) {
    return (
      <Panel>
        <Callout tone="bad" icon={<IconAlert size={13} />}>
          {loadError ?? "The rejection notice could not be loaded."}
        </Callout>
      </Panel>
    );
  }

  const { draft, sender, already_sent, history } = preview;
  const lastSent = [...history].reverse().find((h) => h.event_type === "REJECTION_EMAIL_SENT");
  const canSend = can("invoice:review");

  return (
    <Panel>
      <PanelHeader
        title="Rejection details"
        description="What the vendor will be told, and whether they already have been."
        actions={<Badge tone="bad">Rejected</Badge>}
      />

      <div className="mt-3.5 flex flex-col gap-3">
        <div>
          <p className="t-caption mb-1.5">Reasons</p>
          {draft.reasons.length === 0 ? (
            <p className="t-meta">No vendor-facing reason is on file for this rejection.</p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {draft.reasons.map((r, i) => (
                <li key={i} className="flex gap-2 text-[13.5px] leading-snug">
                  <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-bad-vivid" />
                  <span>{r}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {!sender.available && (
          <Callout tone="warn" icon={<IconAlert size={13} />} title="Sending is not available">
            {sender.reason}
          </Callout>
        )}

        {already_sent && lastSent ? (
          <Callout tone="ok" icon={<IconCheck size={13} />} title="Rejection email sent">
            To: {lastSent.metadata?.recipient}
            <br />
            Sent: {when(lastSent.created_at)} by {lastSent.actor}
          </Callout>
        ) : (
          !draft.recipient && (
            <Callout tone="warn" icon={<IconAlert size={13} />} title="No known vendor email">
              This invoice has no vendor email on file. You can enter one below before sending.
            </Callout>
          )
        )}

        {canSend && (
          <div>
            <Button
              variant={already_sent ? "secondary" : "primary"}
              size="sm"
              icon={<IconMail size={13} />}
              onClick={openComposer}
              disabled={!sender.available}
            >
              {already_sent ? "Resend rejection email" : "Send rejection email"}
            </Button>
          </div>
        )}
      </div>

      <Modal
        open={open}
        onClose={() => !sending && setOpen(false)}
        size="lg"
        title={already_sent ? "Resend rejection email?" : "Send rejection email"}
        description={`Invoice ${draft.invoice_number ?? `run #${runId}`}${draft.vendor_name ? ` · ${draft.vendor_name}` : ""}`}
        footer={
          <>
            <Button variant="secondary" size="sm" onClick={() => setOpen(false)} disabled={sending}>
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              loading={sending}
              onClick={() => send(resendConfirm)}
              disabled={!recipient.trim() || !subject.trim() || !body.trim()}
            >
              {resendConfirm ? "Confirm resend" : "Confirm & send"}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          {resendConfirm && (
            <Callout tone="warn" title="A rejection email was already sent for this invoice">
              Sending again will notify the vendor a second time. Confirm to resend.
            </Callout>
          )}
          {sendError && (
            <Callout tone="bad" icon={<IconAlert size={13} />} title="Not sent">
              {sendError}
            </Callout>
          )}
          <Field label="Recipient">
            <Input
              type="email"
              value={recipient}
              onChange={(e) => setRecipient(e.target.value)}
              placeholder="vendor@example.com"
            />
          </Field>
          <Field label="Subject">
            <Input value={subject} onChange={(e) => setSubject(e.target.value)} />
          </Field>
          <Field label="Body" hint="Editable before sending.">
            <Textarea rows={12} value={body} onChange={(e) => setBody(e.target.value)} />
          </Field>
        </div>
      </Modal>
    </Panel>
  );
}
