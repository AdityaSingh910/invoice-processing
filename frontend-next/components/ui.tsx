"use client";

/** Small shared primitives. Presentation only. */
import type { ReactNode } from "react";
import { humanise } from "@/lib/format";

export function Card({
  title, aside, children, className = "",
}: { title?: string; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section
      className={`rounded-[var(--radius-card)] border border-border bg-panel shadow-[var(--shadow-card)] ${className}`}
    >
      {(title || aside) && (
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
          {title && <h2 className="text-[15px] font-semibold">{title}</h2>}
          {aside}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Chip({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="rounded-full border border-border bg-panel2 px-2 py-0.5 text-[11px] font-medium text-dim"
    >
      {children}
    </span>
  );
}

/** Verdict colouring is driven by the server-produced value via data-status. */
export function StatusPill({ status }: { status?: string | null }) {
  if (!status) return null;
  return (
    <span className="pill" data-status={status}>
      {humanise(status)}
    </span>
  );
}

export function EmptyState({ title, sub }: { title: string; sub?: ReactNode }) {
  return (
    <div className="grid place-items-center gap-1 px-4 py-10 text-center">
      <div className="text-2xl text-faint">◌</div>
      <p className="font-semibold">{title}</p>
      {sub && <p className="max-w-md text-dim">{sub}</p>}
    </div>
  );
}

export function KvTable({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <table className="w-full border-collapse">
      <tbody>
        {rows.map(([k, v], i) => (
          <tr key={i} className="border-b border-border last:border-0">
            <td className="py-1.5 pr-3 align-top whitespace-nowrap text-dim">{k}</td>
            <td className="py-1.5 text-right align-top break-words">{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export const Missing = () => <span className="italic text-fail">missing</span>;
