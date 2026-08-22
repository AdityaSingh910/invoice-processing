"use client";

/**
 * Sign-in.
 *
 * Split layout: product identity and a plain statement of what the process does
 * on the left, the authentication card on the right. The left panel is built
 * from the system's real rule names and pipeline stages — it is a description
 * of this software, not decoration, and it contains no invented figures.
 *
 * The API rejects unauthenticated calls regardless of what this screen does; it
 * is a convenience for a human, never the control. No secret lives here.
 */
import { useState } from "react";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { Badge, Button, Callout, Field, Input } from "@/components/ui";
import { IconAlert, IconCheck, IconShield, IconUser } from "@/components/ui/icons";
import LanguagePicker from "@/components/ui/LanguagePicker";

/**
 * Whether to offer the demo credentials below.
 *
 * These accounts exist so an evaluator can open the case study and be inside it
 * in one click, and `data/users.json` really does ship them. They are published
 * in this repository, which is precisely why `APP_ENV=production` refuses to
 * start while any of them is in the user store.
 *
 * So on a real deployment this panel is worse than useless: the accounts are
 * not there to sign in with, and a sign-in box that lists credentials which do
 * not work reads as a broken product before it reads as a demo.
 *
 * `NEXT_PUBLIC_API_BASE_URL` is the signal, and it needs no new configuration
 * to be correct. It is empty for exactly one arrangement -- the single process
 * that serves this UI and the API together, which IS the local demo -- and set
 * for any deployment where the UI is hosted apart from the API. Nothing here is
 * a security control: the server rejects these credentials on its own, and
 * hiding the panel only stops the app advertising sign-ins it cannot honour.
 */
const SHOW_DEMO_ACCOUNTS = !process.env.NEXT_PUBLIC_API_BASE_URL;

const DEMO = [
  { user: "analyst", pass: "demo-analyst", role: "Process invoices" },
  { user: "reviewer", pass: "demo-reviewer", role: "Process, accept, reject" },
  { user: "admin", pass: "demo-admin", role: "Full administrative access" },
  // Phase J. Two SUPPLIER accounts, which sign in here exactly as an employee
  // does -- there is no separate client login and no second token issuer -- and
  // land in the supplier portal rather than in this application. Marked so an
  // evaluator can tell before clicking that these two show a different product,
  // not a narrower view of this one.
  { user: "acme", pass: "demo-acme", role: "Supplier portal — Acme", external: true },
  {
    user: "globex",
    pass: "demo-globex",
    role: "Supplier portal — Globex, view only",
    external: true,
  },
];

/** The nine stages, named as the pipeline names them. */
const PIPELINE = [
  "Ingest",
  "Extract text",
  "Extract fields",
  "Validate",
  "Vendor check",
  "PO match",
  "Duplicate check",
  "Tolerance check",
  "Decision",
];

export default function LoginGate() {
  const { signIn, notice } = useAuth();
  const t = useT();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(username.trim(), password);
      setPassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : t("login.failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-[1.05fr_minmax(420px,0.95fr)]">
      {/* ------------------------------------------------- product identity */}
      <aside className="relative hidden flex-col justify-between border-r border-line bg-surface p-10 lg:flex">
        <div className="flex items-center gap-2.5">
          <span className="grid h-7 w-7 place-items-center rounded-[var(--radius-md)] bg-accent text-[11px] font-bold text-accent-fg">
            AP
          </span>
          <span className="text-[13px] font-semibold tracking-[-0.01em]">Invoice Processing</span>
        </div>

        <div className="max-w-[440px]">
          <h1 className="t-display">
            The AI reads.
            <br />
            The rules decide.
          </h1>
          <p className="mt-4 text-[14px] leading-relaxed text-secondary">
            Vendor invoices are read by a language model, then judged by
            deterministic Python. No model ever touches a dollar comparison, so
            the same invoice produces the same verdict every time — and the
            reasoning is on record.
          </p>

          {/* The nine stages, as a compact rail. Static: this is what the
              pipeline is, not a live readout. */}
          <div className="mt-9">
            <p className="t-caption mb-3">Nine checks, every invoice</p>
            <ol className="flex flex-wrap gap-1.5">
              {PIPELINE.map((stage, i) => (
                <li
                  key={stage}
                  className="flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-line bg-sunken px-2 py-1"
                >
                  <span className="tnum text-[10px] text-faint">{i + 1}</span>
                  <span className="text-[11.5px] text-secondary">{stage}</span>
                </li>
              ))}
            </ol>
          </div>

          <div className="mt-9 flex flex-wrap gap-2">
            <Badge tone="ok" dot>
              Approved
            </Badge>
            <Badge tone="warn" dot>
              Needs review
            </Badge>
            <Badge tone="bad" dot>
              Rejected
            </Badge>
          </div>
        </div>

        <p className="t-meta flex items-center gap-1.5">
          <IconShield size={13} />
          OAuth 2.0 bearer tokens, scoped per user, rate limited
        </p>
      </aside>

      {/* --------------------------------------------------- authentication */}
      <main className="flex items-center justify-center p-6">
        <div className="w-full max-w-[380px]">
          <div className="mb-6 lg:hidden">
            <div className="flex items-center gap-2.5">
              <span className="grid h-7 w-7 place-items-center rounded-[var(--radius-md)] bg-accent text-[11px] font-bold text-accent-fg">
                AP
              </span>
              <span className="text-[13px] font-semibold">Invoice Processing</span>
            </div>
          </div>

          {/* The picker sits ABOVE the form on purpose: it is the one control
              on this page that someone who cannot read the page needs to find
              first, and there is no token yet, so it falls back to the list
              this bundle carries. */}
          <div className="mb-3 flex justify-end">
            <LanguagePicker />
          </div>

          <h2 className="t-page">{t("login.title")}</h2>
          <p className="t-meta mt-1">{t("login.scopedNote")}</p>

          <form onSubmit={submit} autoComplete="on" className="mt-6 flex flex-col gap-4">
            <Field label={t("login.username")} htmlFor="username">
              <Input
                id="username"
                name="username"
                autoComplete="username"
                placeholder={SHOW_DEMO_ACCOUNTS ? "analyst" : ""}
                required
                value={username}
                onChange={(e) => {
                  setUsername(e.currentTarget.value);
                  setSelected(null);
                }}
              />
            </Field>

            <Field label={t("login.password")} htmlFor="password">
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

            {(error || notice) && (
              <Callout tone="bad" icon={<IconAlert size={13} />}>
                {error || notice}
              </Callout>
            )}

            <Button type="submit" variant="primary" className="h-9 w-full" loading={busy}>
              {busy ? t("login.working") : t("login.submit")}
            </Button>
          </form>

          {/* ------------------------------------------- demo access panel */}
          {SHOW_DEMO_ACCOUNTS && (
          <div className="mt-7 rounded-[var(--radius-lg)] border border-line bg-surface">
            <div className="flex items-center justify-between border-b border-line px-3 py-2">
              <span className="t-caption">{t("login.demo")}</span>
              <Badge tone="accent">Evaluation</Badge>
            </div>
            <div className="p-1">
              {DEMO.map((d) => {
                const active = selected === d.user;
                return (
                  <button
                    key={d.user}
                    type="button"
                    onClick={() => {
                      setUsername(d.user);
                      setPassword(d.pass);
                      setSelected(d.user);
                      setError(null);
                    }}
                    className={`flex w-full items-center gap-2.5 rounded-[var(--radius-md)] px-2.5 py-2
                      text-left transition-colors ${active ? "bg-hover" : "hover:bg-hover"}`}
                  >
                    <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-sunken text-faint">
                      <IconUser size={12} />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5 text-[12.5px] font-medium">
                        {d.user}
                        {d.external && <Badge tone="accent">{t("login.role.supplier")}</Badge>}
                      </span>
                      <span className="t-meta block text-[11px]">{d.role}</span>
                    </span>
                    {active ? (
                      <span className="text-ok">
                        <IconCheck size={14} />
                      </span>
                    ) : (
                      <span className="t-meta text-[11px]">{t("login.use")}</span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
          )}
        </div>
      </main>
    </div>
  );
}
