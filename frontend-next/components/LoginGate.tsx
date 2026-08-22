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
 *
 * Read straight from process.env so the minifier folds the branch and the five
 * passwords below are dead-code-eliminated rather than merely hidden.
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

/* -------------------------------------------------------------- the cards */

type Tone = "ok" | "warn" | "bad" | "plain";

interface SampleCard {
  tag: string;
  tone: Tone;
  ref: string;
  sub: string;
  value: string;
  foot: string;
}

const TONE: Record<Tone, { chip: string; dot: string }> = {
  ok: { chip: "bg-emerald-400/12 text-emerald-300 ring-emerald-400/25", dot: "bg-emerald-400" },
  warn: { chip: "bg-amber-400/12 text-amber-300 ring-amber-400/25", dot: "bg-amber-400" },
  bad: { chip: "bg-rose-400/12 text-rose-300 ring-rose-400/25", dot: "bg-rose-400" },
  plain: { chip: "bg-white/[0.06] text-white/70 ring-white/15", dot: "bg-white/40" },
};

// Row one: invoices, as the register lists them.
const ROW_INVOICES: SampleCard[] = [
  { tag: "APPROVED", tone: "ok", ref: "INV-1042", sub: "Acme Office Supplies", value: "₹1,234.28", foot: "Matched PO-1001" },
  { tag: "NEEDS REVIEW", tone: "warn", ref: "INV-2287", sub: "Globex Logistics", value: "$8,400.00", foot: "Spans two purchase orders" },
  { tag: "APPROVED", tone: "ok", ref: "INV-3310", sub: "Initech Supplies", value: "€2,000.00", foot: "Converted at the pinned rate" },
  { tag: "REJECTED", tone: "bad", ref: "INV-1042", sub: "Acme Office Supplies", value: "₹1,234.28", foot: "Duplicate check" },
  { tag: "APPROVED", tone: "ok", ref: "INV-7701", sub: "Wayne Facilities", value: "$6,500.00", foot: "Within tolerance" },
  { tag: "NEEDS REVIEW", tone: "warn", ref: "INV-5064", sub: "Soylent Foods", value: "₹78,900.00", foot: "Low extraction confidence" },
];

// Row two: the orders those invoices are billed against.
const ROW_ORDERS: SampleCard[] = [
  { tag: "OPEN", tone: "plain", ref: "PO-1001", sub: "Acme Office Supplies", value: "₹1,240.00", foot: "remaining of ₹2,474.28" },
  { tag: "OPEN", tone: "plain", ref: "PO-1006", sub: "Wayne Facilities", value: "$6,500.00", foot: "remaining of $13,000.00" },
  { tag: "FULLY BILLED", tone: "ok", ref: "PO-1008", sub: "Initech Supplies", value: "$0.00", foot: "remaining of $2,160.00" },
  { tag: "OPEN", tone: "plain", ref: "PO-1002", sub: "Globex Logistics", value: "$5,000.00", foot: "remaining of $9,400.00" },
  { tag: "OPEN", tone: "plain", ref: "PO-1004", sub: "Soylent Foods", value: "₹21,100.00", foot: "remaining of ₹100,000.00" },
];

// Row three: the checks themselves, in the words the audit trail uses.
const ROW_CHECKS: SampleCard[] = [
  { tag: "PASSED", tone: "ok", ref: "Duplicate check", sub: "No earlier run matches", value: "9 of 9", foot: "checks completed" },
  { tag: "PASSED", tone: "ok", ref: "Within tolerance", sub: "$12.40 under a $50.00 allowance", value: "PO-1001", foot: "one-sided by design" },
  { tag: "PASSED", tone: "ok", ref: "Vendor approved", sub: "On the approved vendor list", value: "V-001", foot: "matched on a normalised name" },
  { tag: "HELD", tone: "warn", ref: "Confidence gate", sub: "Vendor name scored 0.58", value: "0.65", foot: "threshold to clear" },
  { tag: "PASSED", tone: "ok", ref: "PO match", sub: "Single order referenced", value: "PO-1006", foot: "bound to the ledger" },
  { tag: "PASSED", tone: "ok", ref: "Arithmetic", sub: "Subtotal + tax equals total", value: "±0.01", foot: "rounding allowance" },
];

function Card({ card }: { card: SampleCard }) {
  const tone = TONE[card.tone];
  return (
    <article
      aria-hidden
      className="mr-3.5 w-[248px] shrink-0 rounded-2xl border border-white/[0.08] bg-white/[0.035]
        p-3.5 backdrop-blur-sm sm:w-[268px]"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[12px] tracking-tight text-white/85">{card.ref}</span>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2 py-[3px] text-[10px]
            font-semibold tracking-wide ring-1 ring-inset ${tone.chip}`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} />
          {card.tag}
        </span>
      </div>
      <p className="mt-2 truncate text-[12.5px] text-white/50">{card.sub}</p>
      <p className="mt-2.5 font-mono text-[19px] tracking-tight text-white">{card.value}</p>
      <p className="mt-1 text-[11.5px] text-white/35">{card.foot}</p>
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
          <Card key={`${card.ref}-${i}`} card={card} />
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */

export default function LoginGate() {
  const { signIn, notice } = useAuth();
  const t = useT();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
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

  /* The hero call to action does not sign anybody in -- there is nothing to
     submit yet. On a narrow screen the form is below the fold, so it scrolls
     there and puts the caret in the first field, which is the whole of what
     "open it" can honestly mean before a credential exists. */
  function focusForm() {
    usernameRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    usernameRef.current?.focus({ preventScroll: true });
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
        className="pointer-events-none absolute inset-x-0 top-[8%] flex select-none flex-col gap-4 opacity-[0.55]"
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
          {/* There is no token yet, so the picker falls back to the locale list
              this bundle carries rather than the server's. */}
          <LanguagePicker />
        </header>

        <main className="flex flex-1 items-center justify-center px-5 py-10 sm:px-8">
          <div className="grid w-full max-w-[1140px] items-center gap-12 lg:grid-cols-[1.1fr_minmax(380px,0.9fr)] lg:gap-16">
            {/* ----------------------------------------------------- hero */}
            <section className="text-center lg:text-left">
              <span className="inline-flex items-center gap-2 rounded-full border border-white/12 bg-white/[0.04] px-3 py-1 text-[11.5px] font-medium text-white/60">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                {t("login.eyebrow")}
              </span>

              <h1 className="mt-5 text-[38px] leading-[1.05] font-semibold tracking-[-0.03em] sm:text-[52px] lg:text-[58px]">
                {t("login.hero.title")}
              </h1>

              <p className="mx-auto mt-5 max-w-[520px] text-[15.5px] leading-relaxed text-white/55 lg:mx-0">
                {t("login.hero.subtitle")}
              </p>

              <div className="mt-8 flex flex-col items-center gap-3 sm:flex-row sm:justify-center lg:justify-start">
                <button
                  type="button"
                  onClick={focusForm}
                  className="group inline-flex h-11 items-center justify-center gap-2 rounded-xl bg-white px-5
                    text-[14px] font-semibold text-[#0a0a0c] transition-transform hover:-translate-y-px
                    focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
                    focus-visible:outline-white/60"
                >
                  {t("login.hero.cta")}
                  <span aria-hidden className="transition-transform group-hover:translate-x-0.5">
                    &rarr;
                  </span>
                </button>
              </div>

              {/* Three properties the API can actually be held to, not three
                  adjectives. */}
              <ul className="mt-9 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-[12.5px] text-white/40 lg:justify-start">
                {[t("login.footer.secure"), t("login.footer.roleBased"), t("login.footer.explainable")].map(
                  (item) => (
                    <li key={item} className="flex items-center gap-1.5">
                      <IconCheck size={13} className="text-emerald-400/70" />
                      {item}
                    </li>
                  )
                )}
              </ul>
            </section>

            {/* --------------------------------------------- authentication */}
            <div className="mx-auto w-full max-w-[400px]">
              <div className="rounded-2xl border border-white/[0.09] bg-white/[0.045] p-6 shadow-[0_28px_70px_-24px_rgba(0,0,0,0.9)] backdrop-blur-xl sm:p-7">
                <h2 className="text-[21px] font-semibold tracking-[-0.02em]">{t("login.title")}</h2>
                <p className="mt-1 text-[12.5px] text-white/45">{t("login.scopedNote")}</p>

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

        <footer className="px-5 py-5 sm:px-8">
          <p className="flex items-center justify-center gap-2 text-[11.5px] text-white/30">
            <IconShield size={12} />
            OAuth 2.0 bearer tokens · scopes re-checked on every request · every decision audited
          </p>
        </footer>
      </div>
    </div>
  );
}
