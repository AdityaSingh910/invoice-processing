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
    return (
      <div className="grid min-h-screen place-items-center">
        <div className="flex items-center gap-3 text-dim">
          <span className="block h-4 w-4 animate-spin rounded-full border-2 border-accent border-t-transparent" />
          Loading…
        </div>
      </div>
    );
  }
  if (!user) return <LoginGate />;

  return (
    <div className="min-h-screen">
      {/* A soft three-colour mesh instead of a flat canvas. Decorative only,
          and low enough in contrast that nothing on top of it shifts. */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0"
        style={{
          background:
            "radial-gradient(50rem 30rem at 8% -8%, var(--bg-tint-a), transparent 60%)," +
            "radial-gradient(45rem 28rem at 95% -12%, var(--bg-tint-b), transparent 60%)," +
            "radial-gradient(40rem 26rem at 60% 105%, var(--bg-tint-c), transparent 60%)",
        }}
      />

      <header className="sticky top-0 z-20 border-b border-border bg-panel/80 backdrop-blur-xl">
        <div className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-4 px-5 py-3">
          <div className="flex items-center gap-3">
            <span
              className="grid h-10 w-10 place-items-center rounded-2xl text-[14px] font-black text-white shadow-[var(--shadow)]"
              style={{ background: "var(--grad-accent)" }}
            >
              IP
            </span>
            <div className="leading-tight">
              <div className="grad-text text-[17px] font-black tracking-[-0.02em]">Invoice Processing</div>
              <div className="text-[12px] text-faint">
                From PDF to decision — and it shows its working
              </div>
            </div>
          </div>

          {/* Segmented control rather than underlined tabs: it reads as one
              object, which suits three peers better than a nav bar. */}
          <nav className="flex gap-1 rounded-full border border-border bg-panel2 p-1">
            {TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                aria-current={tab === t.key}
                className={`rounded-full px-4 py-1.5 text-[14px] font-bold transition-all ${
                  tab === t.key ? "text-white shadow-[var(--shadow-sm)]" : "text-dim hover:text-text"
                }`}
                style={tab === t.key ? { background: "var(--grad-accent)" } : undefined}
              >
                {t.label}
              </button>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2.5">
            <div className="flex items-center gap-2 rounded-full border border-border bg-panel2 py-1 pr-3 pl-1">
              <span className="grid h-7 w-7 place-items-center rounded-full text-[11px] font-black text-white uppercase"
                style={{ background: "var(--grad-accent)" }}>
                {user.username.slice(0, 2)}
              </span>
              <span className="text-[13px] font-medium">{user.username}</span>
            </div>
            <button onClick={() => signOut()} className="btn btn-ghost px-3 py-1.5 text-[13px]">
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="relative mx-auto max-w-[1440px] p-5">
        {/* Tabs mount on demand: the dashboard refetches on entry, which is what
            makes a just-finished run show up without a manual refresh. */}
        {tab === "run" && <RunTab onRan={() => setReloadKey((k) => k + 1)} />}
        {tab === "dashboard" && <Dashboard reloadKey={reloadKey} />}
        {tab === "reference" && <ReferenceTab />}
      </main>
    </div>
  );
}
