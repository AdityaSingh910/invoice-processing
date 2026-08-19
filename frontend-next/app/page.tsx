"use client";

/**
 * The shell: sign-in gate, top bar, and the three tabs.
 *
 * Client-rendered throughout. Everything behind the gate is per-user, per-token
 * data that only exists after someone signs in, so there is nothing for a server
 * render to produce -- which is why the production build is a static export the
 * FastAPI server can hand out directly.
 */
import { useState } from "react";
import { useAuth } from "@/lib/auth";
import LoginGate from "@/components/LoginGate";
import RunTab from "@/components/RunTab";
import Dashboard from "@/components/Dashboard";
import ReferenceTab from "@/components/ReferenceTab";

type Tab = "run" | "dashboard" | "reference";

const TABS: { key: Tab; label: string }[] = [
  { key: "run", label: "Run" },
  { key: "dashboard", label: "Dashboard" },
  { key: "reference", label: "Reference" },
];

export default function Home() {
  const { user, ready, signOut } = useAuth();
  const [tab, setTab] = useState<Tab>("run");
  // Bumped when a run finishes or a review lands, so the dashboard refetches
  // instead of showing a stale ledger.
  const [reloadKey, setReloadKey] = useState(0);

  if (!ready) {
    return <div className="grid min-h-screen place-items-center text-dim">Loading…</div>;
  }
  if (!user) return <LoginGate />;

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-border bg-panel/85 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-4 px-4 py-2.5">
          <div className="flex items-center gap-2.5">
            <span className="grid h-8 w-8 place-items-center rounded-lg bg-accent text-[13px] font-bold text-white">
              IP
            </span>
            <div className="leading-tight">
              <div className="font-semibold">Invoice Processing</div>
              <div className="text-[12px] text-faint">PDF → decision, with the reasoning visible</div>
            </div>
          </div>

          <nav className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={`rounded-lg px-3 py-1.5 font-medium transition ${
                  tab === t.key
                    ? "bg-accent-soft text-accent"
                    : "text-dim hover:bg-panel2 hover:text-text"
                }`}
              >
                {t.label}
              </button>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-3">
            <span className="text-dim">{user.username}</span>
            <button
              onClick={() => signOut()}
              className="rounded-lg border border-border px-2.5 py-1 text-dim transition hover:border-border-strong hover:text-text"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1400px] p-4">
        {/* Tabs stay mounted-on-demand: the dashboard refetches on entry, which
            is what makes a just-finished run show up without a manual refresh. */}
        {tab === "run" && <RunTab onRan={() => setReloadKey((k) => k + 1)} />}
        {tab === "dashboard" && <Dashboard reloadKey={reloadKey} />}
        {tab === "reference" && <ReferenceTab />}
      </main>
    </div>
  );
}
