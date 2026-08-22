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
import AppShell, {
  navIdFor,
  type NavId,
  type Navigate,
  type Section,
} from "@/components/layout/AppShell";
import AnalyticsPage from "@/components/pages/AnalyticsPage";
import AssistantPage from "@/components/pages/AssistantPage";
import OverviewPage from "@/components/pages/OverviewPage";
import ProcessPage from "@/components/pages/ProcessPage";
import InvoicesPage from "@/components/pages/InvoicesPage";
import PortalApp from "@/components/portal/PortalApp";
import ReferencePage from "@/components/pages/ReferencePage";
import { Spinner } from "@/components/ui";

export default function Home() {
  const { user, ready, can } = useAuth();
  const [section, setSection] = useState<Section>("overview");
  // Set only by a navigation that promised a filtered view (Overview's "Open
  // review queue", or the sidebar's own "Review queue" item); consumed once
  // by InvoicesPage's own initial state, so it does not fight the reviewer's
  // own filter choice on the way back.
  const [invoicesFilter, setInvoicesFilter] = useState<"EXCEPTIONS" | undefined>(undefined);
  // Same idea for Reference's two nav entries (Purchase orders / Approved
  // vendors), which both open the one page with a different tab preselected.
  const [referenceTab, setReferenceTab] = useState<"orders" | "vendors" | undefined>(undefined);
  // Which sidebar ROW is lit. Seven rows share five sections, so the section
  // alone cannot say — see NavId in AppShell. Derived from the same options
  // the caller already passes, so navigate()'s signature is unchanged.
  const [navId, setNavId] = useState<NavId>("overview");
  // Bumped when a run finishes or a review lands, so every view refetches.
  const [reloadKey, setReloadKey] = useState(0);

  const navigate: Navigate = (s, opts) => {
    setInvoicesFilter(opts?.exceptionsOnly ? "EXCEPTIONS" : undefined);
    setReferenceTab(opts?.referenceTab);
    setNavId(navIdFor(s, opts));
    setSection(s);
  };

  // An external client (Phase J). Identified by what the token carries rather
  // than by a role name, because scopes are what the server enforces and a
  // deployment may name its roles anything.
  const isPortalClient = can("portal:read") && !can("invoice:read");

  // Gated on `user` AND on this not being a client: the internal endpoints
  // 403 for a client token, and firing them here would fill the console with
  // failures for a screen that never renders.
  const internal = !!user && !isPortalClient;
  const runs = useRuns(reloadKey, internal);
  const reference = useReference(internal);

  if (!ready) {
    return (
      <div className="grid min-h-screen place-items-center">
        <span className="flex items-center gap-2.5 text-[14px] text-muted">
          <Spinner />
          Loading
        </span>
      </div>
    );
  }

  if (!user) return <LoginGate />;

  // EXTERNAL CLIENTS GET A DIFFERENT APPLICATION, not this one with rows
  // hidden (Phase J).
  //
  // The branch is total on purpose. A supplier never mounts AppShell, so
  // there is no internal navigation to hide, no section a stale piece of
  // state could reach, and no shared nav array an internal feature could be
  // added to and appear on a vendor's screen. It is checked here rather than
  // inside the shell because a shell that had to know about both audiences is
  // exactly the thing that eventually shows one of them the other's.
  //
  // Not a security control either way -- the portal endpoints resolve the
  // caller server-side and the internal ones refuse a client token outright.
  // This decides which product the person is looking at.
  if (isPortalClient) return <PortalApp />;

  const openExceptions = runs.data ? totals(runs.data).openExceptions : 0;
  const refresh = () => setReloadKey((k) => k + 1);

  return (
    <AppShell section={section} activeId={navId} onNavigate={navigate} badge={openExceptions}>
      {section === "overview" && (
        <OverviewPage runs={runs} reference={reference} onNavigate={navigate} />
      )}
      {/* Analytics fetches its own data: the figures are computed by the
          server per reporting window, so there is nothing here to hand down
          and nothing that would go stale if there were. */}
      {section === "analytics" && <AnalyticsPage />}
      {section === "assistant" && <AssistantPage />}
      {section === "process" && <ProcessPage runs={runs} onRan={refresh} />}
      {section === "invoices" && <InvoicesPage runs={runs} initialFilter={invoicesFilter} />}
      {/* Email review queue: fetches its own data, same reason as Settings --
          it is about held messages, not about the shared run/reference data. */}
      {section === "reference" && (
        <ReferencePage reference={reference} runs={runs} initialTab={referenceTab} />
      )}
    </AppShell>
  );
}
