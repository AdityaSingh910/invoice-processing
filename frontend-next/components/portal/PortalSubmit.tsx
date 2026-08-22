"use client";

/**
 * Send an invoice.
 *
 * DELIBERATELY NOT THE INTERNAL UPLOAD SCREEN. ProcessPage streams the nine
 * pipeline stages as they land, which is exactly right for an employee
 * watching their own upload work — and exactly wrong here: those frames name
 * internal stages and carry their detail lines, and the server does not send
 * them to this endpoint at all. So this screen shows one thing happening and
 * then the outcome, in the supplier's own vocabulary.
 *
 * The result shown is the one that was COMMITTED, re-read by the server
 * through the same visibility check every other portal read goes through — so
 * a supplier is never told an invoice was approved by a screen that saw a
 * decision the database later changed.
 */
import { useRef, useState } from "react";
import { submitPortalInvoice } from "@/lib/api";
import { amount } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { Badge, Button, Callout, Panel, PanelHeader, Spinner } from "@/components/ui";
import { IconUpload } from "@/components/ui/icons";
import type { PortalInvoice } from "@/lib/types";
import { PortalPage, stateTone, useStateWord } from "./PortalApp";

export default function PortalSubmit({ onSubmitted }: { onSubmitted: () => void }) {
  const t = useT();
  const stateWord = useStateWord();
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PortalInvoice | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const choose = (f: File | null | undefined) => {
    setError(null);
    setResult(null);
    if (!f) return;
    // A courtesy check only. The server validates by MAGIC BYTES, not by
    // extension or by the Content-Type the browser guessed, so a renamed file
    // is caught there whatever this says.
    if (!f.name.toLowerCase().endsWith(".pdf")) {
      setError(t("portal.submit.notPdf"));
      return;
    }
    setFile(f);
  };

  const send = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const res = await submitPortalInvoice(file);
      setResult(res.invoice);
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
    } catch (e) {
      setError(e instanceof Error ? e.message : t("portal.submit.failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <PortalPage title={t("portal.submit.title")} description={t("portal.submit.subtitle")}>
      <Panel>
        <PanelHeader
          title={t("portal.submit.panel.title")}
          description={t("portal.submit.panel.desc")}
        />

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            choose(e.dataTransfer.files?.[0]);
          }}
          className={`mt-3 flex flex-col items-center gap-3 rounded-[var(--radius-md)]
            border border-dashed px-6 py-10 text-center transition-colors ${
              dragging ? "border-accent bg-accent-quiet" : "border-line bg-sunken"
            }`}
        >
          <span className="grid h-9 w-9 place-items-center rounded-[var(--radius-md)] border border-line bg-surface text-faint">
            <IconUpload size={16} />
          </span>
          <div>
            <p className="text-[13px] font-medium">
              {file ? file.name : t("portal.submit.drop")}
            </p>
            <p className="t-meta mt-1">
              {file
                ? `${(file.size / 1024).toFixed(0)} KB`
                : t("portal.submit.orChoose")}
            </p>
          </div>

          <input
            ref={inputRef}
            type="file"
            accept="application/pdf,.pdf"
            className="sr-only"
            id="portal-file"
            onChange={(e) => choose(e.target.files?.[0])}
          />
          <div className="flex flex-wrap items-center justify-center gap-2">
            <Button size="sm" onClick={() => inputRef.current?.click()} disabled={busy}>
              {t("portal.submit.choose")}
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={send}
              disabled={!file || busy}
              loading={busy}
            >
              {busy ? t("portal.submit.sending") : t("portal.submit.send")}
            </Button>
          </div>

          {busy && (
            <span className="flex items-center gap-2 text-[12px] text-muted">
              <Spinner />
              {t("portal.submit.reading")}
            </span>
          )}
        </div>

        {error && (
          <Callout tone="bad" className="mt-3" title={t("portal.submit.notSent")}>
            {error}
          </Callout>
        )}
      </Panel>

      {result && (
        <Panel>
          <PanelHeader
            title={t("portal.submit.received")}
            description={result.invoice_number ?? "—"}
            actions={
              <Button size="sm" onClick={onSubmitted}>
                {t("portal.submit.viewMine")}
              </Button>
            }
          />
          <div className="mt-3 flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={stateTone(result.state)} dot>
                {stateWord(result.state)}
              </Badge>
              <span className="text-[12.5px] text-muted">{result.state_headline}</span>
              <span className="ml-auto text-[12.5px] font-medium">
                {amount(result.total, result.currency)}
              </span>
            </div>

            {/* Why it is where it is, in the same frozen sentences the invoice
                list shows. An invoice held on arrival is the most useful thing
                a supplier can be told at the moment they send it, because it
                is the moment they can still do something about it. */}
            {result.state_detail.length > 0 && (
              <ul className="flex flex-col gap-1.5">
                {result.state_detail.map((line, i) => (
                  <li key={i} className="text-[12.5px] leading-relaxed">
                    {line}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Panel>
      )}
    </PortalPage>
  );
}
