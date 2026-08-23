"use client";

/**
 * The original invoice document, rendered where real bytes actually exist.
 *
 * Three genuine sources, tried in this order, no fourth:
 *  - `file`: the in-memory File just uploaded in this browser tab (the live
 *    processing screen has it for free — nothing is fetched).
 *  - `runId`: the PDF Phase C persisted for that run, fetched from
 *    GET /api/runs/{id}/document/download?inline=1. `inline=1` is what an
 *    embedded viewer needs (Content-Disposition: inline); the bytes are the
 *    real uploaded file either way.
 *  - `filename` matching one of the sample invoices: served back from
 *    GET /api/sample-invoices/{name}. This is the fallback for a demo run
 *    whose stored copy is gone — the sample bytes are the same document.
 *
 * THIS COMPONENT USED TO SKIP THE MIDDLE ONE, and its docstring said the
 * backend "stores only a run's filename, never its bytes". That was true
 * before Phase C and false after it: `documents` holds the metadata and a
 * DocumentStore holds the bytes (§5). So every non-sample run reported
 * "not stored" over a PDF the server had all along.
 *
 * When nothing resolves, the reason comes from the server rather than being
 * assumed — "no PDF was ever kept for this run" and "the stored copy is
 * gone" are different facts with different remedies.
 *
 * Rendering itself is the browser's native PDF viewer (<object>/<embed>),
 * not a reimplementation — it already has its own zoom, search and paging.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, apiJson } from "@/lib/api";
import type { SampleInvoice } from "@/lib/types";
import { EmptyState, Spinner } from "@/components/ui";
import { IconFile, IconLink } from "@/components/ui/icons";

/** Where the bytes on screen came from. `none` means there are none. */
export type DocumentSource = "live" | "stored" | "sample" | "none";

export type ResolvedDocument = {
  source: DocumentSource;
  /** An object URL for the bytes, when there are any. */
  url: string | null;
};

let sampleIndexCache: Promise<Set<string>> | null = null;

/** The sample filenames, fetched once per session and shared by every caller
 *  — this component may mount many times (register rows) without refetching. */
function sampleIndex(): Promise<Set<string>> {
  if (!sampleIndexCache) {
    sampleIndexCache = apiJson<SampleInvoice[]>("/api/sample-invoices")
      .then((list) => new Set(list.map((s) => s.filename)))
      .catch(() => new Set<string>());
  }
  return sampleIndexCache;
}

/** Both 404s the download endpoint can give, told apart by their own detail
 *  line rather than guessed at from the status code they share. */
async function unavailableReason(res: Response): Promise<string> {
  let detail = "";
  try {
    detail = ((await res.json()) as { detail?: string }).detail || "";
  } catch {
    /* not JSON — fall through to the generic wording */
  }
  if (/no longer available/i.test(detail)) {
    return "The stored copy of this PDF is no longer on file. The extracted fields and the audit trail are unaffected.";
  }
  if (/no document is stored/i.test(detail)) {
    return "No PDF was kept for this run. The extracted fields and the audit trail are unaffected.";
  }
  return "The source PDF could not be loaded. The extracted fields and the audit trail are unaffected.";
}

export default function DocumentPreview({
  file,
  filename,
  runId,
  onResolved,
}: {
  /** The live File object, when this is the run that was just processed in
   *  this tab. Takes priority over fetching — it is strictly more current. */
  file?: File | null;
  filename?: string | null;
  /** The run whose stored PDF to fetch. Absent on the pre-run preview, where
   *  there is no run yet. */
  runId?: number | null;
  /** Told what resolved, so a caller can offer a download of exactly the
   *  bytes on screen rather than a button that might 404. */
  onResolved?: (doc: ResolvedDocument) => void;
}) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "unavailable">("loading");
  const [reason, setReason] = useState<string | null>(null);

  const liveUrl = useMemo(() => (file ? URL.createObjectURL(file) : null), [file]);

  // Held in a ref so a caller passing an inline arrow does not re-run the
  // fetch on every render.
  const onResolvedRef = useRef(onResolved);
  onResolvedRef.current = onResolved;
  const report = useCallback((doc: ResolvedDocument) => onResolvedRef.current?.(doc), []);

  useEffect(() => {
    if (liveUrl) {
      setBlobUrl(liveUrl);
      setState("ready");
      report({ source: "live", url: liveUrl });
      return () => URL.revokeObjectURL(liveUrl);
    }

    let cancelled = false;
    setState("loading");
    setBlobUrl(null);
    setReason(null);

    (async () => {
      let fallbackReason: string | null = null;

      // 1. The PDF this run was actually processed from.
      if (runId != null) {
        const res = await apiFetch(`/api/runs/${runId}/document/download?inline=1`);
        if (cancelled) return;
        if (res.ok) {
          const url = URL.createObjectURL(await res.blob());
          if (cancelled) {
            URL.revokeObjectURL(url);
            return;
          }
          setBlobUrl(url);
          setState("ready");
          report({ source: "stored", url });
          return;
        }
        fallbackReason = await unavailableReason(res);
        if (cancelled) return;
      }

      // 2. A sample invoice, whose bytes ship with the application — so a
      //    demo run still previews after a redeploy cleared the store.
      if (filename) {
        const samples = await sampleIndex();
        if (cancelled) return;
        if (samples.has(filename)) {
          const res = await apiFetch(`/api/sample-invoices/${encodeURIComponent(filename)}`);
          if (cancelled) return;
          if (res.ok) {
            const url = URL.createObjectURL(await res.blob());
            if (cancelled) {
              URL.revokeObjectURL(url);
              return;
            }
            setBlobUrl(url);
            setState("ready");
            report({ source: "sample", url });
            return;
          }
        }
      }

      setReason(fallbackReason);
      setState("unavailable");
      report({ source: "none", url: null });
    })();

    return () => {
      cancelled = true;
    };
  }, [filename, liveUrl, runId, report]);

  useEffect(() => {
    // Revoke fetched (non-live) blob URLs on unmount/replacement; the live
    // one is owned and revoked by the effect above.
    return () => {
      if (blobUrl && blobUrl !== liveUrl) URL.revokeObjectURL(blobUrl);
    };
  }, [blobUrl, liveUrl]);

  if (state === "loading") {
    return (
      <div className="flex h-full min-h-[240px] items-center justify-center">
        <span className="flex items-center gap-2 text-[13.5px] text-muted">
          <Spinner size={14} />
          Loading document
        </span>
      </div>
    );
  }

  if (state === "unavailable" || !blobUrl) {
    return (
      <div className="flex h-full min-h-[240px] items-center justify-center p-6">
        <EmptyState
          icon={<IconFile size={16} />}
          title="Original document not available"
          description={
            reason ??
            "No PDF was kept for this run. The extracted fields and the audit trail are unaffected."
          }
        />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-[240px] flex-col">
      <object
        // PDF open parameters, honoured by Chrome's built-in viewer.
        //   navpanes=0  drops the page-thumbnail sidebar, which at this pane
        //               width took roughly half the space and left the invoice
        //               itself rendered too small to read.
        //   view=FitH   scales the page to the pane width instead of to a
        //               default zoom chosen for a full browser window.
        // The toolbar is deliberately KEPT: zoom, search, page and print are
        // the viewer's own and are the reason this embeds a real PDF viewer
        // rather than reimplementing one.
        data={`${blobUrl}#navpanes=0&view=FitH`}
        type="application/pdf"
        className="min-h-[420px] w-full flex-1"
        aria-label={filename ? `Invoice document: ${filename}` : "Invoice document"}
      >
        {/* Browsers that cannot embed a PDF (rare, mostly mobile) fall through here. */}
        <div className="flex h-full flex-col items-center justify-center gap-2.5 p-6 text-center">
          <IconFile size={20} className="text-faint" />
          <p className="text-[13.5px]">This browser cannot preview a PDF inline.</p>
          <a
            href={blobUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-[13.5px] font-medium text-accent hover:underline"
          >
            <IconLink size={12} />
            Open the document in a new tab
          </a>
        </div>
      </object>
    </div>
  );
}
