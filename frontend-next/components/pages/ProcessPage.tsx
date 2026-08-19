"use client";

/**
 * The processing workflow: choose a PDF, watch the pipeline run, read the
 * outcome.
 *
 * The page is deliberately two-state. Before a run it is a focused upload
 * screen; during and after, the pipeline and the evidence take over. Showing
 * empty result panels beside an upload box is the thing that makes internal
 * tools feel unfinished.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, apiJson, streamRun } from "@/lib/api";
import { STAGE_ORDER } from "@/lib/format";
import type { RunResult, SampleInvoice, Stage } from "@/lib/types";
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  Skeleton,
  StatusBadge,
} from "@/components/ui";
import { IconAlert, IconFile, IconUpload } from "@/components/ui/icons";
import StageList from "@/components/invoice/StageList";
import RunDetail from "@/components/invoice/RunDetail";
import { VerdictHeader } from "@/components/invoice/Panels";

const MAX_MB = 10;

export default function ProcessPage({ onRan }: { onRan?: () => void }) {
  const [samples, setSamples] = useState<SampleInvoice[] | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [stages, setStages] = useState<Stage[]>([]);
  const [result, setResult] = useState<RunResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    apiJson<SampleInvoice[]>("/api/sample-invoices")
      .then(setSamples)
      .catch(() => setSamples([]));
  }, []);

  const choose = useCallback((f: File) => {
    // Validated here purely to fail fast with a readable message; the server
    // enforces type and size independently and is the real gate.
    if (!f.name.toLowerCase().endsWith(".pdf") && f.type !== "application/pdf") {
      setError("That is not a PDF. This process reads vendor invoices as PDF files.");
      return;
    }
    if (f.size > MAX_MB * 1024 * 1024) {
      setError(`That file is ${(f.size / 1024 / 1024).toFixed(1)} MB. The limit is ${MAX_MB} MB.`);
      return;
    }
    setError(null);
    setFile(f);
    setPicked(null);
  }, []);

  async function pickSample(s: SampleInvoice) {
    const res = await apiFetch(`/api/sample-invoices/${encodeURIComponent(s.filename)}`);
    if (!res.ok) {
      setError("That sample could not be loaded.");
      return;
    }
    setError(null);
    setFile(new File([await res.blob()], s.filename, { type: "application/pdf" }));
    setPicked(s.filename);
  }

  async function run() {
    if (!file) return;
    setRunning(true);
    setError(null);
    setStages([]);
    setResult(null);
    try {
      await streamRun(file, (evt) => {
        if (evt.type === "stage") setStages((p) => [...p, evt.stage]);
        else if (evt.type === "final") setResult(evt.result);
        else if (evt.type === "error") setError(evt.error);
      });
      onRan?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The run could not be completed.");
    } finally {
      setRunning(false);
    }
  }

  function reset() {
    setFile(null);
    setPicked(null);
    setStages([]);
    setResult(null);
    setError(null);
  }

  const done = stages.length;
  const pct = Math.round((done / STAGE_ORDER.length) * 100);
  const started = running || done > 0;

  return (
    <PageBody>
      <PageHeader
        title="Process an invoice"
        description="Upload a vendor invoice. Nine checks run in order and the rules decide the outcome."
        actions={
          started && (
            <Button variant="secondary" size="sm" onClick={reset} disabled={running}>
              Process another
            </Button>
          )
        }
      />

      {error && (
        <Callout tone="danger" icon={<IconAlert size={14} />} title="Could not process">
          {error}
        </Callout>
      )}

      {result && (
        <VerdictHeader
          status={result.status}
          filename={result.filename}
          runId={result.run_id}
          vendor={result.extracted.vendor_name}
          invoiceNumber={result.extracted.invoice_number}
          total={result.extracted.total}
          remaining={result.po_match?.po_number ? result.po_match.remaining_before : null}
        />
      )}

      <div className="grid gap-5 lg:grid-cols-[320px_minmax(0,1fr)]">
        {/* ------------------------------------------------------- input */}
        <div className="flex flex-col gap-5">
          <Card>
            <CardHeader title="Invoice file" />
            <div
              onClick={() => !running && input.current?.click()}
              onKeyDown={(e) =>
                (e.key === "Enter" || e.key === " ") && !running && input.current?.click()
              }
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                if (e.dataTransfer.files[0]) choose(e.dataTransfer.files[0]);
              }}
              role="button"
              tabIndex={running ? -1 : 0}
              aria-label="Choose a PDF invoice"
              aria-disabled={running}
              className={`mt-4 flex cursor-pointer flex-col items-center gap-1.5 rounded-[var(--radius-md)]
                border border-dashed px-4 py-7 text-center transition-colors ${
                  dragging
                    ? "border-accent bg-accent-weak"
                    : "border-border-strong bg-surface2 hover:border-accent hover:bg-accent-weak/40"
                } ${running ? "pointer-events-none opacity-50" : ""}`}
            >
              <span className="grid h-9 w-9 place-items-center rounded-full border border-border bg-surface text-muted">
                <IconUpload size={16} />
              </span>
              <span className="text-[13px] font-medium">Drop a PDF or browse</span>
              <span className="text-[12px] text-subtle">Up to {MAX_MB} MB</span>
              <input
                ref={input}
                type="file"
                accept="application/pdf"
                hidden
                onChange={(e) => e.target.files?.[0] && choose(e.target.files[0])}
              />
            </div>

            {file && (
              <div className="mt-3 flex items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface2 px-3 py-2">
                <IconFile size={15} className="shrink-0 text-subtle" />
                <span className="min-w-0 flex-1 truncate text-[13px]">{file.name}</span>
                <span className="num shrink-0 text-[11px] text-subtle">
                  {(file.size / 1024).toFixed(0)} KB
                </span>
              </div>
            )}

            <Button
              variant="primary"
              className="mt-3 w-full"
              onClick={run}
              disabled={!file}
              loading={running}
            >
              {running ? "Processing…" : "Run process"}
            </Button>
          </Card>

          <Card>
            <CardHeader
              title="Sample invoices"
              description="Several build on each other — run them top to bottom."
            />
            <div className="mt-4 flex flex-col gap-1.5">
              {samples === null &&
                Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-[62px] w-full" />
                ))}

              {samples?.map((s) => {
                const active = picked === s.filename;
                return (
                  <button
                    key={s.filename}
                    onClick={() => pickSample(s)}
                    disabled={running}
                    className={`rounded-[var(--radius-md)] border p-2.5 text-left transition-colors
                      disabled:opacity-50 ${
                        active
                          ? "border-accent bg-accent-weak"
                          : "border-border bg-surface hover:border-border-strong hover:bg-surface2"
                      }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <span className="text-[13px] font-medium">{s.label || s.filename}</span>
                      {s.expect && <StatusBadge status={s.expect} />}
                    </div>
                    {s.note && <p className="mt-0.5 text-[12px] text-muted">{s.note}</p>}
                  </button>
                );
              })}

              {samples?.length === 0 && (
                <p className="py-2 text-[13px] text-muted">No samples are available.</p>
              )}
            </div>
          </Card>
        </div>

        {/* ---------------------------------------------------- pipeline */}
        <div className="flex flex-col gap-5">
          <Card>
            <CardHeader
              title="Pipeline"
              actions={
                <Badge tone={running ? "accent" : done ? "neutral" : "neutral"}>
                  {running && done === 0
                    ? "starting"
                    : done
                      ? `${done} of ${STAGE_ORDER.length}`
                      : "idle"}
                </Badge>
              }
            />

            <div
              className="mt-3 h-1 w-full overflow-hidden rounded-full bg-surface2"
              role="progressbar"
              aria-valuenow={pct}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label="Pipeline progress"
            >
              <div
                className="h-full rounded-full bg-accent transition-[width] duration-300 ease-out"
                style={{ width: `${pct}%` }}
              />
            </div>

            <div className="mt-5">
              {!started ? (
                <EmptyState
                  icon={<IconUpload size={18} />}
                  title="Nothing processed yet"
                  description="Choose a file or a sample on the left, then run the process. Each stage reports as it completes."
                />
              ) : (
                <StageList stages={stages} running={running} />
              )}
            </div>
          </Card>

          {result && (
            <div className="rise">
              <RunDetail
                run={{
                  id: result.run_id,
                  filename: result.filename,
                  status: result.status,
                  reasons: result.reasons,
                  stages: result.stages,
                  extracted: result.extracted,
                  po_match: result.po_match,
                  audit: result.audit,
                  automated_decision: result.status,
                  human_decision: null,
                }}
                onReviewed={onRan}
                showStages={false}
              />
            </div>
          )}
        </div>
      </div>
    </PageBody>
  );
}
