"use client";

/**
 * Application shell: a 236px rail on desktop, a slide-over drawer below lg.
 *
 * Navigation is client-side state rather than routes. The production build is a
 * static export served by FastAPI's StaticFiles mount, so real paths would need
 * the server to resolve deep links — a backend change for no user-visible gain
 * across five sections. Recorded here so it does not read as an oversight.
 */
import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth";
import { useI18n, type MessageKey } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";
import { Button, Tooltip } from "@/components/ui";
import LanguagePicker from "@/components/ui/LanguagePicker";
import Modal from "@/components/ui/Modal";
import {
  IconAnalytics,
  IconInvoice,
  IconLedger,
  IconMenu,
  IconMoon,
  IconOverview,
  IconShield,
  IconSignOut,
  IconSun,
  IconChat,
  IconUpload,
  IconX,
} from "@/components/ui/icons";

export type Section =
  | "overview"
  | "analytics"
  | "assistant"
  | "process"
  | "invoices"
  | "reference";

/**
 * Which sidebar ROW is lit.
 *
 * Five sections but seven nav rows: Invoices and Review queue both open
 * "invoices", Purchase orders and Approved vendors both open "reference". The
 * section alone therefore cannot say which row the user is on, and lighting
 * both rows of a pair — what this did before — reads as a rendering bug rather
 * than as the deliberate ambiguity it was.
 *
 * The id is derived in page.tsx from the navigation options that were already
 * being passed (`exceptionsOnly`, `referenceTab`), so no call site had to
 * learn a new argument.
 */
export type NavId =
  | "overview"
  | "analytics"
  | "assistant"
  | "process"
  | "invoices"
  | "review-queue"
  | "purchase-orders"
  | "approved-vendors";

/** `exceptionsOnly` lets a caller (Overview's exception card, or the "Review
 *  queue" nav item) send the reviewer straight to the pre-filtered queue
 *  instead of the unfiltered register — the same filter InvoicesPage already
 *  applies for the EXCEPTIONS segment, not a second implementation of it.
 *  `referenceTab` does the same for Reference's two nav entries. */
export type Navigate = (
  section: Section,
  opts?: { exceptionsOnly?: boolean; referenceTab?: "orders" | "vendors" }
) => void;

/** The row a section lands on when a caller names no finer destination. */
export function navIdFor(
  section: Section,
  opts?: { exceptionsOnly?: boolean; referenceTab?: "orders" | "vendors" }
): NavId {
  if (section === "invoices") return opts?.exceptionsOnly ? "review-queue" : "invoices";
  if (section === "reference")
    return opts?.referenceTab === "vendors" ? "approved-vendors" : "purchase-orders";
  return section;
}

/**
 * The navigation, as MESSAGE KEYS rather than as words (Phase L).
 *
 * The table is otherwise unchanged: same rows, same order, same scopes, same
 * destinations. `labelKey`/`hintKey` are looked up at render in the reader's
 * own language, so adding a language never means touching this structure --
 * and a row cannot go missing in one language and not another, because the
 * rows are not per-language at all.
 */
const GROUPS: {
  labelKey: MessageKey;
  items: {
    id: NavId;
    key: Section;
    labelKey: MessageKey;
    /** One line under the label on the wide rail — what the section is for,
     *  in the words an AP clerk would use. */
    hintKey: MessageKey;
    icon: (p: { size?: number }) => React.ReactElement;
    scope?: string;
    exceptionsOnly?: boolean;
    referenceTab?: "orders" | "vendors";
    badge?: boolean;
  }[];
}[] = [
  {
    labelKey: "nav.group.operations",
    items: [
      { id: "overview", key: "overview", labelKey: "nav.overview", hintKey: "nav.overview.hint", icon: IconOverview },
      {
        id: "process",
        key: "process",
        labelKey: "nav.process",
        hintKey: "nav.process.hint",
        icon: IconUpload,
        scope: "invoice:process",
      },
      { id: "invoices", key: "invoices", labelKey: "nav.invoices", hintKey: "nav.invoices.hint", icon: IconInvoice },
      {
        id: "review-queue",
        key: "invoices",
        labelKey: "nav.review",
        hintKey: "nav.review.hint",
        icon: IconShield,
        exceptionsOnly: true,
        badge: true,
      },
    ],
  },
  {
    labelKey: "nav.group.reporting",
    items: [
      {
        id: "analytics",
        key: "analytics",
        labelKey: "nav.analytics",
        hintKey: "nav.analytics.hint",
        icon: IconAnalytics,
      },
      {
        id: "assistant",
        key: "assistant",
        labelKey: "nav.assistant",
        hintKey: "nav.assistant.hint",
        icon: IconChat,
      },
    ],
  },
  {
    labelKey: "nav.group.reference",
    items: [
      {
        id: "purchase-orders",
        key: "reference",
        labelKey: "nav.orders",
        hintKey: "nav.orders.hint",
        icon: IconLedger,
        referenceTab: "orders",
      },
      {
        id: "approved-vendors",
        key: "reference",
        labelKey: "nav.vendors",
        hintKey: "nav.vendors.hint",
        icon: IconShield,
        referenceTab: "vendors",
      },
    ],
  },
];

export default function AppShell({
  section,
  activeId,
  onNavigate,
  badge,
  children,
}: {
  section: Section;
  /** Which nav ROW is current. See NavId — the section alone is ambiguous for
   *  the two pairs of rows that share a destination. */
  activeId: NavId;
  onNavigate: Navigate;
  badge?: number;
  children: React.ReactNode;
}) {
  const { user, signOut, can } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const { t } = useI18n();
  const [drawer, setDrawer] = useState(false);
  // Signing out used to happen on the click itself. It is not destructive --
  // nothing is lost that signing back in does not restore -- but it is one
  // misclick away from the theme toggle right beside it, and it drops whatever
  // was half-finished on screen. Confirming costs one keystroke; the accident
  // costs a re-authentication and the page you were reading.
  const [confirmSignOut, setConfirmSignOut] = useState(false);

  useEffect(() => setDrawer(false), [section]);
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawer(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawer]);

  const nav = (
    <nav className="flex flex-col gap-6" aria-label="Sections">
      {GROUPS.map((group) => {
        const items = group.items.filter((i) => !i.scope || can(i.scope));
        if (!items.length) return null;

        return (
          <div key={group.labelKey}>
            <p className="px-2.5 pb-2 text-[10px] font-semibold tracking-[0.08em] text-rail-faint uppercase">
              {t(group.labelKey)}
            </p>
            <div className="flex flex-col gap-0.5">
              {items.map((item) => {
                const active = item.id === activeId;
                const Icon = item.icon;
                return (
                  <button
                    key={item.id}
                    onClick={() =>
                      onNavigate(item.key, {
                        exceptionsOnly: item.exceptionsOnly,
                        referenceTab: item.referenceTab,
                      })
                    }
                    aria-current={active ? "page" : undefined}
                    // The active row is filled AND rule-marked; hover only
                    // tints. Two different treatments, so "where I am" never
                    // has to be told apart from "where the pointer is".
                    className={`group relative flex items-center gap-2.5 rounded-[var(--radius-md)]
                      py-[7px] pr-2 pl-3 text-left transition-colors ${
                        active
                          ? "bg-rail-active text-rail-fg"
                          : "text-rail-muted hover:bg-rail-hover hover:text-rail-fg"
                      }`}
                  >
                    <span
                      aria-hidden
                      className={`absolute top-1/2 left-0 h-[18px] w-[2.5px] -translate-y-1/2
                        rounded-r-full bg-rail-accent transition-opacity ${
                          active ? "opacity-100" : "opacity-0"
                        }`}
                    />
                    <span
                      className={`shrink-0 transition-colors ${
                        active ? "text-rail-accent" : "text-rail-faint group-hover:text-rail-muted"
                      }`}
                    >
                      <Icon size={15} />
                    </span>

                    <span className="min-w-0 flex-1 leading-tight">
                      <span
                        className={`block truncate text-[12.5px] ${active ? "font-semibold" : "font-medium"}`}
                      >
                        {t(item.labelKey)}
                      </span>
                      <span className="block truncate text-[10.5px] text-rail-faint">
                        {t(item.hintKey)}
                      </span>
                    </span>

                    {item.badge && !!badge && (
                      <span
                        title={`${badge} ${t("app.awaitingReview")}`}
                        className="tnum inline-flex h-[18px] min-w-[18px] shrink-0 items-center justify-center
                          rounded-full bg-warn-vivid px-1 text-[10.5px] font-semibold text-white"
                      >
                        {badge}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}
    </nav>
  );

  // Shared by the always-dark rail (desktop aside, mobile drawer) and the
  // theme-reactive mobile top bar, which sits on the light canvas -- the two
  // contexts need opposite text colors, so this takes which one it is in.
  const Brand = ({ dark }: { dark: boolean }) => (
    <div className="flex items-center gap-2.5">
      <span
        className={`grid h-8 w-8 shrink-0 place-items-center rounded-[var(--radius-md)]
          text-[11px] font-bold tracking-[-0.02em] shadow-[var(--shadow-xs)] ${
            dark ? "bg-rail-accent text-rail-accent-fg" : "bg-accent text-accent-fg"
          }`}
      >
        AP
      </span>
      <div className="min-w-0 leading-tight">
        <div
          className={`truncate text-[13px] font-semibold tracking-[-0.015em] ${
            dark ? "text-rail-fg" : "text-fg"
          }`}
        >
          Invoice Processing
        </div>
        <div
          className={`truncate text-[10.5px] tracking-[0.01em] ${
            dark ? "text-rail-faint" : "text-faint"
          }`}
        >
          Accounts payable automation
        </div>
      </div>
    </div>
  );

  const roleOf = () =>
    can("invoice:admin")
      ? "Administrator"
      : can("invoice:review")
        ? "Reviewer"
        : can("invoice:process")
          ? "Analyst"
          : "Read only";

  const account = user && (
    <div className="flex items-center gap-2.5 border-t border-rail-line px-3 py-3">
      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-rail-hover text-[10px] font-semibold text-rail-fg uppercase">
        {user.username.slice(0, 2)}
      </span>
      <div className="min-w-0 flex-1 leading-tight">
        <div className="truncate text-[12px] font-semibold text-rail-fg">{user.username}</div>
        <div className="truncate text-[10.5px] text-rail-faint">{roleOf()}</div>
      </div>
      {/* Offered the list the SERVER said it can answer in, so nobody is shown
          a language the backend would then answer in English. */}
      <LanguagePicker options={user?.languages} compact />
      <Tooltip label={theme === "dark" ? t("app.theme.toLight") : t("app.theme.toDark")}>
        <button
          onClick={toggleTheme}
          aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          className="grid h-7 w-7 shrink-0 place-items-center rounded-[var(--radius-sm)] text-rail-muted transition-colors hover:bg-rail-hover hover:text-rail-fg"
        >
          {theme === "dark" ? <IconSun size={14} /> : <IconMoon size={14} />}
        </button>
      </Tooltip>
      <Tooltip label={t("app.signOut")}>
        <button
          onClick={() => setConfirmSignOut(true)}
          aria-label={t("app.signOut")}
          className="grid h-7 w-7 shrink-0 place-items-center rounded-[var(--radius-sm)] text-rail-muted transition-colors hover:bg-rail-hover hover:text-rail-fg"
        >
          <IconSignOut size={14} />
        </button>
      </Tooltip>
    </div>
  );

  const signOutDialog = (
    <Modal
      open={confirmSignOut}
      onClose={() => setConfirmSignOut(false)}
      size="sm"
      title={t("app.signOut.confirm.title")}
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={() => setConfirmSignOut(false)}>
            {t("app.signOut.confirm.cancel")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => {
              setConfirmSignOut(false);
              signOut();
            }}
          >
            {t("app.signOut.confirm.action")}
          </Button>
        </>
      }
    >
      <p className="t-body text-muted">{t("app.signOut.confirm.body")}</p>
    </Modal>
  );

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[236px_minmax(0,1fr)]">
      {signOutDialog}
      <aside className="sticky top-0 hidden h-screen flex-col border-r border-rail-line bg-rail lg:flex">
        <div className="border-b border-rail-line px-3.5 py-3.5">
          <Brand dark />
        </div>
        <div className="flex-1 overflow-y-auto px-2.5 py-3.5">{nav}</div>
        {account}
      </aside>

      <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-line bg-surface/90 px-3 py-2 backdrop-blur-md lg:hidden">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setDrawer(true)}
          aria-label="Open navigation"
          aria-expanded={drawer}
          icon={<IconMenu size={16} />}
        />
        <Brand dark={false} />
      </header>

      {drawer && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-black/50"
            onClick={() => setDrawer(false)}
            aria-hidden
          />
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Navigation"
            className="slide-in absolute inset-y-0 left-0 flex w-[264px] flex-col border-r border-rail-line bg-rail shadow-[var(--shadow-lg)]"
          >
            <div className="flex items-center justify-between gap-2 border-b border-rail-line px-3.5 py-3.5">
              <Brand dark />
              <button
                onClick={() => setDrawer(false)}
                aria-label="Close navigation"
                className="grid h-6 w-6 shrink-0 place-items-center rounded-[var(--radius-sm)] text-rail-faint transition-colors hover:bg-rail-hover hover:text-rail-fg"
              >
                <IconX size={14} />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-2.5 py-3.5">{nav}</div>
            {account}
          </div>
        </div>
      )}

      <main className="min-w-0">{children}</main>
    </div>
  );
}

/**
 * Page chrome.
 *
 * The title bar is sticky and sits directly on the canvas rather than inside a
 * panel — one less nested box, and the eye keeps a fixed anchor while long
 * tables scroll.
 *
 * It carries its own horizontal padding rather than negative margins: it renders
 * straight into <main>, which has no padding of its own, so pulling outwards
 * would make the page wider than the viewport and add a horizontal scrollbar on
 * small screens.
 */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="sticky top-0 z-20 mb-5 border-b border-line bg-canvas/85 px-4 py-3.5 backdrop-blur-md sm:px-7">
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h1 className="t-page">{title}</h1>
          {description && <p className="t-meta mt-0.5">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}

/**
 * Consistent page width and vertical rhythm for every section.
 *
 * The gap is the product's one vertical spacing decision: every page stacks
 * panels at the same interval, which is most of what makes four separately
 * built screens read as one application.
 */
export function PageBody({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-4 pb-10 sm:px-7">
      <div className="mx-auto flex max-w-[1400px] flex-col gap-5">{children}</div>
    </div>
  );
}
