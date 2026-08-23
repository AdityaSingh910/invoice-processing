"use client";

/**
 * Sign-in.
 *
 * A dark hero over three drifting rows of sample records, with the
 * authentication card beside it. The moving cards are DECORATION and are built
 * as such -- pointer-events off, masked away at both edges, and stopped dead by
 * prefers-reduced-motion. Nothing on this screen reads from the API, because
 * there is no token yet to read it with.
 *
 * WHAT THE CARDS SAY IS THE REAL VOCABULARY. INV-/PO- references, APPROVED /
 * NEEDS REVIEW / REJECTED, "Within tolerance", "Duplicate check", "Vendor
 * approved", "PO match" -- every one of those is a rule name or a status this
 * pipeline actually produces (§3). They are sample rows, not live ones, and the
 * figures are illustrative; nothing here claims to be a reading of the ledger.
 *
 * THE FIELDS ARE PLAIN ELEMENTS RATHER THAN THE SHARED Input/Field. Those two
 * are painted from the theme tokens, and this screen deliberately commits to a
 * dark palette whatever theme the reader last chose (see globals.css) -- so a
 * token-driven label would be dark-on-dark for anyone who signed out of light
 * mode. The AUTHENTICATION is untouched: same signIn(), same state, same error
 * handling, same demo gate.
 *
 * The API rejects unauthenticated calls regardless of what this screen does; it
 * is a convenience for a human, never the control. No secret lives here.
 */
import { useRef, useState } from "react";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { IconAlert, IconCheck, IconUser } from "@/components/ui/icons";

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
 *
 * Read straight from process.env so the minifier folds the branch and the five
 * passwords below are dead-code-eliminated rather than merely hidden.
 */
const SHOW_DEMO_ACCOUNTS = !process.env.NEXT_PUBLIC_API_BASE_URL;

/**
 * The credential the sign-in box opens with, so a visitor can just press the
 * button.
 *
 * THIS IS A PUBLIC CREDENTIAL AND IS MEANT TO BE. It ships in the browser
 * bundle in clear text, which is not a leak -- it is the point. Anyone who
 * reaches this page has it.
 *
 * IT ONLY WORKS IF THE SERVER'S USER STORE ACTUALLY HAS THIS ACCOUNT, and that
 * is two separate provisionings, not one. `data/users.json` carries it for the
 * local demo (flagged `demo`, like every other account shipped in this
 * repository); a real deployment reads `AUTH_USERS_JSON` instead, and the entry
 * there must NOT carry the flag -- `APP_ENV=production` refuses to start while
 * any flagged account is in the store (§8), which is the guard that keeps the
 * SHIPPED accounts out of a real deployment and must stay working.
 *
 * Prefilled fields that fail are worse than empty ones: the visitor presses
 * Sign in, gets "incorrect username or password", and concludes the product is
 * broken rather than that they were handed a credential nobody provisioned. So
 * if this constant and the deployment's user store ever disagree, this constant
 * is the one that is wrong.
 *
 * THE ROLE IS `reviewer`, AND IT IS A CEILING RATHER THAN A DEFAULT. Whatever
 * scopes this account holds, every visitor holds. `reviewer` reaches the whole
 * product -- upload an invoice, watch the nine stages, work the review queue,
 * accept or reject -- while stopping short of `invoice:admin`, which would hand
 * every passer-by the authority to override a decision and to clear the run
 * history from the Overview screen. Widening it is a decision about what
 * strangers may do, not a convenience.
 */
const OPENING_CREDENTIAL = { username: "demo", password: "zampisthebest" };

const DEMO = [
  // The one the box already opens on. Listed anyway rather than left implicit:
  // somebody who types over the prefilled fields to try another account has no
  // other way back to it, and a panel that omitted the account actually in the
  // inputs would be describing a different set of credentials than the form is
  // holding.
  { user: OPENING_CREDENTIAL.username, pass: OPENING_CREDENTIAL.password,
    role: "Process, accept, reject" },
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

/* -------------------------------------------------------------- the cards */

/**
 * THREE LINES, IN THE ORDER SOMEBODY READS THEM: what this row is, the one
 * figure that matters, and what happened to it.
 *
 * The first pass gave every card a reference, a status pill, a vendor, an
 * amount and a footnote -- five things competing at five weights, multiplied
 * by seventeen cards in constant motion. Individually legible, collectively
 * noise. A drifting card is glanced at, never read, so it gets one focal
 * point and nothing else may compete with it: the subject is muted and small,
 * the figure carries all the weight, the outcome is a quiet line beneath.
 *
 * The status pill is gone entirely. Its colour is now a 6px dot on the
 * outcome line, which is the whole of what a badge was communicating at this
 * size anyway.
 */
type Tone = "ok" | "warn" | "bad" | "plain";

interface SampleCard {
  /** What this is about -- reference and party, or the name of the check. */
  subject: string;
  /** The one figure. */
  value: string;
  /** What became of it. */
  outcome: string;
  tone: Tone;
}

const DOT: Record<Tone, string> = {
  ok: "bg-emerald-400",
  warn: "bg-amber-400",
  bad: "bg-rose-400",
  plain: "bg-white/30",
};

// Row one: invoices, as the register lists them.
const ROW_INVOICES: SampleCard[] = [
  { subject: "INV-1042 · Acme Office Supplies", value: "₹1,234.28", outcome: "Approved · matched PO-1001", tone: "ok" },
  { subject: "INV-2287 · Globex Logistics", value: "$8,400.00", outcome: "Needs review · spans two orders", tone: "warn" },
  { subject: "INV-3310 · Initech Supplies", value: "€2,000.00", outcome: "Approved · converted at the pinned rate", tone: "ok" },
  { subject: "INV-1042 · Acme Office Supplies", value: "₹1,234.28", outcome: "Rejected · duplicate check", tone: "bad" },
  { subject: "INV-7701 · Wayne Facilities", value: "$6,500.00", outcome: "Approved · within tolerance", tone: "ok" },
  { subject: "INV-5064 · Soylent Foods", value: "₹78,900.00", outcome: "Needs review · low extraction confidence", tone: "warn" },
];

// Row two: the orders those invoices are billed against.
const ROW_ORDERS: SampleCard[] = [
  { subject: "PO-1001 · Acme Office Supplies", value: "₹1,240.00", outcome: "remaining of ₹2,474.28", tone: "plain" },
  { subject: "PO-1006 · Wayne Facilities", value: "$6,500.00", outcome: "remaining of $13,000.00", tone: "plain" },
  { subject: "PO-1008 · Initech Supplies", value: "$0.00", outcome: "fully billed", tone: "ok" },
  { subject: "PO-1002 · Globex Logistics", value: "$5,000.00", outcome: "remaining of $9,400.00", tone: "plain" },
  { subject: "PO-1004 · Soylent Foods", value: "₹21,100.00", outcome: "remaining of ₹100,000.00", tone: "plain" },
];

// Row three: the checks themselves, in the words the audit trail uses.
const ROW_CHECKS: SampleCard[] = [
  { subject: "Duplicate check", value: "9 of 9", outcome: "checks completed, none short-circuited", tone: "ok" },
  { subject: "Within tolerance", value: "$12.40", outcome: "under a $50.00 allowance", tone: "ok" },
  { subject: "Vendor approved", value: "V-001", outcome: "matched on a normalised name", tone: "ok" },
  { subject: "Confidence gate", value: "0.58", outcome: "held · below the 0.65 threshold", tone: "warn" },
  { subject: "PO match", value: "PO-1006", outcome: "bound to the allocation ledger", tone: "ok" },
  { subject: "Arithmetic", value: "±0.01", outcome: "subtotal plus tax equals total", tone: "ok" },
];

function Card({ card }: { card: SampleCard }) {
  return (
    <article
      aria-hidden
      className="mr-4 w-[272px] shrink-0 rounded-xl border border-white/[0.07] bg-white/[0.03]
        px-5 py-[18px] sm:w-[296px]"
    >
      <p className="truncate text-[12px] leading-none text-white/40">{card.subject}</p>
      <p className="mt-3 text-[25px] leading-none font-semibold tracking-[-0.025em] text-white/90">
        {card.value}
      </p>
      <p className="mt-3 flex items-center gap-2 text-[11.5px] leading-none text-white/35">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT[card.tone]}`} />
        <span className="truncate">{card.outcome}</span>
      </p>
    </article>
  );
}

/** One drifting row. The content is rendered TWICE so the -50% shift loops
 *  seamlessly -- see `.marquee-track` in globals.css for why the spacing lives
 *  on the card rather than on the track. */
function Row({
  cards,
  direction,
  duration,
}: {
  cards: SampleCard[];
  direction: "rtl" | "ltr";
  duration: string;
}) {
  return (
    <div className="marquee-mask overflow-hidden">
      <div
        className={`marquee-track ${direction === "rtl" ? "marquee-rtl" : "marquee-ltr"}`}
        style={{ ["--marquee-duration" as string]: duration }}
      >
        {[...cards, ...cards].map((card, i) => (
          <Card key={`${card.subject}-${i}`} card={card} />
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */

export default function LoginGate() {
  const { signIn, notice } = useAuth();
  const t = useT();
  const [username, setUsername] = useState(OPENING_CREDENTIAL.username);
  const [password, setPassword] = useState(OPENING_CREDENTIAL.password);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const usernameRef = useRef<HTMLInputElement>(null);

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
    <div className="relative min-h-screen overflow-hidden bg-[#0a0a0c] text-white">
      {/* ------------------------------------------------------- background */}
      {/* Two soft pools of colour rather than a flat black, so the cards have
          something to sit in. Fixed, so a short viewport does not reveal an
          edge when the page scrolls. */}
      <div aria-hidden className="pointer-events-none fixed inset-0">
        <div className="absolute -top-1/4 left-1/2 h-[560px] w-[900px] -translate-x-1/2 rounded-full bg-[#2b4bff]/[0.13] blur-[130px]" />
        <div className="absolute -bottom-1/3 right-0 h-[460px] w-[720px] rounded-full bg-[#00d5b8]/[0.07] blur-[130px]" />
      </div>

      {/* The three rows. Row one drifts right-to-left, row two the other way,
          row three back again -- opposing directions read as motion rather
          than as one sheet sliding. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-[7%] flex select-none flex-col gap-5 opacity-[0.5]"
      >
        <Row cards={ROW_INVOICES} direction="rtl" duration="72s" />
        <Row cards={ROW_ORDERS} direction="ltr" duration="86s" />
        <Row cards={ROW_CHECKS} direction="rtl" duration="64s" />
      </div>

      {/* A scrim, so the copy over the top of all that stays readable. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(120%_90%_at_50%_45%,rgba(10,10,12,0.55)_0%,rgba(10,10,12,0.88)_55%,#0a0a0c_100%)]"
      />

      {/* ---------------------------------------------------------- content */}
      <div className="relative flex min-h-screen flex-col">
        <header className="flex items-center justify-between gap-4 px-5 py-4 sm:px-8">
          <div className="flex items-center gap-2.5">
            <span className="grid h-8 w-8 place-items-center rounded-[10px] bg-[#2b4bff] text-[12px] font-bold text-white">
              AP
            </span>
            <span className="text-[14px] font-semibold tracking-[-0.01em]">Invoice Processing</span>
          </div>
        </header>

        <main className="flex flex-1 items-center justify-center px-5 py-10 sm:px-8">
          {/* One column, centred. The hero copy and its call to action are
              gone: the button never signed anybody in -- there is nothing to
              submit before a credential exists -- so it only scrolled to the
              form it was sitting next to. With the form centred there is
              nothing left for it to do. */}
          <div className="w-full max-w-[400px]">
            {/* --------------------------------------------- authentication */}
            <div className="w-full">
              <div className="rounded-2xl border border-white/[0.09] bg-white/[0.045] p-6 shadow-[0_28px_70px_-24px_rgba(0,0,0,0.9)] backdrop-blur-xl sm:p-7">
                <h2 className="text-[21px] font-semibold tracking-[-0.02em]">{t("login.title")}</h2>

                <form onSubmit={submit} autoComplete="on" className="mt-6 flex flex-col gap-4">
                  <div>
                    <label
                      htmlFor="username"
                      className="mb-1.5 block text-[12.5px] font-medium text-white/60"
                    >
                      {t("login.username")}
                    </label>
                    <input
                      ref={usernameRef}
                      id="username"
                      name="username"
                      className="signin-field"
                      autoComplete="username"
                      placeholder={SHOW_DEMO_ACCOUNTS ? "analyst" : ""}
                      required
                      value={username}
                      onChange={(e) => {
                        setUsername(e.currentTarget.value);
                        setSelected(null);
                      }}
                    />
                  </div>

                  <div>
                    <label
                      htmlFor="password"
                      className="mb-1.5 block text-[12.5px] font-medium text-white/60"
                    >
                      {t("login.password")}
                    </label>
                    <input
                      id="password"
                      name="password"
                      type="password"
                      className="signin-field"
                      autoComplete="current-password"
                      placeholder="••••••••"
                      required
                      value={password}
                      onChange={(e) => setPassword(e.currentTarget.value)}
                    />
                  </div>

                  {(error || notice) && (
                    <p
                      role="alert"
                      className="flex items-start gap-2 rounded-xl border border-rose-400/25 bg-rose-400/10 px-3 py-2.5 text-[12.5px] text-rose-200"
                    >
                      <IconAlert size={13} className="mt-px shrink-0" />
                      <span>{error || notice}</span>
                    </p>
                  )}

                  <button
                    type="submit"
                    disabled={busy}
                    className="mt-1 inline-flex h-11 w-full items-center justify-center rounded-xl bg-[#2b4bff]
                      text-[14px] font-semibold text-white transition-colors hover:bg-[#3d59ff]
                      disabled:cursor-not-allowed disabled:opacity-60
                      focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
                      focus-visible:outline-[#2b4bff]"
                  >
                    {busy ? t("login.working") : t("login.submit")}
                  </button>
                </form>
              </div>

              {/* ----------------------------------------- demo access panel */}
              {SHOW_DEMO_ACCOUNTS && (
                <div className="mt-4 overflow-hidden rounded-2xl border border-white/[0.09] bg-white/[0.03] backdrop-blur-xl">
                  <div className="flex items-center justify-between border-b border-white/[0.08] px-3.5 py-2.5">
                    <span className="text-[11.5px] font-semibold tracking-wide text-white/55">
                      {t("login.demo")}
                    </span>
                    <span className="rounded-full bg-white/[0.06] px-2 py-[3px] text-[10px] font-semibold text-white/50 ring-1 ring-inset ring-white/12">
                      Evaluation
                    </span>
                  </div>
                  <div className="p-1.5">
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
                          className={`flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left
                            transition-colors ${active ? "bg-white/[0.08]" : "hover:bg-white/[0.05]"}`}
                        >
                          <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-white/[0.06] text-white/45">
                            <IconUser size={12} />
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="flex items-center gap-1.5 text-[12.5px] font-medium text-white/90">
                              {d.user}
                              {d.external && (
                                <span className="rounded-full bg-[#2b4bff]/20 px-1.5 py-px text-[9.5px] font-semibold text-[#9db0ff]">
                                  {t("login.role.supplier")}
                                </span>
                              )}
                            </span>
                            <span className="block text-[11px] text-white/40">{d.role}</span>
                          </span>
                          {active ? (
                            <IconCheck size={14} className="text-emerald-400" />
                          ) : (
                            <span className="text-[11px] text-white/30">{t("login.use")}</span>
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
      </div>
    </div>
  );
}
