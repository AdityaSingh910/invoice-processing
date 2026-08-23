"use client";

/**
 * "Download" for the invoice PDF on screen — the source document itself, not
 * the audit report AuditExportButtons already offers.
 *
 * It downloads exactly what the preview beside it resolved, which is why it
 * takes the resolved source rather than a run id alone:
 *
 *  - `stored` goes back to GET /api/runs/{id}/document/download WITHOUT
 *    `inline`, so the server sets `Content-Disposition: attachment` and names
 *    the file, and the fetch is recorded as DOCUMENT_DOWNLOADED in the run's
 *    activity history (§5). A download of invoice data is an action on
 *    invoice data and belongs in that history, exactly as viewing it does.
 *  - `live` and `sample` save the object URL the preview already holds. There
 *    is nothing to fetch and nothing to audit: the live file came from this
 *    browser, and a sample is content that ships with the application.
 *
 * The request carries the bearer token as a header, never in the URL — the
 * same reason the preview fetches rather than linking (§7e.5).
 */
import { useState } from "react";
import { downloadFile, ApiError } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { Button } from "@/components/ui";
import { IconDownload } from "@/components/ui/icons";
import type { ResolvedDocument } from "./DocumentPreview";

export default function DocumentDownloadButton({
  doc,
  runId,
  filename,
}: {
  doc: ResolvedDocument;
  runId?: number | null;
  filename?: string | null;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  // Nothing resolved, so there is nothing to hand over. A button that 404s is
  // worse than no button: it reads as the application losing the file.
  if (doc.source === "none") return null;

  const saveAs = filename || (runId != null ? `invoice-run-${runId}.pdf` : "invoice.pdf");

  async function download() {
    if (doc.source === "stored" && runId != null) {
      setBusy(true);
      try {
        await downloadFile(`/api/runs/${runId}/document/download`, saveAs);
      } catch (e) {
        toast.push({
          tone: "bad",
          title: "Download failed",
          detail: e instanceof ApiError ? e.message : "The server could not be reached.",
        });
      } finally {
        setBusy(false);
      }
      return;
    }

    if (!doc.url) return;
    const a = document.createElement("a");
    a.href = doc.url;
    a.download = saveAs;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  return (
    <Button
      // xs with a negative margin: this sits in the document panel's thin
      // header strip beside the filename, and a taller control would push
      // that strip open for no gain.
      size="xs"
      variant="ghost"
      className="-my-1 shrink-0"
      icon={<IconDownload size={12} />}
      loading={busy}
      onClick={download}
      title={`Download ${saveAs}`}
    >
      Download
    </Button>
  );
}
