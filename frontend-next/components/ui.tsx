"use client";

/** Small shared primitives. Presentation only. */
import type { ReactNode } from "react";
import { humanise } from "@/lib/format";

export function Card({
  title,
  aside,
  children,
  className = "",
  bodyClass = "",
}: {
  title?: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClass?: string;
}) {
  return (
    <section className={`card overflow-hidden ${className}`}>
      {(title || aside) && (
        <header className="flex flex-wrap items-center justify-between gap-3 px-5 pt-4 pb-3">
          {title && <h2 className="text-[15px] font-semibold tracking-[-0.01em]">{title}</h2>}
          {aside}
        </header>
      )}
      <div className={`px-5 pb-5 ${title || aside ? "" : "pt-5"} ${bodyClass}`}>{children}</div>
    </section>
  );
}

export function Chip({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="rounded-full border border-border bg-panel2 px-2.5 py-1 text-[11px] font-medium text-dim"
    >
      {children}
    </span>
  );
}

const STATUS_GLYPH: Record<string, string> = {
  APPROVED: "✓",
  HUMAN_APPROVED: "✓",
  NEEDS_REVIEW: "!",
  REJECTED: "✕",
  HUMAN_REJECTED: "✕",
};

/** Verdict colouring is driven by the server-produced value via data-status. */
export function StatusPill({ status, glyph = true }: { status?: string | null; glyph?: boolean }) {
  if (!status) return null;
  const g = STATUS_GLYPH[status];
  return (
    <span className="pill" data-status={status}>
      {glyph && g && <span aria-hidden>{g}</span>}
      {humanise(status)}
    </span>
  );
}

export function EmptyState({ title, sub }: { title: string; sub?: ReactNode }) {
  return (
    <div className="grid place-items-center gap-2 px-4 py-14 text-center">
      <div className="grid h-12 w-12 place-items-center rounded-full border border-border bg-panel2 text-lg text-faint">
        ◌
      </div>
      <p className="font-semibold">{title}</p>
      {sub && <p className="max-w-md text-[14px] text-dim">{sub}</p>}
    </div>
  );
}

export function KvTable({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <table className="w-full border-collapse">
      <tbody>
        {rows.map(([k, v], i) => (
          <tr key={i} className="border-b border-border/70 last:border-0">
            <td className="py-2 pr-3 align-top text-[14px] whitespace-nowrap text-dim">{k}</td>
            <td className="py-2 text-right align-top text-[14px] break-words">{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export const Missing = () => (
  <span className="rounded-md px-1.5 py-0.5 text-[12px] font-semibold" style={{ background: "var(--fail-soft)", color: "var(--fail)" }}>
    missing
  </span>
);

export const Eyebrow = ({ children }: { children: ReactNode }) => (
  <div className="eyebrow mb-2">{children}</div>
);
