"use client";

/**
 * Sign-in.
 *
 * A masthead carrying the product and the language picker, a two-column body —
 * a plain statement of what the process does on the left, the authentication
 * card on the right — and a footer strip naming the three properties the API
 * actually has. The left panel is built from the system's real pipeline: the
 * five moves in the flow rail are the nine stages compressed to their decisive
 * points, and the three pillars are what those stages do. It is a description
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

/**
 * The pipeline as a reader meets it, not as the code enumerates it.
 *
 * Nine stages is the truth, and it is what the Process screen shows. On a
 * sign-in screen it was a wall of nine chips nobody reads. These five are those
 * nine at their decisive points -- no move here is invented, and none of the
 * nine is contradicted.
 */
const FLOW = [
  "login.flow.invoice",
  "login.flow.ai",
  "login.flow.validate",
  "login.flow.po",
  "login.flow.decision",
] as const;

/** What those stages are FOR, in one word each. */
const PILLARS = [
  "login.pillar.extraction",
  "login.pillar.validation",
  "login.pillar.decision",
] as const;

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
    <div className="flex min-h-screen flex-col bg-canvas">
      {/* ---------------------------------------------------------- masthead */}
      {/* The picker moved up here from above the form. It is the one control
          someone who cannot read the page needs to find first, and a masthead
          is where a reader looks for it. There is still no token at this point,
          so it falls back to the locale list this bundle carries rather than
          the server's. */}
      <header className="flex items-center justify-between gap-4 border-b border-line px-5 py-3 sm:px-8">
        <div className="flex items-center gap-2.5">
          <span className="grid h-7 w-7 place-items-center rounded-[var(--radius-md)] bg-accent text-[12px] font-bold text-accent-fg">
            AP
          </span>
          <span className="text-[14px] font-semibold tracking-[-0.01em]">Invoice Processing</span>
        </div>
        <LanguagePicker />
      </header>

      <main className="flex flex-1 items-center justify-center px-5 py-10 sm:px-8">
        <div className="grid w-full max-w-[1080px] items-center gap-12 lg:grid-cols-[1.05fr_minmax(360px,0.85fr)] lg:gap-16">
          {/* --------------------------------------------- product statement */}
          <section className="hidden lg:block">
            <p className="t-caption">{t("login.eyebrow")}</p>

            <h1 className="t-display mt-3">
              {t("login.headline.reads")}
              <br />
              {t("login.headline.decides")}
            </h1>

            <p className="mt-4 max-w-[420px] text-[15px] leading-relaxed text-secondary">
              {t("login.tagline")}
            </p>

            {/* Static: this is what the pipeline IS, not a live readout. */}
            <ol className="mt-9 flex flex-wrap items-center gap-2">
              {FLOW.map((key, i) => (
                <li key={key} className="flex items-center gap-2">
                  {i > 0 && (
                    <span aria-hidden className="text-faint">
                      &rarr;
                    </span>
                  )}
                  <span className="rounded-[var(--radius-sm)] border border-line bg-surface px-2.5 py-1 text-[13px] text-secondary">
                    {t(key)}
                  </span>
                </li>
              ))}
            </ol>

            <div className="mt-10 grid max-w-[440px] grid-cols-3 gap-6">
              {PILLARS.map((key) => (
                <div key={key}>
                  <p className="text-[13.5px] font-semibold">{t(key)}</p>
                  <span className="mt-2 block border-t border-line-strong" />
                </div>
              ))}
            </div>
          </section>

          {/* ----------------------------------------------- authentication */}
          <div className="mx-auto w-full max-w-[380px]">
            <div className="rounded-[var(--radius-lg)] border border-line bg-surface p-6 shadow-[var(--shadow-sm)]">
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
            </div>

            {/* ------------------------------------------- demo access panel */}
            {SHOW_DEMO_ACCOUNTS && (
              <div className="mt-5 rounded-[var(--radius-lg)] border border-line bg-surface">
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
                          <span className="flex items-center gap-1.5 text-[13.5px] font-medium">
                            {d.user}
                            {d.external && <Badge tone="accent">{t("login.role.supplier")}</Badge>}
                          </span>
                          <span className="t-meta block text-[12px]">{d.role}</span>
                        </span>
                        {active ? (
                          <span className="text-ok">
                            <IconCheck size={14} />
                          </span>
                        ) : (
                          <span className="t-meta text-[12px]">{t("login.use")}</span>
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </div>
      </main>

      {/* ------------------------------------------------------------ footer */}
      {/* Three claims this API can actually be held to -- OAuth 2.0 bearer
          tokens, per-user scopes re-checked against the live account on every
          request, and an audit trail written as the rules evaluate. Named,
          not illustrated. */}
      <footer className="border-t border-line px-5 py-4 sm:px-8">
        <p className="t-meta flex flex-wrap items-center justify-center gap-2">
          <IconShield size={13} />
          <span>{t("login.footer.secure")}</span>
          <span aria-hidden className="text-faint">
            &middot;
          </span>
          <span>{t("login.footer.roleBased")}</span>
          <span aria-hidden className="text-faint">
            &middot;
          </span>
          <span>{t("login.footer.explainable")}</span>
        </p>
      </footer>
    </div>
  );
}
