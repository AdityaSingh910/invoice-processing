"use client";

/**
 * The processing workflow.
 *
 * Two states by design. Before a run this is a focused upload screen; once a
 * run starts, the phase stepper and the evidence take over. Rendering empty
 * result panels beside an upload box is the single thing that makes an internal
 * tool feel unfinished.
 *
 * THE RUN NO LONGER LIVES IN THIS COMPONENT.
 *
 * It used to: `streamRun` read the pipeline out of a response body, so the
 * pipeline only advanced while this page's fetch was alive. Refreshing aborted
 * the fetch, the server cancelled the response task and the pipeline with it,
 * and -- because a run is only written at the DECISION stage -- nothing was
 * persisted at all. The upload had not failed; it had stopped existing.
 *
 * Now the upload hands the file over and gets a JOB ID back. The server reads
 * the invoice on its own worker and writes what it finds. This screen holds no
 * part of the work: it asks `GET /api/jobs/{id}` how things are going, which is
 * a question it can ask again after a reload, in another tab, or tomorrow.
 *
 * The job id is remembered in sessionStorage, and that is NOT state the work
 * depends on -- it is a bookmark. Losing it (a new tab, a cleared session) is
 * survivable: this screen then asks the server for anything of yours still
 * running, which is the same question by another route.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiFetch, apiJson, fetchActiveJobs, fetchJob, startRun } from "@/lib/api";
import { STAGE_ORDER } from "@/lib/format";
import type { ProcessingJob, RunRecord, RunResult, SampleInvoice, Stage } from "@/lib/types";
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

/** How often to ask the server how the invoice is going. A second is well
 *  inside the pace of the pipeline's own stages and costs one small read. */
const POLL_MS = 1000;

/**
 * Which job THIS TAB is watching.
 *
 * sessionStorage rather than localStorage, for the same reason the bearer
 * token lives there: it dies with the tab. It holds an id and nothing else --
 * the state of the work is the server's, always re-read, never cached here.
 */
const JOB_KEY = "ip.process.job";

function rememberJob(id: string | null) {
  try {
    if (id) sessionStorage.setItem(JOB_KEY, id);
    else sessionStorage.removeItem(JOB_KEY);
  } catch {
    /* private mode: the bookmark is lost, the job is not */
  }
}

function rememberedJob(): string | null {
  try {
    return sessionStorage.getItem(JOB_KEY);
  } catch {
    return null;
  }
}

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
  // The job this screen is watching. Everything shown about a run is derived
  // from what the server says about this id.
  const [jobId, setJobId] = useState<string | null>(null);
  // Settled once, not once per poll: the register refresh and the toast are
  // both one-shot.
  const settled = useRef<Set<string>>(new Set());
  // Jobs THIS TAB started. Only these are toasted -- a reload that lands on a
  // job finishing in the background should still refresh the register (the run
  // is real and belongs in it), but announcing a verdict for work the reader
  // did not just watch reads as a notification from nowhere.
  const startedHere = useRef<Set<string>>(new Set());

  useEffect(() => {
    apiJson<SampleInvoice[]>("/api/sample-invoices")
      .then(setSamples)
      .catch(() => setSamples([]));
  }, []);

  /**
   * PICK THE WORK BACK UP AFTER A RELOAD.
   *
   * Two routes, in order, because they answer different situations:
   *   1. this tab was already watching a job -- restore exactly that one,
   *      finished or not, so a refresh mid-run is invisible and a refresh
   *      after the verdict still shows the verdict;
   *   2. nothing remembered (a new tab, a reopened browser) -- ask the server
   *      whether anything of mine is STILL RUNNING, and adopt the newest.
   *
   * Only the second is narrowed to active jobs. Adopting a finished job in a
   * fresh tab would ambush someone with a result they did not just produce;
   * adopting a running one tells them something they need to know.
   */
  useEffect(() => {
    let cancelled = false;

    const remembered = rememberedJob();
    if (remembered) {
      // Adopted, not announced. It is deliberately NOT marked settled here:
      // a job restored while still running must still refresh the register
      // when it finishes, or a reader who refreshed mid-run would be left
      // with a stale Invoices table until they went and reloaded it.
      setJobId(remembered);
      setRunning(true);          // corrected by the first poll a moment later
      return;
    }

    fetchActiveJobs()
      .then((list) => {
        if (cancelled || list.length === 0) return;
        rememberJob(list[0].job_id);
        setJobId(list[0].job_id);
        setRunning(true);
      })
      .catch(() => {
        /* nothing to restore is the ordinary case, not an error worth showing */
      });

    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Follow one job until it settles.
   *
   * Polling, not a socket: the answer is a row in Postgres, one small read a
   * second while an invoice is being read and none at all afterwards. It also
   * degrades the way the rest of this application does -- a request that fails
   * is retried on the next tick rather than ending the run, because the run is
   * not here to end.
   */
  useEffect(() => {
    if (!jobId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const settle = (job: ProcessingJob) => {
      if (settled.current.has(job.job_id)) return;
      settled.current.add(job.job_id);
      const r = job.result;
      if (job.status === "completed" && r && startedHere.current.has(job.job_id)) {
        toast.push({
          tone: r.status === "APPROVED" ? "ok" : r.status === "REJECTED" ? "bad" : "warn",
          title: `${r.filename} — ${r.status.replace(/_/g, " ").toLowerCase()}`,
          detail: `Nine checks completed. Run #${r.run_id}.`,
        });
      }
      // Refresh the shared run/reference data either way: a completed run
      // belongs in the register, and a failed one changes nothing but costs
      // one read to confirm.
      onRan?.();
    };

    const tick = async () => {
      try {
        const job = await fetchJob(jobId);
        if (stopped) return;
        setStages(job.stages || []);
        const live = job.status === "queued" || job.status === "processing";
        setRunning(live);
        if (job.status === "completed" && job.result) {
          setResult(job.result);
          setError(null);
        } else if (job.status === "failed") {
          setResult(null);
          setError(
            job.error_message ||
              "Processing failed. The invoice was not recorded — try uploading it again."
          );
        }
        if (!live) {
          settle(job);
          return;                       // settled: stop polling
        }
      } catch (e) {
        if (stopped) return;
        // The job is genuinely gone -- the run history was cleared, or this is
        // a stale bookmark. Forget it rather than polling a 404 for ever.
        if (e instanceof ApiError && e.status === 404) {
          rememberJob(null);
          setJobId(null);
          setRunning(false);
          return;
        }
        // Anything else is this reader's connection, not the invoice: the work
        // is on the server and carries on regardless, so try again.
      }
      timer = setTimeout(tick, POLL_MS);
    };

    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
    // `toast` and `onRan` are intentionally not dependencies: re-running this
    // effect would restart the poll loop for a job already being followed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

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

  /**
   * Hand the invoice over. This function no longer waits for the verdict.
   *
   * All it does is start the work and remember which job it is -- the polling
   * effect above does the rest. Which is the whole fix: there is nothing here
   * for a refresh to interrupt, because the reading is not happening here.
   *
   * A resubmission of a file already being read comes back as the SAME job
   * (`duplicate: true`, decided by the server against a live-job index), so a
   * double-click or a retry joins the work in progress instead of starting a
   * second read of the same PDF.
   */
  async function run() {
    if (!file) return;
    setRunning(true);
    setError(null);
    setStages([]);
    setResult(null);
    try {
      const job = await startRun(file);
      startedHere.current.add(job.job_id);
      rememberJob(job.job_id);
      setJobId(job.job_id);
    } catch (e) {
      setRunning(false);
      setError(e instanceof Error ? e.message : "The invoice could not be accepted.");
    }
  }

  function reset() {
    setFile(null);
    setPicked(null);
    setStages([]);
    setResult(null);
    setError(null);
    // Stop following the job and drop the bookmark. The job itself is
    // untouched -- it is finished, and its run is in the register.
    rememberJob(null);
    setJobId(null);
    setRunning(false);
  }

  const processed = new Set((runs?.data ?? []).map((r) => r.filename));
  const alreadyRun = (samples ?? []).filter((s) => processed.has(s.filename)).length;

  /**
   * The MOST RECENT run per filename — what actually happened, as opposed to
   * what a sample is documented to do.
   *
   * `list_runs` is `ORDER BY id DESC`, so the first row seen for a filename is
   * its latest run and later ones are earlier attempts. That matters here
   * specifically because a race can be re-run to get the other outcome: the
   * card has to show THIS run's verdict, not the first one ever recorded.
   */
  const latestByFilename = new Map<string, RunRecord>();
  for (const r of runs?.data ?? []) {
    if (!latestByFilename.has(r.filename)) latestByFilename.set(r.filename, r);
  }

  const pickedSample = (samples ?? []).find((s) => s.filename === picked);
  const raceGroup = pickedSample?.outcome === "race" ? pickedSample.race_group : undefined;
  const raceField = (samples ?? []).filter(
    (s) => s.outcome === "race" && s.race_group && s.race_group === raceGroup
  );

  /**
   * Start every invoice in one race group at once.
   *
   * This is not a special processing path and it is not a simulation: it is
   * `startRun` per file, exactly what the single Run button does, dispatched
   * together so the requests are in flight at the same time. Each lands on its
   * own worker, runs the same nine stages, and commits through the same
   * `save_run_checked` — where the loser blocks on the `SELECT ... FOR UPDATE`
   * held by the winner, re-reads the balance, and is held for review.
   *
   * Nothing here decides the outcome, and nothing here can: this function does
   * not know which invoice is which, and both requests are identical in kind.
   * The winner is whichever transaction reaches the PO row lock first.
   *
   * The main panel follows the FIRST job so the stage list has something to
   * show; both verdicts arrive on the sample cards from the register, which
   * `onRan()` refreshes when the followed job settles.
   */
  async function runRace() {
    if (!raceField.length || running) return;
    setRunning(true);
    setError(null);
    setStages([]);
    setResult(null);
    try {
      const files = await Promise.all(
        raceField.map(async (s) => {
          const res = await apiFetch(`/api/sample-invoices/${encodeURIComponent(s.filename)}`);
          if (!res.ok) throw new Error(`${s.label || s.filename} could not be loaded.`);
          return new File([await res.blob()], s.filename, { type: "application/pdf" });
        })
      );
      // Dispatched together, deliberately: awaiting them in sequence would put
      // a full round trip between the two uploads and the second would very
      // often find the first already committed, which is not a race.
      const jobs = await Promise.all(files.map((f) => startRun(f)));
      jobs.forEach((j) => startedHere.current.add(j.job_id));
      rememberJob(jobs[0].job_id);
      setJobId(jobs[0].job_id);
    } catch (e) {
      setRunning(false);
      setError(e instanceof Error ? e.message : "The race could not be started.");
    }
  }

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
              showPipeline
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
                  {/* THE DROPZONE IS FOR CHOOSING A FILE, SO IT GOES ONCE ONE
                      IS CHOSEN. Leaving it up asked the reader to drop a PDF
                      underneath a panel already previewing the PDF they had
                      just dropped, and put a 140px target between the file and
                      the button that acts on it. What stays is the file, with
                      its size and a way to remove it, and the button. Removing
                      the file brings this back. */}
                  {!file && (
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
                    </div>
                  )}

                  {/* Outside the dropzone, so it is still mounted once the
                      dropzone is gone -- everything that opens the file picker
                      goes through this one input. */}
                  <input
                    ref={input}
                    type="file"
                    accept="application/pdf"
                    hidden
                    onChange={(e) => e.target.files?.[0] && choose(e.target.files[0])}
                  />

                  {file && (
                    <div className="rise mt-4 flex items-center gap-2.5 rounded-[var(--radius-md)] border border-line bg-sunken px-3 py-2">
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

                  {/* Only when a contended sample is selected. Running one of a
                      race on its own is a legitimate thing to do — it is just
                      an ordinary partial invoice — so this is offered beside
                      the normal button rather than replacing it. */}
                  {raceField.length > 1 && (
                    <>
                      <Button
                        variant="secondary"
                        className="mt-2 h-9 w-full"
                        onClick={runRace}
                        disabled={running}
                      >
                        {`Run both at once — race for ${raceGroup}`}
                      </Button>
                      <p className="t-meta mt-2 text-[12px] leading-snug">
                        Sends {raceField.length} invoices in the same instant. Both charge $4,000
                        to a $7,000 order, so only one can be approved — the winner is whichever
                        transaction reaches the PO row lock first, and it is genuinely either.
                        Run it again to see it go the other way.
                      </p>
                    </>
                  )}
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
                  // A race sample carries no `expect` from the server (see
                  // list_sample_invoices), so it shows what it ACTUALLY did
                  // last time, or nothing at all if it has not run yet.
                  // Everything else keeps the documented badge it always had.
                  const isRace = s.outcome === "race";
                  const actual = isRace ? latestByFilename.get(s.filename)?.status : undefined;
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
                          {/* A race sample is MEANT to be run more than once —
                              re-running is how you get the other outcome — so
                              the duplicate warning would be wrong on it. Its
                              two invoices carry different invoice numbers and
                              charge the same PO, so a second pass is a fresh
                              race, not a resubmission. */}
                          {already && !isRace && (
                            <span
                              title="Already processed — running it again will be rejected as a duplicate"
                              aria-label="Already processed"
                              className="h-1.5 w-1.5 shrink-0 rounded-full bg-warn-vivid"
                            />
                          )}
                          {isRace ? (
                            actual ? (
                              <StatusBadge status={actual} />
                            ) : (
                              <Badge tone="neutral">Ready to run</Badge>
                            )
                          ) : (
                            s.expect && <StatusBadge status={s.expect} />
                          )}
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
