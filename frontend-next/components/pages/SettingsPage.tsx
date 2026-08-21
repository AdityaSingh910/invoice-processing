"use client";

/**
 * Email integration settings (Phase G2) — connecting a Gmail mailbox.
 *
 * WHAT THIS SCREEN IS FOR
 *
 * Before this page existed, connecting a mailbox meant putting an IMAP
 * password in a `.env` file and restarting the process. That is a bad way to
 * hold a credential and a worse way to ask a customer for one, and it is why
 * this page exists: an administrator hands Google their password, on Google's
 * own domain, and this application never sees it.
 *
 * THIS PAGE HANDLES NO SECRET, AND THAT IS STRUCTURAL RATHER THAN CAREFUL.
 *
 * The client secret lives in the server's environment. The refresh token lives
 * encrypted in the server's database. The `state` and the PKCE verifier are
 * generated server-side and are deliberately NOT returned by the authorize
 * call, because a CSRF token handed to client-side JavaScript is one an XSS
 * can read. What this component holds is a URL to navigate to and a status
 * object with no token-shaped field in it — there is nothing here to leak.
 *
 * The outcome of the round trip arrives as `?gmail=<result>` on the app's own
 * URL, from a fixed vocabulary the server controls, which is why it is safe to
 * switch on and render.
 */
import { useCallback, useEffect, useState } from "react";

import { apiFetch, apiJson, ApiError } from "@/lib/api";
import { when } from "@/lib/format";
import type {
  EmailIngestionStatus,
  GmailAuthorizeStart,
  GmailDisconnectResult,
  GmailStatus,
} from "@/lib/types";
import {
  Badge,
  Button,
  Callout,
  EmptyState,
  KeyValues,
  Panel,
  PanelHeader,
  Spinner,
} from "@/components/ui";
import { IconSettings } from "@/components/ui/icons";

/**
 * The outcomes the callback can redirect back with. Mirrors the server's
 * `_GMAIL_CALLBACK_RESULTS` — a CLOSED set on both sides, so nothing arbitrary
 * from Google can reach this switch, and an unknown value falls through to a
 * generic message rather than being rendered.
 */
const CALLBACK_MESSAGE: Record<string, { tone: "ok" | "warn" | "bad"; text: string }> = {
  connected: { tone: "ok", text: "Gmail is connected. Invoices arriving in that mailbox will be picked up automatically." },
  denied: { tone: "warn", text: "Authorization was cancelled at Google, so nothing was connected." },
  invalid_state: { tone: "bad", text: "That authorization could not be verified — it may have expired, or already been used. Start again." },
  exchange_failed: { tone: "bad", text: "Google would not complete the authorization. Check the OAuth client configuration and try again." },
  insufficient_scope: { tone: "bad", text: "The permission to read Gmail was not granted, so the mailbox could not be connected. Try again and leave the requested permission ticked." },
  no_refresh_token: { tone: "bad", text: "Google did not return a long-lived credential, so ingestion would have stopped within the hour. Remove this app at myaccount.google.com/permissions, then connect again." },
  not_configured: { tone: "bad", text: "This deployment has no Google OAuth client configured." },
};

export default function SettingsPage() {
  const [status, setStatus] = useState<GmailStatus | null>(null);
  const [ingestion, setIngestion] = useState<EmailIngestionStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"connect" | "disconnect" | "poll" | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "warn" | "bad"; text: string } | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [gmail, ingest] = await Promise.all([
        apiJson<GmailStatus>("/api/email/oauth/gmail/status"),
        apiJson<EmailIngestionStatus>("/api/email/ingestion"),
      ]);
      setStatus(gmail);
      setIngestion(ingest);
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 403
          ? "Email integration is managed by an administrator."
          : "Could not load the email integration status."
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /* The callback's outcome, read once and then removed from the address bar —
     so a reload does not replay a stale "connected" banner over a mailbox that
     has since been disconnected. */
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const result = params.get("gmail");
    if (!result) return;
    setNotice(
      CALLBACK_MESSAGE[result] ?? {
        tone: "bad",
        text: "The Gmail authorization did not complete.",
      }
    );
    params.delete("gmail");
    const query = params.toString();
    window.history.replaceState({}, "", window.location.pathname + (query ? `?${query}` : ""));
  }, []);

  async function connect() {
    setBusy("connect");
    setError(null);
    try {
      const started = await apiJson<GmailAuthorizeStart>("/api/email/oauth/gmail/authorize", {
        method: "POST",
      });
      // A full-page navigation, not a popup or an iframe: Google refuses to be
      // framed, and a popup is the thing browsers block by default.
      window.location.href = started.authorization_url;
    } catch (e) {
      setBusy(null);
      setError(
        e instanceof ApiError && e.status === 409
          ? "This deployment has no Google OAuth client configured yet. See the deployment notes below."
          : "Could not start the Google authorization."
      );
    }
  }

  async function disconnect() {
    if (!window.confirm("Disconnect this Gmail mailbox? Invoices arriving there will no longer be collected.")) {
      return;
    }
    setBusy("disconnect");
    setError(null);
    try {
      const result = await apiJson<GmailDisconnectResult>("/api/email/oauth/gmail/disconnect", {
        method: "POST",
      });
      setNotice(
        result.revoked_at_google
          ? { tone: "ok", text: "Gmail disconnected, and the access was revoked at Google." }
          : { tone: "warn", text: result.notice ?? "Gmail disconnected." }
      );
      await load();
    } catch {
      setError("Could not disconnect the mailbox.");
    } finally {
      setBusy(null);
    }
  }

  async function pollNow() {
    setBusy("poll");
    setError(null);
    try {
      const response = await apiFetch("/api/email/ingestion/poll", { method: "POST" });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        // 502 means the mailbox itself could not be reached — a genuinely
        // different problem from a rejected request, and worth saying so.
        setNotice({
          tone: "bad",
          text: body?.detail ?? "The mailbox could not be reached.",
        });
      } else {
        const fetched = body?.fetched ?? 0;
        setNotice({
          tone: "ok",
          text:
            fetched === 0
              ? "Checked the mailbox — nothing new."
              : `Checked the mailbox — ${fetched} message${fetched === 1 ? "" : "s"} collected.`,
        });
      }
      await load();
    } catch {
      setError("Could not check the mailbox.");
    } finally {
      setBusy(null);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 p-6 text-[13px] text-muted">
        <Spinner /> Loading email integration…
      </div>
    );
  }

  const connection = status?.connection ?? null;
  const connected = connection?.status === "CONNECTED";
  const revoked = connection?.status === "REVOKED";
  const scopes = Array.isArray(status?.scopes_requested) ? status?.scopes_requested : null;
  const scopeError =
    status && !Array.isArray(status.scopes_requested) ? status.scopes_requested.error : null;

  return (
    <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-4 sm:p-6">
      <PanelHeader
        title="Email integration"
        description="Collect invoices automatically from a mailbox, without holding its password."
      />

      {notice && (
        <Callout tone={notice.tone} title={notice.tone === "ok" ? "Done" : "Attention"}>
          {notice.text}
        </Callout>
      )}
      {error && <Callout tone="bad">{error}</Callout>}
      {scopeError && (
        <Callout tone="bad" title="Configured Gmail scope is not usable">
          {scopeError}
        </Callout>
      )}

      {/* ---------------------------------------------------------- Gmail */}
      <Panel>
        <PanelHeader
          title="Gmail"
          description={
            connected
              ? "Connected through Google. This application never sees the mailbox password."
              : "Connect a mailbox by signing in at Google. No password is entered here."
          }
          actions={
            <div className="flex items-center gap-1.5">
              {connected && <Badge tone="ok">Connected</Badge>}
              {revoked && <Badge tone="bad">Access revoked</Badge>}
              {!connection && <Badge tone="neutral">Not connected</Badge>}
            </div>
          }
        />

        <div className="mt-3">
          {!status?.oauth_configured ? (
            /* A configuration problem, not a connection problem — and the two
               have completely different remedies, so they are never collapsed
               into one "not connected" state. */
            <Callout tone="warn" title="No Google OAuth client is configured">
              An operator needs to set <code>GOOGLE_OAUTH_CLIENT_ID</code>,{" "}
              <code>GOOGLE_OAUTH_CLIENT_SECRET</code> and <code>GOOGLE_OAUTH_REDIRECT_URI</code> in
              the server environment and restart the application. Until then, Gmail cannot be
              connected from here.
            </Callout>
          ) : !connection ? (
            <EmptyState
              icon={<IconSettings size={20} />}
              title="No mailbox connected"
              description="Connect the mailbox your suppliers send invoices to. You will sign in at Google — this application only receives permission to read that mailbox."
              action={
                <Button variant="primary" loading={busy === "connect"} onClick={connect}>
                  Connect Gmail
                </Button>
              }
            />
          ) : (
            <>
              {revoked && (
                <Callout tone="bad" title="Google is no longer accepting this connection" className="mb-3">
                  Access was revoked, expired, or invalidated by a password change. Nothing is being
                  collected from this mailbox. Reconnect to resume.
                </Callout>
              )}
              {connection.last_error && !revoked && (
                <Callout tone="warn" title="Last attempt reported a problem" className="mb-3">
                  {connection.last_error}
                </Callout>
              )}

              <KeyValues
                rows={[
                  ["Mailbox", connection.email_address ?? <span className="text-faint">not reported by Google</span>],
                  ["Connected by", connection.connected_by ?? "—"],
                  ["Connected", when(connection.connected_at)],
                  ["Last checked", connection.last_polled_at ? when(connection.last_polled_at) : <span className="text-faint">not yet</span>],
                  [
                    "Permission granted",
                    <span key="s" className="font-mono text-[11.5px]">
                      {(connection.scopes ?? "—").replace("https://www.googleapis.com/auth/", "")}
                    </span>,
                  ],
                  [
                    "Automatic collection",
                    status?.poller_running ? (
                      <Badge tone="ok">Running</Badge>
                    ) : status?.ingestion_active ? (
                      <Badge tone="warn">Starts with the server</Badge>
                    ) : (
                      <Badge tone="neutral">Off</Badge>
                    ),
                  ],
                ]}
              />

              <div className="mt-3 flex flex-wrap gap-1.5">
                <Button loading={busy === "poll"} onClick={pollNow} disabled={revoked}>
                  Check now
                </Button>
                <Button loading={busy === "connect"} onClick={connect}>
                  {revoked ? "Reconnect" : "Reconnect a different mailbox"}
                </Button>
                <Button variant="danger" loading={busy === "disconnect"} onClick={disconnect}>
                  Disconnect
                </Button>
              </div>
            </>
          )}
        </div>
      </Panel>

      {/* ------------------------------------------------- what is collected */}
      {ingestion && connection && (
        <Panel>
          <PanelHeader
            title="What has been collected"
            description="Every message that arrived is recorded, including the ones that were filtered out or held."
          />
          <div className="mt-3">
            <KeyValues
              rows={[
                ["Messages seen", String(sum(ingestion.counts.by_ingest_status))],
                ["Held for review", String(ingestion.counts.by_ingest_status?.QUARANTINED ?? 0)],
                ["Filtered out", String(ingestion.counts.by_ingest_status?.FILTERED_OUT ?? 0)],
                ["Invoices created", String(ingestion.counts.invoice_runs_created ?? 0)],
                ["Checked every", `${ingestion.poll_seconds} seconds`],
              ]}
            />
          </div>
        </Panel>
      )}

      {/* -------------------------------------------------------- what we ask */}
      <Panel>
        <PanelHeader
          title="What this application is allowed to do"
          description="The permission requested is the narrowest one that can read an invoice."
        />
        <div className="mt-3 flex flex-col gap-2 text-[12.5px] text-muted">
          <p>
            Gmail is read through Google&apos;s API using{" "}
            <span className="font-mono text-[11.5px]">
              {scopes?.[0]?.replace("https://www.googleapis.com/auth/", "") ?? "gmail.readonly"}
            </span>
            . That permission can read messages and download attachments, and nothing else — it
            cannot send, reply, delete, or change a label.
          </p>
          <p>
            Connecting over IMAP instead would require Google&apos;s{" "}
            <span className="font-mono text-[11.5px]">mail.google.com</span> permission, which
            grants full control of the mailbox including deletion. That is why this uses the API.
          </p>
          <p>
            Messages still go through the same sender verification, quarantine rules and invoice
            processing as every other route into this application. Nothing about arriving by Gmail
            makes an invoice more trusted.
          </p>
        </div>
      </Panel>
    </div>
  );
}

function sum(counts: Record<string, number> | undefined): number {
  return Object.values(counts ?? {}).reduce((total, n) => total + (n || 0), 0);
}
