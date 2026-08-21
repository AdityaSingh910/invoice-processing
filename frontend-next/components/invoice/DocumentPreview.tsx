"use client";

/**
 * The original invoice document, rendered where real bytes actually exist.
 *
 * Two genuine sources, no third:
 *  - `file`: the in-memory File just uploaded in this browser tab (the live
 *    processing screen has it for free — nothing is fetched).
 *  - `filename` matching one of the sample invoices: the real bytes are
 *    served back from GET /api/sample-invoices/{name}, which already exists.
 *
 * The backend stores only a run's filename, never its bytes (see
 * backend/storage.py — the `runs` table has no blob column). So a historical
 * run opened later that is NOT a sample has no document to show. That is
 * shown honestly as an empty state rather than a fabricated page render.
 *
 * Rendering itself is the browser's native PDF viewer (<object>/<embed>),
 * not a reimplementation — it already has its own zoom, search and paging.
 */
import { useEffect, useMemo, useState } from "react";
import { apiFetch, apiJson } from "@/lib/api";
import type { SampleInvoice } from "@/lib/types";
import { EmptyState, Spinner } from "@/components/ui";
import { IconFile, IconLink } from "@/components/ui/icons";

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

export default function DocumentPreview({
  file,
  filename,
}: {
  /** The live File object, when this is the run that was just processed in
   *  this tab. Takes priority over fetching — it is strictly more current. */
  file?: File | null;
  filename?: string | null;
}) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "unavailable">("loading");

  const liveUrl = useMemo(() => (file ? URL.createObjectURL(file) : null), [file]);

  useEffect(() => {
    if (liveUrl) {
      setBlobUrl(liveUrl);
      setState("ready");
      return () => URL.revokeObjectURL(liveUrl);
    }

    if (!filename) {
      setState("unavailable");
      return;
    }

    let cancelled = false;
    setState("loading");
    setBlobUrl(null);

    (async () => {
      const samples = await sampleIndex();
      if (cancelled) return;
      if (!samples.has(filename)) {
        setState("unavailable");
        return;
      }
      const res = await apiFetch(`/api/sample-invoices/${encodeURIComponent(filename)}`);
      if (cancelled) return;
      if (!res.ok) {
        setState("unavailable");
        return;
      }
      const blob = await res.blob();
      if (cancelled) return;
      const url = URL.createObjectURL(blob);
      setBlobUrl(url);
      setState("ready");
    })();

    return () => {
      cancelled = true;
    };
  }, [filename, liveUrl]);

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
        <span className="flex items-center gap-2 text-[12.5px] text-muted">
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
          title="Original document not stored"
          description="Only the extracted data and the audit trail are retained after processing. The source PDF itself is not persisted server-side."
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
          <p className="text-[12.5px]">This browser cannot preview a PDF inline.</p>
          <a
            href={blobUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-[12.5px] font-medium text-accent hover:underline"
          >
            <IconLink size={12} />
            Open the document in a new tab
          </a>
        </div>
      </object>
    </div>
  );
}
