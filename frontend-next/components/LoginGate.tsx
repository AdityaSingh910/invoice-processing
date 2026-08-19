"use client";

/**
 * Sign-in gate. The API rejects unauthenticated calls regardless of what this
 * overlay does -- it is a convenience for a human, never the control. No secret
 * lives here: the user supplies their own credentials and the server returns a
 * token scoped to them.
 */
import { useState } from "react";
import { useAuth } from "@/lib/auth";

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
    "w-full rounded-lg border border-border bg-panel2 px-3 py-2.5 text-text outline-none " +
    "transition focus:border-accent focus:ring-2 focus:ring-accent/25";

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-bg p-4">
      <form
        onSubmit={submit}
        autoComplete="on"
        className="w-full max-w-[440px] rounded-[var(--radius-card)] border border-border bg-panel p-7 shadow-[var(--shadow-card)]"
      >
        <div className="flex items-center gap-3">
          <span className="grid h-9 w-9 place-items-center rounded-lg bg-accent text-sm font-bold text-white">
            IP
          </span>
          <h1 className="text-xl font-semibold">Invoice Processing</h1>
        </div>
        <p className="mt-2 text-dim">Sign in to process and review invoices.</p>

        <label className="mt-6 block">
          <span className="mb-1.5 block font-semibold">Username</span>
          <input
            className={field}
            type="text"
            name="username"
            autoComplete="username"
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>

        <label className="mt-4 block">
          <span className="mb-1.5 block font-semibold">Password</span>
          <input
            className={field}
            type="password"
            name="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>

        {(error || notice) && (
          <div
            role="alert"
            className="mt-4 rounded-lg border px-3 py-2.5"
            style={{
              borderColor: "var(--fail-solid)",
              background: "var(--fail-soft)",
              color: "var(--fail)",
            }}
          >
            {error || notice}
          </div>
        )}

        <button
          type="submit"
          disabled={busy}
          className="mt-5 w-full rounded-lg bg-accent px-4 py-2.5 font-semibold text-white transition hover:opacity-90 disabled:opacity-60"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="mt-4 text-[13px] leading-relaxed text-faint">
          Demo accounts:{" "}
          {[
            ["analyst", "demo-analyst", "process"],
            ["reviewer", "demo-reviewer", "process + accept/reject"],
            ["admin", "demo-admin", "+ override any run"],
          ].map(([u, p, what], i) => (
            <span key={u}>
              {i > 0 && ", "}
              <code className="rounded bg-panel2 px-1 py-0.5 font-mono text-text">{u}</code> /{" "}
              <code className="rounded bg-panel2 px-1 py-0.5 font-mono text-text">{p}</code> ({what})
            </span>
          ))}
          .
        </p>
      </form>
    </div>
  );
}
