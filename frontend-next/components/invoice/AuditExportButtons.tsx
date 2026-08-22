"use client";

/**
 * "Download audit report" -- a real file the browser saves, not a link to an
 * API endpoint. `downloadFile` (lib/api.ts) fetches WITH the bearer token and
 * triggers a save via a momentary `<a download>`, the same reason the
 * document preview never puts a token in a URL (§7e.5).
 */
import { useState } from "react";
import { downloadFile, ApiError } from "@/lib/api";
import { useToast } from "@/components/ui/Toast";
import { Button } from "@/components/ui";
import { IconDownload } from "@/components/ui/icons";

export default function AuditExportButtons({ runId }: { runId: number }) {
  const toast = useToast();
  const [busy, setBusy] = useState<"pdf" | "csv" | null>(null);

  async function download(format: "pdf" | "csv") {
    setBusy(format);
    try {
      await downloadFile(
        `/api/runs/${runId}/audit-report.${format}`,
        `invoice_run-${runId}_audit_report.${format}`
      );
      toast.push({ tone: "ok", title: "Audit report downloaded" });
    } catch (e) {
      toast.push({
        tone: "bad",
        title: "Download failed",
        detail: e instanceof ApiError ? e.message : "The server could not be reached.",
      });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex items-center gap-1.5">
      <Button
        size="sm"
        variant="ghost"
        icon={<IconDownload size={13} />}
        loading={busy === "pdf"}
        onClick={() => download("pdf")}
      >
        PDF
      </Button>
      <Button
        size="sm"
        variant="ghost"
        icon={<IconDownload size={13} />}
        loading={busy === "csv"}
        onClick={() => download("csv")}
      >
        CSV
      </Button>
    </div>
  );
}
