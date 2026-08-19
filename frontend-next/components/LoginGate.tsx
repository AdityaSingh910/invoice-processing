"use client";

/**
 * Sign-in gate. The API rejects unauthenticated calls regardless of what this
 * overlay does -- it is a convenience for a human, never the control. No secret
 * lives here: the user supplies their own credentials and the server returns a
 * token scoped to them.
 */
import { useState } from "react";
import { useAuth } from "@/lib/auth";

const DEMO_ACCOUNTS: [string, string, string][] = [
  ["analyst", "demo-analyst", "process invoices"],
  ["reviewer", "demo-reviewer", "process + accept/reject"],
  ["admin", "demo-admin", "override any run"],
];

export default function LoginGate() {
  const { signIn, notice } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(username.trim(), password);
      setPassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  }

  const field =
    "w-full rounded-[var(--radius-inner)] border border-border bg-panel2 px-3.5 py-2.5 text-[15px] " +
    "text-text outline-none transition-all placeholder:text-faint focus:border-accent focus:bg-panel";

  return (
    <div className="relative grid min-h-screen place-items-center overflow-hidden p-5">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(60rem 40rem at 50% -10%, var(--bg-accent), transparent 70%)",
        }}
      />

      <form onSubmit={submit} autoComplete="on" className="card relative w-full max-w-[430px] p-8">
        <div className="flex items-center gap-3">
          <span
            className="grid h-11 w-11 place-items-center rounded-2xl text-[15px] font-bold text-white shadow-[var(--shadow)]"
            style={{ background: "linear-gradient(135deg, var(--accent), #7c3aed)" }}
          >
            IP
          </span>
          <div>
            <h1 className="text-[20px] font-semibold tracking-[-0.02em]">Invoice Processing</h1>
            <p className="text-[13px] text-dim">The AI reads. The rules decide.</p>
          </div>
        </div>

        <div className="mt-7 grid gap-4">
          <label className="block">
            <span className="mb-1.5 block text-[13px] font-semibold">Username</span>
            <input
              className={field}
              type="text"
              name="username"
              autoComplete="username"
              placeholder="analyst"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>

          <label className="block">
            <span className="mb-1.5 block text-[13px] font-semibold">Password</span>
            <input
              className={field}
              type="password"
              name="password"
              autoComplete="current-password"
              placeholder="••••••••"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
        </div>

        {(error || notice) && (
          <div
            role="alert"
            className="mt-4 flex items-start gap-2.5 rounded-[var(--radius-inner)] border px-3.5 py-2.5 text-[14px]"
            style={{
              borderColor: "var(--fail-border)",
              background: "var(--fail-soft)",
              color: "var(--fail)",
            }}
          >
            <span className="mt-0.5 font-bold" aria-hidden>
              !
            </span>
            <span>{error || notice}</span>
          </div>
        )}

        <button type="submit" disabled={busy} className="btn btn-primary mt-5 w-full">
          {busy ? (
            <>
              <span className="block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              Signing in…
            </>
          ) : (
            "Sign in"
          )}
        </button>

        <div className="mt-7 border-t border-border pt-5">
          <div className="eyebrow mb-2.5">Demo accounts</div>
          <div className="grid gap-1.5">
            {DEMO_ACCOUNTS.map(([u, p, what]) => (
              <button
                key={u}
                type="button"
                onClick={() => {
                  setUsername(u);
                  setPassword(p);
                  setError(null);
                }}
                className="flex items-center justify-between gap-3 rounded-[var(--radius-inner)] border border-border bg-panel2 px-3 py-2 text-left transition-colors hover:border-accent hover:bg-accent-soft"
              >
                <span className="font-mono text-[13px] font-semibold">{u}</span>
                <span className="text-[12px] text-dim">{what}</span>
              </button>
            ))}
          </div>
          <p className="mt-2.5 text-[12px] text-faint">Click one to fill the form.</p>
        </div>
      </form>
    </div>
  );
}
