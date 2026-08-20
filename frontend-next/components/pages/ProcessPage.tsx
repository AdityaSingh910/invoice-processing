"use client";

/**
 * The processing workflow.
 *
 * Two states by design. Before a run this is a focused upload screen; once a
 * run starts, the phase stepper and the evidence take over. Rendering empty
 * result panels beside an upload box is the single thing that makes an internal
 * tool feel unfinished.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, apiJson, streamRun } from "@/lib/api";
import { STAGE_ORDER } from "@/lib/format";
import type { RunRecord, RunResult, SampleInvoice, Stage } from "@/lib/types";
import type { Async } from "@/lib/useData";
import { PageBody, PageHeader } from "@/components/layout/AppShell";
import {
  Badge,
  Button,
  Callout,
  EmptyState,
  Panel,
  PanelHeader,
  Skeleton,
  StatusBadge,
} from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { IconAlert, IconFile, IconUpload, IconX } from "@/components/ui/icons";
import StageList, { PhaseStepper } from "@/components/invoice/StageList";
import RunDetail from "@/components/invoice/RunDetail";
import { VerdictHeader } from "@/components/invoice/Panels";
import ResetDemoButton from "@/components/ResetDemoButton";

const MAX_MB = 10;

export default function ProcessPage({
  runs,
  onRan,
}: {
  runs?: Async<RunRecord[]>;
  onRan?: () => void;
}) {
  const toast = useToast();
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
    // Checked here only to fail fast with a readable message; the server
    // validates type and size independently and is the real gate.
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
      let final: RunResult | null = null;
      await streamRun(file, (evt) => {
        if (evt.type === "stage") setStages((p) => [...p, evt.stage]);
        else if (evt.type === "final") {
          final = evt.result;
          setResult(evt.result);
        } else if (evt.type === "error") setError(evt.error);
      });
      if (final) {
        const r = final as RunResult;
        toast.push({
          tone: r.status === "APPROVED" ? "ok" : r.status === "REJECTED" ? "bad" : "warn",
          title: `${r.filename} — ${r.status.replace(/_/g, " ").toLowerCase()}`,
          detail: `Nine checks completed. Run #${r.run_id}.`,
        });
      }
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

  const processed = new Set((runs?.data ?? []).map((r) => r.filename));

  // A rejection whose findings cite an earlier run is the duplicate rule
  // firing, which for a sample almost always means it has simply been run
  // before. Detected from the reasons the engine emitted, never guessed.
  const rejectedAsDuplicate =
    result?.status === "REJECTED" &&
    (result.reasons || []).some((raw) => {
      const text = typeof raw === "string" ? raw : raw.text;
      return /matches run #\d+|duplicat/i.test(text);
    });

  const done = stages.length;
  const started = running || done > 0;

  return (
    <>
      <PageHeader
        title="Process an invoice"
        description="Nine checks run in order. The rules decide the outcome, not the model."
        actions={
          started && (
            <Button size="sm" onClick={reset} disabled={running}>
              Process another
            </Button>
          )
        }
      />

      <PageBody>
        {error && (
          <Callout tone="bad" icon={<IconAlert size={13} />} title="Could not process">
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
            currency={result.extracted.currency}
            remaining={result.po_match?.po_number ? result.po_match.remaining_before : null}
            poCurrency={result.po_match?.po_currency}
          />
        )}

        {/* A duplicate rejection is correct but confusing on a sample the
            user has simply run before, so name the cause and offer the way
            out instead of leaving them to work it out. */}
        {rejectedAsDuplicate && (
          <Callout
            tone="warn"
            icon={<IconAlert size={13} />}
            title="This invoice has been processed before"
          >
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span>
                The duplicate check found an earlier run with the same vendor, invoice number and
                amount, so it was rejected — which is the rule working. The sample invoices are
                meant to be run once through in order; clearing the run history lets them be
                replayed from the start.
              </span>
              <ResetDemoButton onReset={() => { runs?.refresh(); reset(); }} />
            </div>
          </Callout>
        )}

        {/* The phase rail is the workflow at a glance; the stage list below is
            the detail. Both derive from the same streamed stages. */}
        {started && (
          <Panel>
            <PhaseStepper stages={stages} running={running} />
          </Panel>
        )}

        <div className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
          <div className="flex flex-col gap-4">
            <Panel>
              <PanelHeader title="Invoice file" />
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
                className={`mt-3.5 flex cursor-pointer flex-col items-center gap-1.5 rounded-[var(--radius-md)]
                  border border-dashed px-4 py-6 text-center transition-colors ${
                    dragging
                      ? "border-accent bg-accent-quiet"
                      : "border-line-strong bg-sunken hover:border-accent"
                  } ${running ? "pointer-events-none opacity-50" : ""}`}
              >
                <span className="grid h-8 w-8 place-items-center rounded-full border border-line bg-surface text-muted">
                  <IconUpload size={14} />
                </span>
                <span className="text-[12.5px] font-medium">Drop a PDF or browse</span>
                <span className="t-meta text-[11px]">Up to {MAX_MB} MB</span>
                <input
                  ref={input}
                  type="file"
                  accept="application/pdf"
                  hidden
                  onChange={(e) => e.target.files?.[0] && choose(e.target.files[0])}
                />
              </div>

              {file && (
                <div className="mt-2.5 flex items-center gap-2 rounded-[var(--radius-md)] border border-line bg-sunken px-2.5 py-1.5">
                  <IconFile size={13} className="shrink-0 text-faint" />
                  <span className="min-w-0 flex-1 truncate text-[12px]">{file.name}</span>
                  <span className="tnum shrink-0 text-[10.5px] text-faint">
                    {(file.size / 1024).toFixed(0)} KB
                  </span>
                  {!running && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        reset();
                      }}
                      aria-label="Remove file"
                      className="shrink-0 text-faint transition-colors hover:text-fg"
                    >
                      <IconX size={12} />
                    </button>
                  )}
                </div>
              )}

              <Button
                variant="primary"
                className="mt-2.5 w-full"
                onClick={run}
                disabled={!file}
                loading={running}
              >
                {running ? "Processing" : "Run process"}
              </Button>
            </Panel>

            <Panel flush>
              <PanelHeader
                bordered
                title="Sample invoices"
                description="Several build on each other — run them in order"
              />
              <div className="p-1.5">
                {samples === null &&
                  Array.from({ length: 4 }).map((_, i) => (
                    <Skeleton key={i} className="mb-1.5 h-12 w-full" />
                  ))}

                {samples?.map((s) => {
                  const active = picked === s.filename;
                  return (
                    <button
                      key={s.filename}
                      onClick={() => pickSample(s)}
                      disabled={running}
                      className={`w-full rounded-[var(--radius-md)] p-2 text-left transition-colors
                        disabled:opacity-50 ${active ? "bg-accent-quiet" : "hover:bg-hover"}`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <span className="text-[12.5px] font-medium">{s.label || s.filename}</span>
                        {s.expect && <StatusBadge status={s.expect} />}
                      </div>
                      {s.note && <p className="t-meta mt-0.5 text-[11px]">{s.note}</p>}
                      {/* Running one of these again is a genuine duplicate, so
                          say so before it surprises anyone. */}
                      {processed.has(s.filename) && (
                        <p className="mt-1 flex items-center gap-1 text-[11px] text-warn">
                          <IconAlert size={10} />
                          Already processed — will be rejected as a duplicate
                        </p>
                      )}
                    </button>
                  );
                })}

                {samples?.length === 0 && (
                  <p className="t-meta p-2">No samples are available.</p>
                )}
              </div>
            </Panel>
          </div>

          <div className="flex flex-col gap-4">
            <Panel flush>
              <PanelHeader
                bordered
                title="Pipeline"
                actions={
                  <Badge tone={running ? "accent" : "neutral"} dot={running}>
                    {running && done === 0
                      ? "starting"
                      : done
                        ? `${done} of ${STAGE_ORDER.length}`
                        : "idle"}
                  </Badge>
                }
              />
              <div className="p-4">
                {!started ? (
                  <EmptyState
                    icon={<IconUpload size={16} />}
                    title="Nothing processed yet"
                    description="Choose a file or a sample, then run the process. Each stage reports as it completes."
                  />
                ) : (
                  <StageList stages={stages} running={running} />
                )}
              </div>
            </Panel>

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
    </>
  );
}
