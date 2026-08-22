"use client";

/**
 * The assistant (Phase K2).
 *
 * A question box over the records this user can already read. It is built from
 * the same primitives as every other section — Panel, Button, Badge, Callout,
 * EmptyState — because it is a part of this application rather than a widget
 * bolted onto it.
 *
 * THE ONE THING THIS SCREEN DOES THAT AN ORDINARY CHAT UI DOES NOT
 *
 * It says where every answer came from. The server returns `answered_from` on
 * each reply, and the badge under the answer reports it:
 *
 *   From your records         retrieved and laid out by the server, no model
 *   Records, written up       retrieved, then phrased by the language model
 *   What this app tracks      a fixed answer about data the app does not hold
 *
 * That distinction is the whole point of showing it. A sentence a model wrote
 * and a figure read out of the ledger look identical on screen, and a reader
 * deciding whether to act on an invoice needs to know which one they are
 * looking at. The retrieved records are shown underneath, collapsed, for the
 * same reason: the prose is a convenience, the records are the evidence.
 *
 * `sources` are computed server-side from what was actually read, so the
 * citations under an answer cannot name an invoice that does not exist.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiJson } from "@/lib/api";
import { useT } from "@/lib/i18n";
import type { ChatReply, ChatSuggestions, ChatTurn } from "@/lib/types";
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Button,
  Callout,
  EmptyState,
  Panel,
  Spinner,
  type Tone,
} from "@/components/ui";
import { IconAlert, IconChat, IconRefresh } from "@/components/ui/icons";

/** How many prior turns travel with the next question. Matches the server's
 *  own MAX_HISTORY_TURNS — sending more would be trimmed there anyway, and
 *  sending fewer would lose the context the server is willing to use. */
const HISTORY_TURNS = 6;

/** Mirrors chat.MAX_MESSAGE_CHARS. Enforced here so someone pasting an essay
 *  is told before they send it, and again on the server because a client-side
 *  limit is a courtesy, not a control. */
const MAX_CHARS = 2000;

/**
 * How an answer was produced. The KEY is the server's `answered_from` value and
 * never changes; `labelKey` is looked up in the reader's language (Phase L).
 *
 * The one-line `hint` behind each is left in English on purpose: it is the
 * explanatory tooltip rather than the label, and translating an explanation of
 * the provenance model badly would be worse than leaving it in the language it
 * was written in. The LABEL -- the part somebody has to read at a glance to
 * know whether a model wrote the sentence -- is translated.
 */
const PROVENANCE: Record<
  string,
  { labelKey: "assistant.from.data" | "assistant.from.model" | "assistant.from.policy"; tone: Tone; hint: string }
> = {
  application_data: {
    labelKey: "assistant.from.data",
    tone: "ok",
    hint: "Read from this application's database and laid out by the server. No language model was involved.",
  },
  application_data_phrased_by_model: {
    labelKey: "assistant.from.model",
    tone: "accent",
    hint: "The figures were read from this application's database, then a language model wrote them up. Check the records below if anything matters.",
  },
  application_policy: {
    labelKey: "assistant.from.policy",
    tone: "neutral",
    hint: "A fixed answer about what this application does and does not record. Not generated.",
  },
};

let turnSeq = 0;
const nextId = () => `t${++turnSeq}`;

export default function AssistantPage() {
  const t = useT();
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [suggestions, setSuggestions] = useState<ChatSuggestions | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const input = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    let live = true;
    apiJson<ChatSuggestions>("/api/chat/suggestions")
      .then((s) => live && setSuggestions(s))
      .catch(() => {
        /* Suggestions are a convenience. Losing them must not break the page,
           so the composer stays usable and no error is shown for it. */
      });
    return () => {
      live = false;
    };
  }, []);

  // Keep the newest turn in view as the conversation grows.
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, busy]);

  const send = useCallback(
    async (question: string) => {
      const text = question.trim();
      if (!text || busy) return;

      const history = turns
        .filter((t) => !t.error)
        .slice(-HISTORY_TURNS)
        .map((t) => ({ role: t.role, content: t.content }));

      const asked: ChatTurn = { id: nextId(), role: "user", content: text };
      setTurns((prev) => [...prev, asked]);
      setDraft("");
      setBusy(true);

      try {
        const reply = await apiJson<ChatReply>("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text, history }),
        });
        setTurns((prev) => [
          ...prev,
          { id: nextId(), role: "assistant", content: reply.answer, reply },
        ]);
      } catch (err) {
        // The message names what the user can act on. A 429 is "wait", a 400
        // is "the question was rejected", anything else is "it did not work" —
        // and none of them is a stack trace.
        const status = err instanceof ApiError ? err.status : 0;
        const message =
          status === 429
            ? "Too many questions in a row. Give it a moment and try again."
            : status === 400
              ? "That question could not be processed. Try rephrasing it."
              : status === 403
                ? "Your account does not have permission to use the assistant."
                : "The assistant could not be reached.";
        setTurns((prev) => [
          ...prev,
          { id: nextId(), role: "assistant", content: message, error: message },
        ]);
      } finally {
        setBusy(false);
        input.current?.focus();
      }
    },
    [busy, turns]
  );

  /** Re-ask the last question. The failed exchange is dropped first so a retry
   *  does not leave the error sitting above its own successful answer. */
  const retry = useCallback(() => {
    const lastAsked = [...turns].reverse().find((t) => t.role === "user");
    if (!lastAsked) return;
    setTurns((prev) => {
      const trimmed = [...prev];
      while (trimmed.length && trimmed[trimmed.length - 1].role === "assistant") trimmed.pop();
      if (trimmed.length && trimmed[trimmed.length - 1].role === "user") trimmed.pop();
      return trimmed;
    });
    void send(lastAsked.content);
  }, [turns, send]);

  const tooLong = draft.length > MAX_CHARS;

  return (
    <>
      <PageHeader
        title={t("assistant.title")}
        description={t("assistant.subtitle")}
        actions={
          turns.length > 0 ? (
            <Button size="sm" icon={<IconRefresh size={12} />} onClick={() => setTurns([])}>
              New conversation
            </Button>
          ) : undefined
        }
      />
      <PageBody>
        {suggestions && !suggestions.available && (
          <Callout tone="warn" title="No language model is configured">
            The assistant still works: answers come straight from the records,
            laid out rather than written up.
          </Callout>
        )}

        <Panel flush className="flex min-h-[26rem] flex-col">
          <div
            ref={scroller}
            className="flex-1 space-y-4 overflow-y-auto px-4 py-4"
            // A conversation is a feed: announce new answers to a screen
            // reader without stealing focus from the box being typed in.
            aria-live="polite"
            aria-atomic="false"
          >
            {turns.length === 0 ? (
              <EmptyState
                icon={<IconChat size={16} />}
                title={t("assistant.empty")}
                description="Answers come from this application's own records. It cannot tell you whether an invoice was paid — nothing here records that."
                action={
                  suggestions?.suggestions?.length ? (
                    <div className="mt-1 flex flex-wrap justify-center gap-1.5">
                      {/* Two halves, and the distinction is load-bearing
                          (Phase L): `label` is what the reader sees, in their
                          own language, and `ask` is what gets sent. Intent
                          routing matches English patterns, so sending the
                          label would offer a question the assistant could not
                          then recognise. */}
                      {suggestions.suggestions.map((s) => (
                        <button
                          key={s.ask}
                          type="button"
                          onClick={() => void send(s.ask)}
                          className="rounded-[var(--radius-md)] border border-line bg-sunken px-2.5 py-1.5 text-[13px] text-muted transition-colors hover:border-line-strong hover:text-fg"
                        >
                          {s.label}
                        </button>
                      ))}
                    </div>
                  ) : undefined
                }
              />
            ) : (
              turns.map((turn) => <Turn key={turn.id} turn={turn} onRetry={retry} />)
            )}

            {busy && (
              <div className="flex items-center gap-2 text-[13.5px] text-faint">
                <Spinner size={12} />
                {t("assistant.thinking")}
              </div>
            )}
          </div>

          <form
            className="border-t border-line px-3 py-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (!tooLong) void send(draft);
            }}
          >
            <div className="flex items-end gap-2">
              <textarea
                ref={input}
                rows={1}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  // Enter sends, Shift+Enter breaks the line — the convention
                  // every chat interface uses, so it needs no explaining.
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    if (!tooLong) void send(draft);
                  }
                }}
                placeholder={t("assistant.placeholder")}
                aria-label={t("assistant.placeholder")}
                className="max-h-40 min-h-[2.25rem] flex-1 resize-y rounded-[var(--radius-md)] border border-line bg-sunken px-3 py-2 text-[14px] text-fg placeholder:text-faint focus:border-line-strong focus:outline-none"
              />
              <Button type="submit" variant="primary" loading={busy} disabled={!draft.trim() || tooLong}>
                {t("assistant.send")}
              </Button>
            </div>
            {tooLong && (
              <p className="t-meta mt-1.5 text-bad">
                {draft.length.toLocaleString()} characters — the limit is{" "}
                {MAX_CHARS.toLocaleString()}.
              </p>
            )}
          </form>
        </Panel>
      </PageBody>
    </>
  );
}

function Turn({ turn, onRetry }: { turn: ChatTurn; onRetry: () => void }) {
  const t = useT();

  if (turn.role === "user") {
    return (
      <div className="flex justify-end">
        <p className="max-w-[85%] rounded-[var(--radius-md)] bg-accent-quiet px-3 py-2 text-[14px] whitespace-pre-wrap">
          {turn.content}
        </p>
      </div>
    );
  }

  if (turn.error) {
    return (
      <div className="flex items-start gap-2">
        <span className="mt-0.5 shrink-0 text-bad">
          <IconAlert size={14} />
        </span>
        <div className="min-w-0">
          <p className="text-[14px] text-bad">{turn.error}</p>
          <Button size="sm" className="mt-1.5" onClick={onRetry}>
            {t("app.retry")}
          </Button>
        </div>
      </div>
    );
  }

  const reply = turn.reply;
  const provenance = reply ? PROVENANCE[reply.answered_from] : undefined;

  return (
    <div className="max-w-[92%] space-y-2">
      <p className="text-[14px] whitespace-pre-wrap">{turn.content}</p>

      {reply?.notice && (
        <Callout tone="warn" className="text-[13px]">
          {reply.notice}
        </Callout>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        {provenance && (
          <span title={provenance.hint}>
            <Badge tone={provenance.tone}>{t(provenance.labelKey)}</Badge>
          </span>
        )}
        {reply?.sources?.map((s) => (
          <Badge key={`${s.type}:${s.ref}`} tone="neutral">
            {s.ref}
            {s.label ? ` · ${s.label}` : ""}
          </Badge>
        ))}
      </div>

      {reply && reply.facts && Object.keys(reply.facts).length > 0 && (
        <details className="rounded-[var(--radius-md)] border border-line bg-sunken">
          <summary className="cursor-pointer px-3 py-2 text-[13px] text-muted select-none">
            {t("assistant.records")}
          </summary>
          <pre className="max-h-72 overflow-auto border-t border-line px-3 py-2 text-[12.5px] leading-relaxed text-muted">
            {JSON.stringify(reply.facts, null, 1)}
          </pre>
        </details>
      )}
    </div>
  );
}
