"use client";

/**
 * Entry point: auth gate, then the shell and whichever section is active.
 *
 * Run and reference data are fetched once here and passed down, so switching
 * sections does not refetch and the sidebar's exception badge always agrees
 * with the Invoices table.
 *
 * Client-rendered throughout. Everything behind the gate is per-user,
 * per-token data that only exists after someone signs in, so there is nothing
 * for a server render to produce — which is why the production build is a
 * static export the FastAPI server can hand out directly.
 */
import { useState } from "react";
import { useAuth } from "@/lib/auth";
import { useReference, useRuns } from "@/lib/useData";
import { totals } from "@/lib/metrics";
import LoginGate from "@/components/LoginGate";
import AppShell, { type Navigate, type Section } from "@/components/layout/AppShell";
import OverviewPage from "@/components/pages/OverviewPage";
import ProcessPage from "@/components/pages/ProcessPage";
import InvoicesPage from "@/components/pages/InvoicesPage";
import ReferencePage from "@/components/pages/ReferencePage";
import { Spinner } from "@/components/ui";

export default function Home() {
  const { user, ready } = useAuth();
  const [section, setSection] = useState<Section>("overview");
  // Set only by a navigation that promised a filtered view (Overview's "Open
  // review queue"); consumed once by InvoicesPage's own initial state, so it
  // does not fight the reviewer's own filter choice on the way back.
  const [invoicesFilter, setInvoicesFilter] = useState<"EXCEPTIONS" | undefined>(undefined);
  // Bumped when a run finishes or a review lands, so every view refetches.
  const [reloadKey, setReloadKey] = useState(0);

  const navigate: Navigate = (s, opts) => {
    setInvoicesFilter(opts?.exceptionsOnly ? "EXCEPTIONS" : undefined);
    setSection(s);
  };

  // Gated on `user`: fetching before the token exists 401s and would sign the
  // user out at the moment they sign in.
  const runs = useRuns(reloadKey, !!user);
  const reference = useReference(!!user);

  if (!ready) {
    return (
      <div className="grid min-h-screen place-items-center">
        <span className="flex items-center gap-2.5 text-[13px] text-muted">
          <Spinner />
          Loading
        </span>
      </div>
    );
  }

  if (!user) return <LoginGate />;

  const openExceptions = runs.data ? totals(runs.data).openExceptions : 0;
  const refresh = () => setReloadKey((k) => k + 1);

  return (
    <AppShell section={section} onNavigate={navigate} badge={openExceptions}>
      {section === "overview" && (
        <OverviewPage runs={runs} reference={reference} onNavigate={navigate} />
      )}
      {section === "process" && <ProcessPage runs={runs} onRan={refresh} />}
      {section === "invoices" && <InvoicesPage runs={runs} initialFilter={invoicesFilter} />}
      {section === "reference" && <ReferencePage reference={reference} runs={runs} />}
    </AppShell>
  );
}
