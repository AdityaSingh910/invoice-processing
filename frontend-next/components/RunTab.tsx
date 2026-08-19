"use client";

/**
 * The live run view: pick a PDF, drive the nine-stage pipeline, watch each
 * stage report as it executes, then see the verdict and the evidence behind it.
 *
 * The UI never decides anything. It renders stages the server streamed and a
 * verdict the rule engine computed.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, apiJson, streamRun } from "@/lib/api";
import { STAGE_ORDER, money } from "@/lib/format";
import type { RunResult, SampleInvoice, Stage } from "@/lib/types";
import { Card, Chip, EmptyState, KvTable, Missing, StatusPill } from "./ui";
import StageList from "./StageList";
import PoBalance from "./PoBalance";
import ReasonList from "./ReasonList";
import AuditTrail from "./AuditTrail";
import ReviewBar from "./ReviewBar";

export default function RunTab({ onRan }: { onRan?: () => void }) {
  const [samples, setSamples] = useState<SampleInvoice[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [selectedSample, setSelectedSample] = useState<string | null>(null);
  const [stages, setStages] = useState<Stage[]>([]);
  const [result, setResult] = useState<RunResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    apiJson<SampleInvoice[]>("/api/sample-invoices")
      .then(setSamples)
      .catch(() => setSamples([]));
  }, []);

  const pickSample = useCallback(async (s: SampleInvoice) => {
    const res = await apiFetch(`/api/sample-invoices/${encodeURIComponent(s.filename)}`);
    if (!res.ok) return;
    const blob = await res.blob();
    setFile(new File([blob], s.filename, { type: "application/pdf" }));
    setSelectedSample(s.filename);
  }, []);

  function chooseLocal(f: File) {
    setFile(f);
    setSelectedSample(null);
  }

  async function run() {
    if (!file) return;
    setRunning(true);
    setError(null);
    setStages([]);
    setResult(null);

    try {
      await streamRun(file, (evt) => {
        if (evt.type === "stage") setStages((prev) => [...prev, evt.stage]);
        else if (evt.type === "final") setResult(evt.result);
        else if (evt.type === "error") setError(evt.error);
      });
      onRan?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  const progress = stages.length;
  const pct = Math.round((progress / STAGE_ORDER.length) * 100);

  return (
    <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
      {/* ---------------- left rail: input ---------------- */}
      <div className="grid content-start gap-4">
        <Card title="Input">
          <div
            onClick={() => fileInput.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              if (e.dataTransfer.files.length) chooseLocal(e.dataTransfer.files[0]);
            }}
            className={`grid cursor-pointer place-items-center gap-1 rounded-lg border-2 border-dashed px-4 py-7 text-center transition ${
              dragging ? "border-accent bg-accent-soft" : "border-border bg-panel2"
            }`}
          >
            <div className="text-2xl text-faint">⭳</div>
            <p className="font-semibold">Drop a PDF</p>
            <p className="text-dim">or click to browse</p>
            <input
              ref={fileInput}
              type="file"
              accept="application/pdf"
              hidden
              onChange={(e) => e.target.files?.[0] && chooseLocal(e.target.files[0])}
            />
          </div>

          {file && (
            <div className="mt-3 truncate rounded-lg border border-border bg-panel2 px-3 py-2 font-mono text-[12px]">
              {file.name}
            </div>
          )}

          <button
            onClick={run}
            disabled={!file || running}
            className="mt-3 w-full rounded-lg bg-accent px-4 py-2.5 font-semibold text-white transition hover:opacity-90 disabled:opacity-50"
          >
            {running ? "Running…" : "Run process"}
          </button>
        </Card>

        <Card
          title="Sample invoices"
          aside={<Chip title="Some scenarios depend on earlier runs">order matters</Chip>}
        >
          <div className="grid gap-2">
            {samples.map((s) => (
              <button
                key={s.filename}
                onClick={() => pickSample(s)}
                className={`rounded-lg border px-3 py-2 text-left transition hover:border-accent ${
                  selectedSample === s.filename
                    ? "border-accent bg-accent-soft"
                    : "border-border bg-panel2"
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="font-medium">{s.label || s.filename}</span>
                  {s.expect && <StatusPill status={s.expect} />}
                </div>
                {s.note && <div className="mt-0.5 text-dim">{s.note}</div>}
                <div className="mt-0.5 font-mono text-[11px] text-faint">{s.filename}</div>
              </button>
            ))}
          </div>
        </Card>
      </div>

      {/* ---------------- right: pipeline + decision ---------------- */}
      <div className="grid content-start gap-4">
        {result && <VerdictBar r={result} />}

        {error && (
          <div
            className="rounded-[var(--radius-card)] border px-4 py-3"
            style={{
              borderColor: "var(--fail-solid)",
              background: "var(--fail-soft)",
              color: "var(--fail)",
            }}
          >
            Run failed: {error}
          </div>
        )}

        <Card
          title="Pipeline"
          aside={
            <span className="text-dim">
              {running && progress === 0
                ? "running…"
                : progress
                  ? `${progress} / ${STAGE_ORDER.length} stages`
                  : "idle"}
            </span>
          }
        >
          <div className="mb-3 h-1.5 w-full overflow-hidden rounded-full bg-panel2">
            <div
              className="h-full rounded-full transition-[width] duration-300"
              style={{ width: `${pct}%`, background: "var(--accent)" }}
            />
          </div>

          {progress === 0 && !running ? (
            <EmptyState
              title="No run yet"
              sub={
                <>
                  Pick a sample invoice on the left, then hit <em>Run process</em>. Each stage
                  reports as it executes.
                </>
              }
            />
          ) : (
            <StageList stages={stages} running={running} />
          )}
        </Card>

        {result?.po_match?.po_number && (
          <Card title="PO balance" aside={<Chip>{result.po_match.matched_via} match</Chip>}>
            <PoBalance pm={result.po_match} />
          </Card>
        )}

        {result && (
          <div className="grid gap-4 xl:grid-cols-2">
            <Card title="Reasoning">
              <ReasonList reasons={result.reasons} />
            </Card>
            <Card title="Extracted fields">
              <ExtractedFields r={result} />
            </Card>
          </div>
        )}

        {result && (
          <Card title="Decision details" aside={<Chip>audit trail</Chip>}>
            <AuditTrail audit={result.audit} />
            {/* A run that has just finished carries no human ruling yet, so the
                review bar is offered whenever the rules held it. */}
            <ReviewBar
              runId={result.run_id}
              automatedDecision={result.status}
              humanDecision={null}
              onReviewed={onRan}
            />
          </Card>
        )}
      </div>
    </div>
  );
}

function VerdictBar({ r }: { r: RunResult }) {
  const pm = r.po_match;
  const tone = r.status === "APPROVED" ? "ok" : r.status === "REJECTED" ? "fail" : "warn";

  return (
    <div
      data-testid="verdict-bar"
      className="flex flex-wrap items-center justify-between gap-4 rounded-[var(--radius-card)] border px-4 py-3"
      style={{ borderColor: `var(--${tone}-solid)`, background: `var(--${tone}-soft)` }}
    >
      <div className="flex min-w-0 items-center gap-3">
        <StatusPill status={r.status} />
        <div className="min-w-0">
          <div className="truncate font-semibold">{r.filename}</div>
          <div className="text-dim">
            run #{r.run_id} · {r.extracted.vendor_name || "unknown vendor"}
            {r.extracted.invoice_number ? ` · ${r.extracted.invoice_number}` : ""}
          </div>
        </div>
      </div>
      <div className="flex gap-6">
        <div className="text-right">
          <div className="text-lg font-semibold">{money(r.extracted.total)}</div>
          <div className="text-[11px] text-faint uppercase">invoice total</div>
        </div>
        {pm.po_number && (
          <div className="text-right">
            <div className="text-lg font-semibold">{money(pm.remaining_before)}</div>
            <div className="text-[11px] text-faint uppercase">PO available</div>
          </div>
        )}
      </div>
    </div>
  );
}

function ExtractedFields({ r }: { r: RunResult }) {
  const e = r.extracted;
  const req = (v: string | null) => v || <Missing />;
  return (
    <KvTable
      rows={[
        ["Vendor", req(e.vendor_name)],
        ["Invoice #", req(e.invoice_number)],
        ["Date", e.invoice_date || "—"],
        ["PO refs", (e.po_references || []).join(", ") || "—"],
        ["Subtotal", money(e.subtotal)],
        ["Tax", money(e.tax)],
        ["Total", e.total != null ? <b key="t">{money(e.total)}</b> : <Missing key="t" />],
        ["Line items", (e.line_items || []).length || "—"],
        ["Currency", e.currency || "—"],
        [
          "Extraction route",
          <span key="r" className="font-mono text-[12px]">
            {e.extraction_method}
          </span>,
        ],
      ]}
    />
  );
}
