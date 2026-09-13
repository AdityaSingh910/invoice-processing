"use client";

/**
 * Email review queue -- the missing product surface for Phase F/G's
 * verification and ingestion endpoints.
 *
 * WHY THIS PAGE EXISTS
 *
 * Phase F already separates a SECURITY FINDING (`classification`: VERIFIED /
 * FAILED / SUSPICIOUS / UNVERIFIED) from a PROCESSING STATE (`status`:
 * ADMITTED / QUARANTINED / RELEASED / DISCARDED), and Phase G already lets a
 * reviewer release a quarantined message and run it through the same
 * invoice pipeline every other door uses -- see `POST
 * /api/email/messages/{id}/release` and `POST
 * /api/email/messages/{id}/process` in backend/main.py. That flow has always
 * worked. What did not exist was anywhere in the browser to use it: the
 * Settings screen shows a COUNT of held messages, with nothing to click. A
 * message with no cryptographic evidence -- which is the normal, expected
 * shape of an invoice sent from ordinary consumer webmail (Gmail, Outlook,
 * Yahoo, ...), not a sign of anything hostile -- was therefore a dead end in
 * practice, even though the API behind it was correct.
 *
 * This page is that missing surface. It changes NOTHING about admission or
 * quarantine policy -- every action here calls an endpoint that already
 * existed, already re-checks the caller's scope server-side, and already
 * enforces the same rules it always did. It just makes the human-review path
 * something a reviewer can actually drive.
 *
 * WHAT IS SHOWN, AND WHY IT IS TWO BADGES, NEVER ONE
 *
 * `classification` and `status` are rendered as two separate badges,
 * deliberately never merged into one word. Collapsing them would blur "why
 * this was held" (a security finding) with "what happens to it next" (a
 * process state) -- exactly the mixing Phase F and Phase G were built to
 * keep apart. A FAILED message and an UNVERIFIED one are both QUARANTINED,
 * and they mean very different things: one is a real authentication problem,
 * the other is "nothing could be checked", which is the ordinary condition
 * of consumer webmail and is never printed as an accusation.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { apiFetch, apiJson, ApiError } from "@/lib/api";
import { when } from "@/lib/format";
import { useAuth } from "@/lib/auth";
import type {
  EmailAttachment,
  EmailMessageDetail,
  EmailMessageSummary,
  EmailProcessResult,
  EmailReleaseResult,
  EmailStatus,
} from "@/lib/types";
import {
  Badge,
  Button,
  Callout,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  Input,
  KeyValues,
  Panel,
  PanelHeader,
  Segmented,
  Spinner,
  StatusBadge,
  TD,
  TH,
  type Tone,
} from "@/components/ui";
import Modal from "@/components/ui/Modal";
import { IconMail } from "@/components/ui/icons";

/**
 * The tabs a reviewer actually works, which are NOT one-per-database-status.
 *
 * `ADMITTED` and `RELEASED` are two ways of reaching one outcome -- the
 * message was allowed to become an invoice, either by policy at ingestion or
 * by a person afterwards -- so they are one tab. Splitting them would ask the
 * reader to care about which door a message came through before they can find
 * it, which is a fact about our plumbing rather than about their work.
 *
 * `BLOCKED` is `QUARANTINED` renamed. With auto-admission on (see
 * `EMAIL_AUTO_ADMIT_UNVERIFIED`), an unauthenticated message no longer waits
 * here, so the only things left are real findings: a signature that did not
 * verify, or a structurally spoofed sender. "Held for review" undersold that
 * and made the ordinary case sound alarming; "Blocked" says what it is.
 */
type Filter = "ADMITTED" | "DISCARDED" | "BLOCKED" | "ALL";

const FILTER_MATCHES: Record<Exclude<Filter, "ALL">, readonly EmailStatus[]> = {
  ADMITTED: ["ADMITTED", "RELEASED"],
  DISCARDED: ["DISCARDED"],
  BLOCKED: ["QUARANTINED"],
};

const CLASSIFICATION_TONE: Record<string, Tone> = {
  VERIFIED: "ok",
  FAILED: "bad",
  SUSPICIOUS: "warn",
  UNVERIFIED: "warn",
};

const CLASSIFICATION_WORD: Record<string, string> = {
  VERIFIED: "Verified",
  FAILED: "Failed",
  SUSPICIOUS: "Suspicious",
  UNVERIFIED: "Unverified",
};

const STATUS_TONE: Record<string, Tone> = {
  ADMITTED: "ok",
  RELEASED: "ok",
  QUARANTINED: "warn",
  DISCARDED: "neutral",
};

const STATUS_WORD: Record<string, string> = {
  ADMITTED: "Admitted",
  RELEASED: "Admitted (released by a person)",
  QUARANTINED: "Blocked",
  DISCARDED: "Discarded",
};

function dash(v: unknown): React.ReactNode {
  if (v === null || v === undefined || v === "") return <span className="text-faint">—</span>;
  return String(v);
}

function ClassificationBadge({ value }: { value: string | null }) {
  if (!value) return <Badge tone="neutral">—</Badge>;
  return (
    <Badge tone={CLASSIFICATION_TONE[value] ?? "neutral"} dot title="Security finding — how much of the sender's claimed origin could be proven">
      {CLASSIFICATION_WORD[value] ?? value}
    </Badge>
  );
}

function EligibilityBadge({ value }: { value: string | null }) {
  if (!value) return <Badge tone="neutral">—</Badge>;
  return (
    <Badge tone={STATUS_TONE[value] ?? "neutral"} title="Processing state — whether this message may reach the invoice pipeline">
      {STATUS_WORD[value] ?? value}
    </Badge>
  );
}

export default function EmailQueuePage({
  onRunCreated,
}: {
  /** Called after Process/Release & process actually creates a run, so the
   *  shared run list (Overview, Invoices, the review queue) picks it up
   *  without waiting for someone to press its own Refresh button. Every
   *  other door that creates a run already does this (ProcessPage's
   *  `onRan`, ReviewBar's `onReviewed`); this page's actions were the one
   *  gap, since its own docstring only reasoned about release/discard,
   *  which don't touch `runs` at all. */
  onRunCreated?: () => void;
}) {
  const { user, can } = useAuth();
  const [filter, setFilter] = useState<Filter>("ADMITTED");
  const [all, setAll] = useState<EmailMessageSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  /** One fetch, grouped in the browser. A tab here is several statuses (see
   *  FILTER_MATCHES), which `?status_filter=` takes one of -- and at this
   *  volume the whole list is one small response, so switching tabs costs
   *  nothing and cannot show a half-stale mix of two fetches. */
  const load = useCallback(async () => {
    setError(null);
    try {
      setAll(await apiJson<EmailMessageSummary[]>("/api/email/messages"));
    } catch {
      setError("Could not load the email queue.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void load();
  }, [load]);

  const messages = useMemo(() => {
    if (!all) return null;
    if (filter === "ALL") return all;
    const wanted = FILTER_MATCHES[filter];
    return all.filter((m) => m.status && wanted.includes(m.status as EmailStatus));
  }, [all, filter]);

  const countFor = useCallback(
    (f: Filter) =>
      !all
        ? 0
        : f === "ALL"
          ? all.length
          : all.filter((m) => m.status && FILTER_MATCHES[f].includes(m.status as EmailStatus))
              .length,
    [all]
  );

  const refresh = useCallback(() => {
    void load();
  }, [load]);

  const canAct = can("invoice:review") || can("invoice:process");

  return (
    <div className="mx-auto flex max-w-[1100px] flex-col gap-4 p-4 sm:p-6">
      <PanelHeader
        title="Email queue"
        description="Every message the connected mailbox has evaluated, and what each one became. Invoices are processed on arrival; only a message that failed a security check waits for a person."
      />

      {!canAct && (
        <Callout tone="neutral">
          Your account can view this queue but cannot release, discard, or process a message —
          that needs a reviewer or administrator.
        </Callout>
      )}

      <Panel flush>
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3">
          <Segmented<Filter>
            ariaLabel="Filter by status"
            value={filter}
            onChange={setFilter}
            options={[
              { value: "ADMITTED", label: `Admitted (${countFor("ADMITTED")})` },
              { value: "DISCARDED", label: `Discarded (${countFor("DISCARDED")})` },
              { value: "BLOCKED", label: `Blocked (${countFor("BLOCKED")})` },
              { value: "ALL", label: "All" },
            ]}
          />
          <Button size="sm" onClick={refresh} disabled={loading}>
            {loading ? <Spinner size={12} /> : "Refresh"}
          </Button>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 p-8 text-[13px] text-muted">
            <Spinner /> Loading…
          </div>
        ) : error ? (
          <ErrorState description={error} onRetry={refresh} />
        ) : !messages || messages.length === 0 ? (
          <EmptyState
            icon={<IconMail size={18} />}
            title="Nothing here"
            description={
              filter === "BLOCKED"
                ? "Nothing is blocked. A message only lands here when a check actually failed — a signature that did not verify, or a sender that is structurally spoofed."
                : filter === "ADMITTED"
                  ? "No message has reached the invoice pipeline yet."
                  : filter === "DISCARDED"
                    ? "Nothing has been discarded."
                    : "No message has been received yet."
            }
          />
        ) : (
          <DataTable minWidth={820}>
            <thead>
              <tr>
                <TH>Received</TH>
                <TH>From</TH>
                <TH>Subject</TH>
                <TH>Security</TH>
                <TH>Status</TH>
                <TH>Invoice</TH>
              </tr>
            </thead>
            <tbody>
              {messages.map((m) => (
                <tr
                  key={m.id}
                  className="cursor-pointer hover:bg-hover"
                  onClick={() => setSelectedId(m.id)}
                >
                  <TD>{when(m.received_at)}</TD>
                  <TD className="max-w-[220px] truncate" title={m.from_address ?? undefined}>
                    {m.from_display_name || m.from_address || <span className="text-faint">unknown</span>}
                  </TD>
                  <TD className="max-w-[260px] truncate">{m.subject || <span className="text-faint">(no subject)</span>}</TD>
                  <TD>
                    <ClassificationBadge value={m.classification} />
                  </TD>
                  <TD>
                    <EligibilityBadge value={m.status} />
                  </TD>
                  <TD>
                    {m.run_id ? (
                      <span className="flex items-center gap-1.5">
                        <StatusBadge status={m.run_status} />
                        <span className="t-meta">#{m.run_id}</span>
                      </span>
                    ) : m.has_pdf_attachment ? (
                      <span className="text-faint">not processed</span>
                    ) : (
                      <span className="text-faint">no PDF</span>
                    )}
                  </TD>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Panel>

      {selectedId !== null && (
        <MessageDetail
          emailId={selectedId}
          onClose={() => setSelectedId(null)}
          onChanged={refresh}
          onRunCreated={onRunCreated}
          canRelease={can("invoice:review")}
          canProcess={can("invoice:process")}
          viewer={user?.username ?? null}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ detail */

function MessageDetail({
  emailId,
  onClose,
  onChanged,
  onRunCreated,
  canRelease,
  canProcess,
  viewer,
}: {
  emailId: number;
  onClose: () => void;
  onChanged: () => void;
  onRunCreated?: () => void;
  canRelease: boolean;
  canProcess: boolean;
  viewer: string | null;
}) {
  const [detail, setDetail] = useState<EmailMessageDetail | null>(null);
  const [attachments, setAttachments] = useState<EmailAttachment[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"release" | "discard" | "process" | "release+process" | null>(
    null
  );
  const [notice, setNotice] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [msg, atts] = await Promise.all([
        apiJson<EmailMessageDetail>(`/api/email/messages/${emailId}`),
        apiJson<{ attachments: EmailAttachment[] }>(`/api/email/messages/${emailId}/attachments`),
      ]);
      setDetail(msg);
      setAttachments(atts.attachments);
    } catch {
      setError("Could not load this message.");
    } finally {
      setLoading(false);
    }
  }, [emailId]);

  useEffect(() => {
    setLoading(true);
    void load();
  }, [load]);

  async function release(): Promise<boolean> {
    const res = await apiFetch(`/api/email/messages/${emailId}/release`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: note.trim() || null }),
    });
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      setNotice({ tone: "bad", text: body.detail || "Could not release this message." });
      return false;
    }
    (await res.json()) as EmailReleaseResult;
    return true;
  }

  async function discard() {
    setBusy("discard");
    setNotice(null);
    try {
      const res = await apiFetch(`/api/email/messages/${emailId}/discard`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: note.trim() || null }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        setNotice({ tone: "bad", text: body.detail || "Could not discard this message." });
        return;
      }
      setNotice({ tone: "ok", text: "Discarded. It can never become an invoice." });
      await load();
      onChanged();
    } finally {
      setBusy(null);
    }
  }

  async function process() {
    setBusy("process");
    setNotice(null);
    try {
      const res = await apiFetch(`/api/email/messages/${emailId}/process`, { method: "POST" });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        setNotice({ tone: "bad", text: body.detail || "Could not process this message." });
        return;
      }
      const result = (await res.json()) as EmailProcessResult;
      setNotice({
        tone: "ok",
        text:
          result.runs.length > 0
            ? `Processed. ${result.runs.length === 1 ? "Invoice run" : "Invoice runs"} #${result.runs.join(", #")} created.`
            : "Processed — no invoice-shaped attachment produced a run.",
      });
      await load();
      onChanged();
      if (result.runs.length > 0) onRunCreated?.();
    } finally {
      setBusy(null);
    }
  }

  async function releaseOnly() {
    setBusy("release");
    setNotice(null);
    try {
      const ok = await release();
      if (ok) {
        setNotice({ tone: "ok", text: "Released. Process it when you are ready." });
        await load();
        onChanged();
      }
    } finally {
      setBusy(null);
    }
  }

  async function releaseAndProcess() {
    setBusy("release+process");
    setNotice(null);
    try {
      // Two separately-authorized, separately-audited calls, one after the
      // other -- a convenience for the reviewer, not a new endpoint and not a
      // change to what /release means. See the module docstring.
      const ok = await release();
      if (!ok) return;
      const res = await apiFetch(`/api/email/messages/${emailId}/process`, { method: "POST" });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        setNotice({
          tone: "bad",
          text: `Released, but processing failed: ${body.detail || "unknown error"}`,
        });
        await load();
        onChanged();
        return;
      }
      const result = (await res.json()) as EmailProcessResult;
      setNotice({
        tone: "ok",
        text:
          result.runs.length > 0
            ? `Released and processed. ${result.runs.length === 1 ? "Invoice run" : "Invoice runs"} #${result.runs.join(", #")} created.`
            : "Released and processed — no invoice-shaped attachment produced a run.",
      });
      await load();
      onChanged();
      if (result.runs.length > 0) onRunCreated?.();
    } finally {
      setBusy(null);
    }
  }

  const quarantined = detail?.status === "QUARANTINED";
  const canFollowUp = detail?.status === "ADMITTED" || detail?.status === "RELEASED";

  return (
    <Modal
      open
      onClose={onClose}
      size="lg"
      title={detail?.subject || "(no subject)"}
      description={detail ? `${detail.from_display_name || detail.from_address || "unknown sender"} · ${when(detail.received_at)}` : undefined}
      footer={
        detail && (
          <div className="flex flex-wrap items-center gap-1.5">
            {quarantined && canRelease && (
              <Button variant="danger" size="sm" loading={busy === "discard"} onClick={discard}>
                Discard
              </Button>
            )}
            {quarantined && canRelease && (
              <Button size="sm" loading={busy === "release"} onClick={releaseOnly}>
                Release
              </Button>
            )}
            {quarantined && canRelease && canProcess && (
              <Button
                variant="primary"
                size="sm"
                loading={busy === "release+process"}
                onClick={releaseAndProcess}
              >
                Release &amp; process
              </Button>
            )}
            {canFollowUp && canProcess && (
              <Button variant="primary" size="sm" loading={busy === "process"} onClick={process}>
                Process attachments
              </Button>
            )}
          </div>
        )
      }
    >
      {loading ? (
        <div className="flex items-center gap-2 py-6 text-[13px] text-muted">
          <Spinner /> Loading…
        </div>
      ) : error || !detail ? (
        <ErrorState description={error ?? "Message not found."} onRetry={load} />
      ) : (
        <div className="flex flex-col gap-4">
          {notice && (
            <Callout tone={notice.tone} title={notice.tone === "ok" ? "Done" : "Attention"}>
              {notice.text}
            </Callout>
          )}

          <div className="flex flex-wrap items-center gap-1.5">
            <ClassificationBadge value={detail.classification} />
            <EligibilityBadge value={detail.status} />
            {detail.trusted_sender && <Badge tone="ok">On trusted-sender list</Badge>}
          </div>

          {/* THE REASONS, INCLUDING THE SENDER-CONTEXT SENTENCE (b70c9b3) when
              present -- the server already appends it here, so it is shown
              exactly where a reviewer is looking, not paraphrased. */}
          {detail.reasons.length > 0 && (
            <Callout
              tone={
                detail.classification === "FAILED"
                  ? "bad"
                  : detail.classification === "VERIFIED"
                    ? "ok"
                    : "warn"
              }
              title={
                detail.classification === "FAILED"
                  ? "Blocked — authentication failed"
                  : detail.classification === "SUSPICIOUS"
                    ? "Held — signals disagree"
                    : detail.classification === "VERIFIED"
                      ? "Authenticated"
                      : "Held — authentication unavailable"
              }
            >
              <ul className="list-disc space-y-1 pl-4">
                {detail.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
              {detail.classification === "UNVERIFIED" && !detail.audit?.sender_context && (
                <p className="mt-2 text-faint">
                  This is a gap in what could be checked, not a failed check — it is not evidence
                  the message is hostile.
                </p>
              )}
            </Callout>
          )}

          {detail.audit?.sender_context && (
            <Panel>
              <p className="t-meta mb-2 font-medium text-fg">Sender context</p>
              <KeyValues
                rows={[
                  ["Sender type", dash(detail.audit.sender_context.sender_type)],
                  ["Trust status", dash(detail.audit.sender_context.trust_status)],
                  ["Matches vendor", dash(detail.audit.sender_context.vendor_name)],
                ]}
              />
            </Panel>
          )}

          <Panel>
            <p className="t-meta mb-2 font-medium text-fg">Authentication evidence</p>
            <KeyValues
              rows={[
                ["From", dash(detail.from_address)],
                ["Domain", dash(detail.from_domain)],
                ["SPF", dash(detail.spf_result)],
                ["DKIM", dash(detail.dkim_result)],
                ["DMARC", dash(detail.dmarc_result)],
                ["DMARC aligned", detail.dmarc_aligned ? "Yes" : "No"],
                ["Digital signature", dash(detail.signature_result)],
                ["Sender type (triage)", dash(detail.sender_type)],
                ["Trust status (triage)", dash(detail.trust_status)],
                ["Relevance", dash(detail.relevance)],
              ]}
            />
          </Panel>

          <Panel flush>
            <p className="t-meta px-4 pt-3 font-medium text-fg">
              Attachments {attachments ? `(${attachments.length})` : ""}
            </p>
            {!attachments || attachments.length === 0 ? (
              <EmptyState compact title="No attachments recorded" />
            ) : (
              <DataTable minWidth={520} className="mt-2">
                <thead>
                  <tr>
                    <TH>File</TH>
                    <TH>Status</TH>
                    <TH>Result</TH>
                  </tr>
                </thead>
                <tbody>
                  {attachments.map((a) => (
                    <tr key={a.id}>
                      <TD>{a.filename || <span className="text-faint">(unnamed)</span>}</TD>
                      <TD>{a.status}</TD>
                      <TD>
                        {a.run_id ? (
                          <span>
                            Run #{a.run_id}
                            {a.run_status ? ` — ${a.run_status}` : ""}
                          </span>
                        ) : a.error ? (
                          <span className="text-bad">{a.error}</span>
                        ) : a.skip_reason ? (
                          <span className="text-faint">{a.skip_reason}</span>
                        ) : (
                          <span className="text-faint">—</span>
                        )}
                      </TD>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
          </Panel>

          {(quarantined || canFollowUp) && (canRelease || canProcess) && (
            <Field
              label="Note (optional)"
              hint="Recorded on the release/discard, next to your name, in this message's history."
            >
              <Input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder={viewer ? `Recorded as ${viewer}` : undefined}
                maxLength={500}
              />
            </Field>
          )}

          <Panel flush>
            <p className="t-meta px-4 pt-3 font-medium text-fg">History</p>
            <div className="flex flex-col gap-1 px-4 py-3">
              {detail.activity.length === 0 ? (
                <p className="t-meta">Nothing recorded yet.</p>
              ) : (
                detail.activity.map((e) => (
                  <div key={e.id} className="flex items-baseline justify-between gap-3 py-1 text-[12.5px]">
                    <span className="text-fg">
                      {e.event_type.replace(/_/g, " ").toLowerCase()}
                      {e.actor ? ` — ${e.actor}` : ""}
                    </span>
                    <span className="t-meta shrink-0">{when(e.created_at)}</span>
                  </div>
                ))
              )}
            </div>
          </Panel>
        </div>
      )}
    </Modal>
  );
}
