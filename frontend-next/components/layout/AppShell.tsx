"use client";

/**
 * Application shell: a 216px rail on desktop, a slide-over drawer below lg.
 *
 * Navigation is client-side state rather than routes. The production build is a
 * static export served by FastAPI's StaticFiles mount, so real paths would need
 * the server to resolve deep links — a backend change for no user-visible gain
 * across four sections. Recorded here so it does not read as an oversight.
 */
import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth";
import { Badge, Button, Tooltip } from "@/components/ui";
import {
  IconInvoice,
  IconLedger,
  IconMenu,
  IconOverview,
  IconSignOut,
  IconUpload,
  IconX,
} from "@/components/ui/icons";

export type Section = "overview" | "process" | "invoices" | "reference";

/** `exceptionsOnly` lets a caller (Overview's exception card) send the reviewer
 *  straight to the pre-filtered queue instead of the unfiltered register. */
export type Navigate = (section: Section, opts?: { exceptionsOnly?: boolean }) => void;

const GROUPS: {
  label: string;
  items: {
    key: Section;
    label: string;
    icon: (p: { size?: number }) => React.ReactElement;
    scope?: string;
  }[];
}[] = [
  {
    label: "Monitor",
    items: [
      { key: "overview", label: "Overview", icon: IconOverview },
      { key: "invoices", label: "Invoices", icon: IconInvoice },
    ],
  },
  {
    label: "Operate",
    items: [
      { key: "process", label: "Process invoice", icon: IconUpload, scope: "invoice:process" },
      { key: "reference", label: "Purchase orders", icon: IconLedger },
    ],
  },
];

export default function AppShell({
  section,
  onNavigate,
  badge,
  children,
}: {
  section: Section;
  onNavigate: Navigate;
  badge?: number;
  children: React.ReactNode;
}) {
  const { user, signOut, can } = useAuth();
  const [drawer, setDrawer] = useState(false);

  useEffect(() => setDrawer(false), [section]);
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawer(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawer]);

  const nav = (
    <nav className="flex flex-col gap-5" aria-label="Sections">
      {GROUPS.map((group) => {
        const items = group.items.filter((i) => !i.scope || can(i.scope));
        if (!items.length) return null;

        return (
          <div key={group.label}>
            <p className="t-caption px-2 pb-1.5">{group.label}</p>
            <div className="flex flex-col gap-px">
              {items.map((item) => {
                const active = item.key === section;
                const Icon = item.icon;
                return (
                  <button
                    key={item.key}
                    onClick={() => onNavigate(item.key)}
                    aria-current={active ? "page" : undefined}
                    // Active outranks hover on purpose: hover only lifts the
                    // text, so the filled row always means "you are here".
                    className={`group relative flex items-center gap-2.5 rounded-[var(--radius-md)]
                      py-1.5 pr-2 pl-2.5 text-[12.5px] transition-colors ${
                        active ? "bg-hover font-medium text-fg" : "text-muted hover:text-fg"
                      }`}
                  >
                    {/* A 2px rule marks the active item instead of a filled
                        block — legible at a glance, quiet at rest. */}
                    <span
                      aria-hidden
                      className={`absolute top-1/2 left-0 h-4 w-[2.5px] -translate-y-1/2 rounded-full transition-opacity ${
                        active ? "bg-accent opacity-100" : "opacity-0"
                      }`}
                    />
                    <span className={active ? "text-accent" : "text-faint group-hover:text-muted"}>
                      <Icon size={15} />
                    </span>
                    <span className="flex-1 text-left">{item.label}</span>
                    {item.key === "invoices" && !!badge && (
                      <Badge tone="warn" title={`${badge} awaiting review`}>
                        {badge}
                      </Badge>
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

  const brand = (
    <div className="flex items-center gap-2.5">
      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-[var(--radius-md)] bg-accent text-[11px] font-bold text-accent-fg">
        AP
      </span>
      <div className="min-w-0 leading-tight">
        <div className="truncate text-[12.5px] font-semibold tracking-[-0.01em]">
          Invoice Processing
        </div>
        <div className="t-meta truncate text-[11px]">Accounts payable</div>
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
    <div className="flex items-center gap-2 border-t border-line px-3 py-2.5">
      <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-sunken text-[10px] font-semibold text-secondary uppercase">
        {user.username.slice(0, 2)}
      </span>
      <div className="min-w-0 flex-1 leading-tight">
        <div className="truncate text-[12px] font-medium">{user.username}</div>
        <div className="t-meta truncate text-[10.5px]">{roleOf()}</div>
      </div>
      <Tooltip label="Sign out">
        <Button
          variant="ghost"
          size="xs"
          onClick={() => signOut()}
          aria-label="Sign out"
          icon={<IconSignOut size={13} />}
        />
      </Tooltip>
    </div>
  );

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[216px_minmax(0,1fr)]">
      <aside className="sticky top-0 hidden h-screen flex-col border-r border-line bg-surface lg:flex">
        <div className="px-3 py-3.5">{brand}</div>
        <div className="flex-1 overflow-y-auto px-2 py-2">{nav}</div>
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
        {brand}
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
            className="slide-in absolute inset-y-0 left-0 flex w-60 flex-col border-r border-line bg-surface shadow-[var(--shadow-lg)]"
          >
            <div className="flex items-center justify-between gap-2 px-3 py-3.5">
              {brand}
              <Button
                variant="ghost"
                size="xs"
                onClick={() => setDrawer(false)}
                aria-label="Close navigation"
                icon={<IconX size={14} />}
              />
            </div>
            <div className="flex-1 overflow-y-auto px-2 py-2">{nav}</div>
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
    <div className="sticky top-0 z-20 mb-4 border-b border-line bg-canvas/85 px-4 py-3 backdrop-blur-md sm:px-6">
      <div className="mx-auto flex max-w-[1320px] flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="t-page">{title}</h1>
          {description && <p className="t-meta mt-0.5">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}

/** Consistent page width and vertical rhythm for every section. */
export function PageBody({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-4 pb-8 sm:px-6">
      <div className="mx-auto flex max-w-[1320px] flex-col gap-4">{children}</div>
    </div>
  );
}
