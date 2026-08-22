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
import { Badge, Button, Callout, Panel, PanelHeader, Skeleton, StatusBadge } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { IconAlert, IconFile, IconUpload, IconX } from "@/components/ui/icons";
import StageList, { PhaseStepper } from "@/components/invoice/StageList";
import { ReviewWorkspaceBody } from "@/components/invoice/ReviewWorkspace";
import DocumentPreview from "@/components/invoice/DocumentPreview";
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
  const alreadyRun = (samples ?? []).filter((s) => processed.has(s.filename)).length;

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
              <ResetDemoButton
                onReset={() => {
                  runs?.refresh();
                  reset();
                }}
              />
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

        {result ? (
          <div className="rise">
            <ReviewWorkspaceBody
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
              file={file}
              onReviewed={onRan}
            />
          </div>
        ) : (
          <>
            {/* Two columns on top — the invoice on the left, what the process
                will do with it on the right — then the sample library full
                width beneath. Stacking the library inside the left column
                instead made that column roughly three times the height of the
                right one, and left most of the page empty. */}
            <div className="grid gap-5 lg:grid-cols-2 lg:items-start">
              <div className="flex flex-col gap-5">
                {/* ---------------------------------------------------- document */}
                {/* THE PREVIEW SITS ON THE LEFT BECAUSE THAT IS WHERE IT ENDS UP.
                    ReviewWorkspaceBody -- which renders the result here, and is
                    also the Invoices and Review-queue detail view -- puts the
                    source document in the left column. Previewing on the right
                    beforehand meant pressing "Run the process" threw the document
                    across the page, in the one moment the reader is watching it.
                    Kept at the TOP of this column so the panel does not move at
                    all: the upload card below it is what disappears. */}
                {file && (
                  <Panel flush className="overflow-hidden">
                    <div className="flex items-center gap-2 border-b border-line bg-sunken px-3 py-2">
                      <IconFile size={12} className="shrink-0 text-faint" />
                      <span className="t-caption">Document</span>
                      <span className="min-w-0 flex-1 truncate text-right text-[12px] text-faint">
                        {file.name}
                      </span>
                    </div>
                    <div className="h-[320px]">
                      <DocumentPreview file={file} filename={file.name} />
                    </div>
                  </Panel>
                )}

                {/* ------------------------------------------------------ upload */}
                <Panel>
                  <PanelHeader
                    title="Invoice file"
                    description="A vendor invoice as a PDF, up to 10 MB."
                  />
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
                    className={`mt-4 flex cursor-pointer flex-col items-center gap-2 rounded-[var(--radius-md)]
                    border border-dashed px-6 py-9 text-center transition-colors ${
                      dragging
                        ? "border-accent bg-accent-quiet"
                        : "border-line-strong bg-sunken hover:border-accent hover:bg-hover"
                    } ${running ? "pointer-events-none opacity-50" : ""}`}
                  >
                    <span
                      className={`grid h-10 w-10 place-items-center rounded-full border transition-colors ${
                        dragging
                          ? "border-accent-line bg-surface text-accent"
                          : "border-line bg-surface text-muted"
                      }`}
                    >
                      <IconUpload size={16} />
                    </span>
                    <span className="text-[14px] font-semibold">
                      {dragging ? "Drop to load the invoice" : "Drop a PDF here, or browse"}
                    </span>
                    <span className="t-meta text-[12.5px]">
                      Read by a language model, judged by deterministic rules
                    </span>
                    <input
                      ref={input}
                      type="file"
                      accept="application/pdf"
                      hidden
                      onChange={(e) => e.target.files?.[0] && choose(e.target.files[0])}
                    />
                  </div>

                  {file && (
                    <div className="rise mt-3 flex items-center gap-2.5 rounded-[var(--radius-md)] border border-line bg-sunken px-3 py-2">
                      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-[var(--radius-sm)] bg-surface text-muted">
                        <IconFile size={13} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[13.5px] font-medium">
                          {file.name}
                        </span>
                        <span className="tnum t-meta block text-[12px]">
                          {(file.size / 1024).toFixed(0)} KB
                          {picked ? " · sample invoice" : ""}
                        </span>
                      </span>
                      {!running && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            reset();
                          }}
                          aria-label="Remove file"
                          className="grid h-6 w-6 shrink-0 place-items-center rounded-[var(--radius-sm)] text-faint transition-colors hover:bg-hover hover:text-fg"
                        >
                          <IconX size={13} />
                        </button>
                      )}
                    </div>
                  )}

                  <Button
                    variant="primary"
                    className="mt-3 h-9 w-full"
                    onClick={run}
                    disabled={!file}
                    loading={running}
                  >
                    {running ? "Processing" : file ? "Run the process" : "Choose a file to begin"}
                  </Button>
                </Panel>
              </div>

              {/* ------------------------------------------------------ plan */}
              <div className="flex flex-col gap-5">
                <Panel flush>
                  <PanelHeader
                    bordered
                    title="Pipeline"
                    description={
                      started ? undefined : "The nine checks this invoice will go through, in order"
                    }
                    actions={
                      <Badge tone={running ? "accent" : "neutral"} dot={running}>
                        {running && done === 0
                          ? "starting"
                          : done
                            ? `${done} of ${STAGE_ORDER.length}`
                            : "ready"}
                      </Badge>
                    }
                  />
                  {/* Before a run this shows the nine stages at rest rather than an
                    "empty" placeholder. It is the honest answer to "what is about
                    to happen", and it reuses the exact component that will report
                    the live run. */}
                  <div className="p-4">
                    <StageList stages={stages} running={running} />
                  </div>
                </Panel>
              </div>
            </div>

            {/* ------------------------------------------------------ samples */}
            <Panel flush>
              <PanelHeader
                bordered
                title="Sample invoices"
                description={
                  alreadyRun > 0
                    ? `Run them top to bottom — several build on each other. ${alreadyRun} carry an amber dot: those have been processed already and would be rejected as duplicates.`
                    : "Several build on each other — run them top to bottom"
                }
              />
              <div className="grid gap-1.5 p-2 sm:grid-cols-2 xl:grid-cols-3">
                {samples === null &&
                  Array.from({ length: 6 }).map((_, i) => (
                    <Skeleton key={i} className="h-[68px] w-full" />
                  ))}

                {samples?.map((s) => {
                  const active = picked === s.filename;
                  const already = processed.has(s.filename);
                  return (
                    <button
                      key={s.filename}
                      onClick={() => pickSample(s)}
                      disabled={running}
                      title={s.note || undefined}
                      className={`flex h-full flex-col rounded-[var(--radius-md)] border p-2.5 text-left
                          transition-colors disabled:opacity-50 ${
                            active
                              ? "border-accent-line bg-accent-quiet"
                              : "border-transparent hover:border-line hover:bg-hover"
                          }`}
                    >
                      <span className="flex items-start justify-between gap-2">
                        <span className="min-w-0 text-[13.5px] font-medium">
                          {s.label || s.filename}
                        </span>
                        <span className="flex shrink-0 items-center gap-1.5">
                          {/* One amber dot, not a sentence. Every sample is
                                "already run" after a single pass through the set,
                                so the full warning appeared ten times down the
                                page and stopped meaning anything. The panel
                                description below explains the marker once. */}
                          {already && (
                            <span
                              title="Already processed — running it again will be rejected as a duplicate"
                              aria-label="Already processed"
                              className="h-1.5 w-1.5 shrink-0 rounded-full bg-warn-vivid"
                            />
                          )}
                          {s.expect && <StatusBadge status={s.expect} />}
                        </span>
                      </span>
                      {/* Clamped to two lines. These notes explain a scenario in
                            full sentences; at full length one of them ran six
                            lines and the picker became the tallest thing on the
                            page. The whole note is still the row's tooltip. */}
                      {s.note && (
                        <span className="t-meta mt-1 line-clamp-2 text-[12px] leading-snug">
                          {s.note}
                        </span>
                      )}
                    </button>
                  );
                })}

                {samples?.length === 0 && (
                  <p className="t-meta col-span-full p-2">No samples are available.</p>
                )}
              </div>
            </Panel>
          </>
        )}
      </PageBody>
    </>
  );
}
