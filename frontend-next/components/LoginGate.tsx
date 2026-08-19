"use client";

/**
 * Sign-in.
 *
 * The API rejects unauthenticated calls regardless of what this screen does —
 * it is a convenience for a human, never the control. No secret lives here: the
 * user supplies their own credentials and the server returns a token scoped to
 * them.
 */
import { useState } from "react";
import { useAuth } from "@/lib/auth";
import { Button, Callout, Field, Input } from "@/components/ui";
import { IconAlert } from "@/components/ui/icons";

const DEMO: { user: string; pass: string; role: string }[] = [
  { user: "analyst", pass: "demo-analyst", role: "Process invoices" },
  { user: "reviewer", pass: "demo-reviewer", role: "Process, accept and reject" },
  { user: "admin", pass: "demo-admin", role: "Full administrative access" },
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

  return (
    <div className="grid min-h-screen place-items-center p-4">
      <div className="w-full max-w-[400px]">
        <div className="mb-6 flex items-center gap-2.5">
          <span className="grid h-9 w-9 place-items-center rounded-[var(--radius-md)] bg-accent text-[13px] font-bold text-accent-fg">
            AP
          </span>
          <div className="leading-tight">
            <h1 className="text-[16px] font-semibold tracking-[-0.01em]">Invoice Processing</h1>
            <p className="text-[12px] text-subtle">The AI reads. The rules decide.</p>
          </div>
        </div>

        <form
          onSubmit={submit}
          autoComplete="on"
          className="rounded-[var(--radius-lg)] border border-border bg-surface p-6 shadow-[var(--shadow-sm)]"
        >
          <h2 className="text-[15px] font-semibold">Sign in</h2>
          <p className="mt-1 text-[13px] text-muted">
            Access is scoped to your account&apos;s permissions.
          </p>

          <div className="mt-5 flex flex-col gap-4">
            <Field label="Username" htmlFor="username">
              <Input
                id="username"
                name="username"
                autoComplete="username"
                placeholder="analyst"
                required
                value={username}
                onChange={(e) => setUsername(e.currentTarget.value)}
              />
            </Field>

            <Field label="Password" htmlFor="password">
              <Input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                placeholder="••••••••"
                required
                value={password}
                onChange={(e) => setPassword(e.currentTarget.value)}
              />
            </Field>
          </div>

          {(error || notice) && (
            <div className="mt-4">
              <Callout tone="danger" icon={<IconAlert size={14} />}>
                {error || notice}
              </Callout>
            </div>
          )}

          <Button type="submit" variant="primary" className="mt-5 w-full" loading={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <div className="mt-4 rounded-[var(--radius-lg)] border border-border bg-surface2 p-4">
          <p className="label mb-2.5">Demo accounts</p>
          <div className="flex flex-col gap-1">
            {DEMO.map((d) => (
              <button
                key={d.user}
                type="button"
                onClick={() => {
                  setUsername(d.user);
                  setPassword(d.pass);
                  setError(null);
                }}
                className="flex items-center justify-between gap-3 rounded-[var(--radius-sm)] px-2 py-1.5 text-left transition-colors hover:bg-surface3"
              >
                <span className="num text-[13px] font-medium">{d.user}</span>
                <span className="text-[12px] text-subtle">{d.role}</span>
              </button>
            ))}
          </div>
          <p className="mt-2.5 text-[12px] text-subtle">Select one to fill the form.</p>
        </div>
      </div>
    </div>
  );
}
