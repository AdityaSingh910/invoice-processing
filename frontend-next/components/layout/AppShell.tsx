"use client";

/**
 * Application shell: fixed sidebar on desktop, slide-over drawer on mobile.
 *
 * Navigation is client-side state rather than routes. The production build is a
 * static export served by FastAPI's StaticFiles mount, so real paths would need
 * the server to resolve deep links to the right directory — a backend change,
 * for no user-visible gain in a four-section tool. The trade is documented here
 * so the next person does not assume it was an oversight.
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

const NAV: {
  key: Section;
  label: string;
  icon: (p: { size?: number }) => React.ReactElement;
  scope?: string;
}[] = [
  { key: "overview", label: "Overview", icon: IconOverview },
  { key: "process", label: "Process invoice", icon: IconUpload, scope: "invoice:process" },
  { key: "invoices", label: "Invoices", icon: IconInvoice },
  { key: "reference", label: "Purchase orders", icon: IconLedger },
];

export default function AppShell({
  section,
  onNavigate,
  badge,
  children,
}: {
  section: Section;
  onNavigate: (s: Section) => void;
  /** Count of open exceptions, surfaced next to Invoices. */
  badge?: number;
  children: React.ReactNode;
}) {
  const { user, signOut, can } = useAuth();
  const [drawer, setDrawer] = useState(false);

  // Close the drawer on navigation and on Escape.
  useEffect(() => setDrawer(false), [section]);
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawer(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawer]);

  const items = NAV.filter((n) => !n.scope || can(n.scope));

  const nav = (
    <nav className="flex flex-col gap-0.5" aria-label="Sections">
      {items.map((item) => {
        const active = item.key === section;
        const Icon = item.icon;
        return (
          <button
            key={item.key}
            onClick={() => onNavigate(item.key)}
            aria-current={active ? "page" : undefined}
            className={`group flex items-center gap-2.5 rounded-[var(--radius-md)] px-2.5 py-2
              text-[13px] font-medium transition-colors ${
                active
                  ? "bg-surface2 text-fg"
                  : "text-muted hover:bg-surface2/70 hover:text-fg"
              }`}
          >
            <span className={active ? "text-accent" : "text-subtle group-hover:text-muted"}>
              <Icon size={16} />
            </span>
            <span className="flex-1 text-left">{item.label}</span>
            {item.key === "invoices" && !!badge && (
              <Badge tone="warning" title={`${badge} awaiting review`}>
                {badge}
              </Badge>
            )}
          </button>
        );
      })}
    </nav>
  );

  const brand = (
    <div className="flex items-center gap-2.5">
      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[var(--radius-md)] bg-accent text-[12px] font-bold text-accent-fg">
        AP
      </span>
      <div className="min-w-0 leading-tight">
        <div className="truncate text-[13px] font-semibold tracking-[-0.01em]">
          Invoice Processing
        </div>
        <div className="truncate text-[11px] text-subtle">Accounts payable</div>
      </div>
    </div>
  );

  const account = user && (
    <div className="flex items-center gap-2 border-t border-border px-3 py-3">
      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full border border-border bg-surface2 text-[11px] font-semibold uppercase">
        {user.username.slice(0, 2)}
      </span>
      <div className="min-w-0 flex-1 leading-tight">
        <div className="truncate text-[13px] font-medium">{user.username}</div>
        <div className="truncate text-[11px] text-subtle">
          {can("invoice:admin")
            ? "Administrator"
            : can("invoice:review")
              ? "Reviewer"
              : can("invoice:process")
                ? "Analyst"
                : "Read only"}
        </div>
      </div>
      <Tooltip label="Sign out">
        <Button
          variant="ghost"
          size="sm"
          className="px-2"
          onClick={() => signOut()}
          aria-label="Sign out"
          icon={<IconSignOut size={15} />}
        />
      </Tooltip>
    </div>
  );

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[240px_minmax(0,1fr)]">
      {/* ---------------------------------------------------- desktop rail */}
      <aside className="sticky top-0 hidden h-screen flex-col border-r border-border bg-surface lg:flex">
        <div className="px-3 py-4">{brand}</div>
        <div className="flex-1 overflow-y-auto px-3">{nav}</div>
        {account}
      </aside>

      {/* ------------------------------------------------------ mobile bar */}
      <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-border bg-surface/90 px-3 py-2.5 backdrop-blur-md lg:hidden">
        <Button
          variant="ghost"
          size="sm"
          className="px-2"
          onClick={() => setDrawer(true)}
          aria-label="Open navigation"
          aria-expanded={drawer}
          icon={<IconMenu size={18} />}
        />
        {brand}
      </header>

      {drawer && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={() => setDrawer(false)}
            aria-hidden
          />
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Navigation"
            className="rise absolute inset-y-0 left-0 flex w-64 flex-col border-r border-border bg-surface shadow-[var(--shadow-lg)]"
          >
            <div className="flex items-center justify-between gap-2 px-3 py-4">
              {brand}
              <Button
                variant="ghost"
                size="sm"
                className="px-2"
                onClick={() => setDrawer(false)}
                aria-label="Close navigation"
                icon={<IconX size={16} />}
              />
            </div>
            <div className="flex-1 overflow-y-auto px-3">{nav}</div>
            {account}
          </div>
        </div>
      )}

      <main className="min-w-0">{children}</main>
    </div>
  );
}

/** Page header. Every section uses it, so titles sit on the same baseline and
 *  actions land in the same place on every screen. */
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
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4">
      <div className="min-w-0">
        <h1 className="text-[19px] leading-tight font-semibold tracking-[-0.02em]">{title}</h1>
        {description && <p className="mt-1 text-[13px] text-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Consistent page padding and max width for every section. */
export function PageBody({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-auto w-full max-w-[1400px] px-4 py-5 sm:px-6 sm:py-6">
      <div className="flex flex-col gap-5">{children}</div>
    </div>
  );
}
