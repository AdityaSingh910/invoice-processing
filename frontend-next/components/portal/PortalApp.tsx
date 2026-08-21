"use client";

/**
 * The client portal (Phase J) — the whole external experience.
 *
 * A DIFFERENT SHELL, NOT THE INTERNAL APP WITH ROWS HIDDEN.
 *
 * `app/page.tsx` sends an account here when its token carries `portal:read`
 * and no `invoice:read`, and that branch is deliberately total: a client never
 * mounts AppShell, so there is no internal navigation to hide, no section that
 * could be reached by a stale piece of state, and no place a future internal
 * feature could appear on a supplier's screen by being added to a shared nav
 * array. Two audiences, two shells.
 *
 * NOTHING HERE IS A SECURITY CONTROL. Every figure and every sentence on these
 * screens was chosen by the server, which filters in SQL against the
 * authenticated principal before a row is read. This file does no filtering,
 * because it is never sent another client's data to filter — which is the only
 * arrangement where a bug in the UI cannot become a disclosure.
 */
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { apiJson } from "@/lib/api";
import { Button, Callout, ErrorState, Spinner, Tooltip } from "@/components/ui";
import {
  IconBuilding,
  IconInvoice,
  IconLedger,
  IconMoon,
  IconSignOut,
  IconSun,
  IconUpload,
} from "@/components/ui/icons";
import type { PortalIdentity } from "@/lib/types";
import PortalInvoices from "./PortalInvoices";
import PortalOrders from "./PortalOrders";
import PortalSubmit from "./PortalSubmit";

type PortalSection = "invoices" | "orders" | "submit";

const SECTIONS: {
  id: PortalSection;
  label: string;
  hint: string;
  icon: (p: { size?: number }) => React.ReactElement;
  /** Rendered only when the account's token carries this scope. Courtesy, not
   *  enforcement — the endpoint re-checks it and a forged click gets a 403. */
  scope?: string;
}[] = [
  { id: "invoices", label: "My invoices", hint: "Status and history", icon: IconInvoice },
  { id: "orders", label: "Purchase orders", hint: "What is left to bill", icon: IconLedger },
  {
    id: "submit",
    label: "Send an invoice",
    hint: "Upload a PDF",
    icon: IconUpload,
    scope: "portal:submit",
  },
];

export default function PortalApp() {
  const { user, signOut, can } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const [section, setSection] = useState<PortalSection>("invoices");
  const [identity, setIdentity] = useState<PortalIdentity | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped when a submission lands, so the invoice list picks it up without
  // the supplier wondering whether it arrived.
  const [reloadKey, setReloadKey] = useState(0);

  const load = useCallback(() => {
    setError(null);
    apiJson<PortalIdentity>("/api/portal/me")
      .then(setIdentity)
      .catch(() => setError("We could not load your account. Please try again."));
  }, []);

  useEffect(load, [load]);

  const items = SECTIONS.filter((s) => !s.scope || can(s.scope));

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[240px_minmax(0,1fr)]">
      <aside className="sticky top-0 flex h-auto flex-col border-b border-rail-line bg-rail lg:h-screen lg:border-r lg:border-b-0">
        <div className="border-b border-rail-line px-3.5 py-3.5">
          <div className="flex items-center gap-2.5">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[var(--radius-md)] bg-rail-accent text-rail-accent-fg shadow-[var(--shadow-xs)]">
              <IconBuilding size={15} />
            </span>
            <div className="min-w-0 leading-tight">
              <div className="truncate text-[13px] font-semibold tracking-[-0.015em] text-rail-fg">
                Supplier portal
              </div>
              {/* The supplier's OWN name, not ours. This screen belongs to
                  them, and naming the buyer here would make it read as our
                  system that they are visiting. */}
              <div className="truncate text-[10.5px] tracking-[0.01em] text-rail-faint">
                {identity?.client_name ?? " "}
              </div>
            </div>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto px-2.5 py-3.5" aria-label="Portal sections">
          <div className="flex flex-col gap-0.5">
            {items.map((item) => {
              const active = item.id === section;
              const Icon = item.icon;
              return (
                <button
                  key={item.id}
                  onClick={() => setSection(item.id)}
                  aria-current={active ? "page" : undefined}
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
                      {item.label}
                    </span>
                    <span className="block truncate text-[10.5px] text-rail-faint">
                      {item.hint}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </nav>

        {user && (
          <div className="flex items-center gap-2.5 border-t border-rail-line px-3 py-3">
            <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-rail-hover text-[10px] font-semibold text-rail-fg uppercase">
              {user.username.slice(0, 2)}
            </span>
            <div className="min-w-0 flex-1 leading-tight">
              <div className="truncate text-[12px] font-semibold text-rail-fg">
                {user.username}
              </div>
              <div className="truncate text-[10.5px] text-rail-faint">
                {can("portal:submit") ? "Supplier" : "Supplier (view only)"}
              </div>
            </div>
            <Tooltip label={theme === "dark" ? "Switch to light" : "Switch to dark"}>
              <button
                onClick={toggleTheme}
                aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
                className="grid h-7 w-7 shrink-0 place-items-center rounded-[var(--radius-sm)] text-rail-muted transition-colors hover:bg-rail-hover hover:text-rail-fg"
              >
                {theme === "dark" ? <IconSun size={14} /> : <IconMoon size={14} />}
              </button>
            </Tooltip>
            <Tooltip label="Sign out">
              <button
                onClick={() => signOut()}
                aria-label="Sign out"
                className="grid h-7 w-7 shrink-0 place-items-center rounded-[var(--radius-sm)] text-rail-muted transition-colors hover:bg-rail-hover hover:text-rail-fg"
              >
                <IconSignOut size={14} />
              </button>
            </Tooltip>
          </div>
        )}
      </aside>

      <main className="min-w-0">
        {error ? (
          <ErrorState description={error} onRetry={load} />
        ) : !identity ? (
          <div className="grid min-h-[60vh] place-items-center">
            <span className="flex items-center gap-2.5 text-[13px] text-muted">
              <Spinner />
              Loading
            </span>
          </div>
        ) : (
          <>
            {/* A broken supplier link is stated in plain language rather than
                presenting as missing invoices — the difference between a
                supplier who rings up and one who quietly assumes we lost
                their paperwork. */}
            {identity.notices.length > 0 && (
              <div className="px-4 pt-4 sm:px-7">
                <div className="mx-auto max-w-[1400px]">
                  <Callout tone="warn" title="There is a problem with this account">
                    {identity.notices.map((n, i) => (
                      <p key={i} className={i ? "mt-1" : ""}>
                        {n}
                      </p>
                    ))}
                  </Callout>
                </div>
              </div>
            )}

            {section === "invoices" && (
              <PortalInvoices identity={identity} reloadKey={reloadKey} />
            )}
            {section === "orders" && <PortalOrders identity={identity} />}
            {section === "submit" && (
              <PortalSubmit
                onSubmitted={() => {
                  setReloadKey((k) => k + 1);
                  setSection("invoices");
                }}
              />
            )}
          </>
        )}
      </main>
    </div>
  );
}

/** Shared page chrome, so the three portal screens read as one product rather
 *  than three. Kept separate from AppShell's PageHeader because the internal
 *  one takes actions this surface has no use for, and because the two are
 *  free to diverge. */
export function PortalPage({
  title,
  description,
  children,
}: {
  title: string;
  description?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <>
      <div className="sticky top-0 z-20 mb-5 border-b border-line bg-canvas/85 px-4 py-3.5 backdrop-blur-md sm:px-7">
        <div className="mx-auto max-w-[1400px]">
          <h1 className="t-page">{title}</h1>
          {description && <p className="t-meta mt-0.5">{description}</p>}
        </div>
      </div>
      <div className="px-4 pb-10 sm:px-7">
        <div className="mx-auto flex max-w-[1400px] flex-col gap-5">{children}</div>
      </div>
    </>
  );
}

/** One place decides how a client state is coloured, so the list and the
 *  detail panel cannot show the same invoice two different ways. */
export function stateTone(state: string): "ok" | "warn" | "bad" | "neutral" {
  if (state === "APPROVED") return "ok";
  if (state === "IN_REVIEW") return "warn";
  if (state === "DECLINED") return "bad";
  return "neutral";
}

export const STATE_WORD: Record<string, string> = {
  RECEIVED: "Received",
  IN_REVIEW: "Being checked",
  APPROVED: "Approved",
  DECLINED: "Declined",
};

export { type PortalSection };
