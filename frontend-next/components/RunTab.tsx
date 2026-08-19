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
import { STAGE_ORDER, VERDICT_BLURB, VERDICT_HEADLINE, money } from "@/lib/format";
import type { RunResult, SampleInvoice, Stage } from "@/lib/types";
import { Card, Chip, EmptyState, Eyebrow, KvTable, Missing, StatusPill } from "./ui";
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
    <div className="grid gap-5 lg:grid-cols-[340px_minmax(0,1fr)]">
      {/* ---------------- left rail: input ---------------- */}
      <div className="grid content-start gap-5">
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
            className={`grid cursor-pointer place-items-center gap-1.5 rounded-[var(--radius-inner)] border-2 border-dashed px-4 py-8 text-center transition-all ${
              dragging
                ? "scale-[1.02] border-accent bg-accent-soft"
                : "border-border bg-panel2 hover:border-border-strong"
            }`}
          >
            <div className="grid h-11 w-11 place-items-center rounded-full bg-accent-soft text-lg text-accent">
              ⭳
            </div>
            <p className="text-[15px] font-bold">Drop an invoice here</p>
            <p className="text-[13px] text-dim">or click to pick a PDF</p>
            <input
              ref={fileInput}
              type="file"
              accept="application/pdf"
              hidden
              onChange={(e) => e.target.files?.[0] && chooseLocal(e.target.files[0])}
            />
          </div>

          {file && (
            <div className="mt-3 flex items-center gap-2 rounded-[var(--radius-inner)] border border-border bg-panel2 px-3 py-2">
              <span className="text-accent">▣</span>
              <span className="truncate font-mono text-[12px]">{file.name}</span>
            </div>
          )}

          <button onClick={run} disabled={!file || running} className="btn btn-primary mt-3 w-full">
            {running ? (
              <>
                <span className="block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                Running…
              </>
            ) : (
              "Run process"
            )}
          </button>
        </Card>

        <Card
          title="Try a sample"
          aside={<Chip title="Some scenarios depend on earlier runs">order matters</Chip>}
        >
          <div className="grid gap-2">
            {samples.map((s) => {
              const active = selectedSample === s.filename;
              return (
                <button
                  key={s.filename}
                  onClick={() => pickSample(s)}
                  className={`rounded-[var(--radius-inner)] border p-3 text-left transition-all ${
                    active
                      ? "border-accent bg-accent-soft shadow-[var(--shadow-sm)]"
                      : "border-border bg-panel2 hover:border-border-strong hover:bg-panel3"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-[14px] font-semibold">{s.label || s.filename}</span>
                    {s.expect && <StatusPill status={s.expect} glyph={false} />}
                  </div>
                  {s.note && <div className="mt-1 text-[13px] text-dim">{s.note}</div>}
                  <div className="mt-1.5 font-mono text-[11px] text-faint">{s.filename}</div>
                </button>
              );
            })}
          </div>
        </Card>
      </div>

      {/* ---------------- right: pipeline + decision ---------------- */}
      <div className="grid content-start gap-5">
        {result && <VerdictBanner r={result} />}

        {error && (
          <div
            className="rounded-[var(--radius-card)] border px-5 py-4"
            style={{
              borderColor: "var(--fail-border)",
              background: "var(--fail-soft)",
              color: "var(--fail)",
            }}
          >
            <b>Run failed.</b> {error}
          </div>
        )}

        <Card
          title="Pipeline"
          aside={
            <span className="rounded-full bg-panel2 px-2.5 py-1 text-[12px] font-medium text-dim tabular-nums">
              {running && progress === 0
                ? "running…"
                : progress
                  ? `${progress} / ${STAGE_ORDER.length} stages`
                  : "idle"}
            </span>
          }
        >
          <div className="mb-5 h-1.5 w-full overflow-hidden rounded-full bg-panel3">
            <div
              className={`h-full rounded-full transition-[width] duration-500 ease-out ${
                running ? "sheen" : ""
              }`}
              style={{
                width: `${pct}%`,
                background: running
                  ? "linear-gradient(90deg, var(--accent), #7c3aed, var(--accent))"
                  : "var(--accent)",
              }}
            />
          </div>

          {progress === 0 && !running ? (
            <EmptyState
              title="Nothing processed yet"
              sub={
                <>
                  Pick one of the sample invoices on the left and press <em>Run process</em>.
                  You’ll see each step report as it happens.
                </>
              }
            />
          ) : (
            <StageList stages={stages} running={running} />
          )}
        </Card>

        {result?.po_match?.po_number && (
          <Card title="Purchase order budget" aside={<Chip>{result.po_match.matched_via} match</Chip>}>
            <PoBalance pm={result.po_match} />
          </Card>
        )}

        {result && (
          <div className="grid gap-5 xl:grid-cols-2">
            <Card title="Why">
              <ReasonList reasons={result.reasons} />
            </Card>
            <Card title="What it read">
              <ExtractedFields r={result} />
            </Card>
          </div>
        )}

        {result && (
          <Card title="How it decided" aside={<Chip>audit trail</Chip>}>
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

/**
 * The verdict, stated once and large. Everything else on the page is evidence
 * for this line, so it gets the strongest treatment on the screen.
 */
function VerdictBanner({ r }: { r: RunResult }) {
  const pm = r.po_match;
  const tone = r.status === "APPROVED" ? "ok" : r.status === "REJECTED" ? "fail" : "warn";
  const glyph = r.status === "APPROVED" ? "✓" : r.status === "REJECTED" ? "✕" : "?";

  return (
    <div
      data-testid="verdict-bar"
      className="pop relative overflow-hidden rounded-[var(--radius-card)] shadow-[var(--shadow-soft)]"
    >
      {/* A soft wash in the verdict's own colour: the outcome is legible from
          across the room before a single number is read. */}
      <div
        aria-hidden
        className="absolute inset-0"
        style={{
          background: `linear-gradient(130deg, var(--${tone}-soft) 0%, var(--panel) 60%)`,
        }}
      />
      <div
        aria-hidden
        className="absolute -top-16 -right-10 h-56 w-56 rounded-full opacity-20 blur-3xl"
        style={{ background: `var(--${tone}-solid)` }}
      />

      <div className="relative flex flex-wrap items-center justify-between gap-6 p-6">
        <div className="flex min-w-0 items-center gap-4">
          <span
            className="grid h-14 w-14 shrink-0 place-items-center rounded-full text-[26px] font-black text-white"
            style={{
              background: `var(--grad-${tone})`,
              boxShadow: `0 10px 24px -8px var(--${tone}-solid)`,
            }}
          >
            {glyph}
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2.5">
              <h2
                className="text-[26px] leading-tight font-black tracking-[-0.03em]"
                style={{ color: `var(--${tone})` }}
              >
                {VERDICT_HEADLINE[r.status] ?? r.status}
              </h2>
              {/* The formal verdict stays on screen beside the friendly line --
                  the plain wording softens delivery, never meaning. */}
              <span className="pill" data-status={r.status} data-testid="verdict-status">
                {r.status.replace("_", " ")}
              </span>
            </div>
            <p className="mt-1 text-[14px] text-dim">{VERDICT_BLURB[r.status]}</p>
            <p className="mt-1.5 truncate text-[13px] text-faint">
              <span className="font-semibold text-dim">{r.filename}</span> · run #{r.run_id} ·{" "}
              {r.extracted.vendor_name || "unknown vendor"}
              {r.extracted.invoice_number ? ` · ${r.extracted.invoice_number}` : ""}
            </p>
          </div>
        </div>

        <div className="flex gap-3">
          <Figure label="invoice total" value={money(r.extracted.total)} />
          {pm.po_number && <Figure label="left on the PO" value={money(pm.remaining_before)} />}
        </div>
      </div>
    </div>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[var(--radius-inner)] border border-border bg-panel/70 px-4 py-3 text-right backdrop-blur-sm">
      <div className="text-[26px] leading-none font-black tracking-[-0.03em] tabular-nums">
        {value}
      </div>
      <div className="mt-1.5 text-[10px] font-extrabold tracking-[0.08em] text-faint uppercase">
        {label}
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
        [
          "Total",
          e.total != null ? (
            <b key="t" className="text-[15px]">
              {money(e.total)}
            </b>
          ) : (
            <Missing key="t" />
          ),
        ],
        ["Line items", (e.line_items || []).length || "—"],
        ["Currency", e.currency || "—"],
        [
          "Extraction route",
          <span
            key="r"
            className="rounded-md bg-accent-soft px-1.5 py-0.5 font-mono text-[12px] text-accent"
          >
            {e.extraction_method}
          </span>,
        ],
      ]}
    />
  );
}

export { Eyebrow };
