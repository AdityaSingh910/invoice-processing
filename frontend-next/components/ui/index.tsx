"use client";

/**
 * Primitive layer.
 *
 * Everything the app renders is built from these, so spacing, radii, weight and
 * focus behaviour are decided once here rather than re-improvised per screen.
 * If a screen needs a variant, it belongs in this file — not as a one-off set
 * of utility classes at the call site.
 */
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";
import { IconAlert, IconEmpty, IconSearch } from "./icons";

/* ------------------------------------------------------------------ button */

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const VARIANT: Record<Variant, string> = {
  primary:
    "bg-accent text-accent-fg border border-transparent hover:bg-accent-hover shadow-[var(--shadow-xs)]",
  secondary:
    "bg-surface text-fg border border-border hover:bg-surface2 hover:border-border-strong shadow-[var(--shadow-xs)]",
  ghost: "bg-transparent text-muted border border-transparent hover:bg-surface2 hover:text-fg",
  danger:
    "bg-surface text-[var(--danger)] border border-[var(--danger-line)] hover:bg-[var(--danger-weak)]",
};

const SIZE: Record<Size, string> = {
  sm: "h-8 px-3 text-[13px] gap-1.5 rounded-[var(--radius-sm)]",
  md: "h-9 px-3.5 text-[14px] gap-2 rounded-[var(--radius-md)]",
};

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  icon,
  children,
  className = "",
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  icon?: ReactNode;
}) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`inline-flex shrink-0 items-center justify-center font-medium whitespace-nowrap
        transition-colors duration-100 disabled:pointer-events-none disabled:opacity-45
        ${VARIANT[variant]} ${SIZE[size]} ${className}`}
    >
      {loading ? <Spinner /> : icon}
      {children}
    </button>
  );
}

export function Spinner({ size = 14 }: { size?: number }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      className="inline-block shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent"
      style={{ width: size, height: size }}
    />
  );
}

/* ------------------------------------------------------------------- badge */

type Tone = "neutral" | "success" | "warning" | "danger" | "accent";

const TONE: Record<Tone, string> = {
  neutral: "bg-surface2 text-muted border-border",
  success: "bg-[var(--success-weak)] text-[var(--success)] border-[var(--success-line)]",
  warning: "bg-[var(--warning-weak)] text-[var(--warning)] border-[var(--warning-line)]",
  danger: "bg-[var(--danger-weak)] text-[var(--danger)] border-[var(--danger-line)]",
  accent: "bg-accent-weak text-accent border-accent-line",
};

export function Badge({
  tone = "neutral",
  children,
  icon,
  className = "",
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  icon?: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-[var(--radius-xs)] border px-1.5 py-0.5
        text-[11px] font-semibold whitespace-nowrap ${TONE[tone]} ${className}`}
    >
      {icon}
      {children}
    </span>
  );
}

/** Maps a server-produced verdict onto a tone. The single place that decision
 *  is made, so a status can never be coloured two different ways. */
export const toneFor = (status?: string | null): Tone => {
  switch (status) {
    case "APPROVED":
    case "HUMAN_APPROVED":
    case "open":
      return "success";
    case "NEEDS_REVIEW":
      return "warning";
    case "REJECTED":
    case "HUMAN_REJECTED":
    case "closed":
      return "danger";
    default:
      return "neutral";
  }
};

export function StatusBadge({ status, className }: { status?: string | null; className?: string }) {
  if (!status) return null;
  return (
    <Badge tone={toneFor(status)} className={className}>
      {String(status).replace(/_/g, " ").toLowerCase()}
    </Badge>
  );
}

/* -------------------------------------------------------------------- card */

export function Card({
  children,
  className = "",
  padded = true,
}: {
  children: ReactNode;
  className?: string;
  padded?: boolean;
}) {
  return (
    <section
      className={`rounded-[var(--radius-lg)] border border-border bg-surface shadow-[var(--shadow-xs)]
        ${padded ? "p-4 sm:p-5" : ""} ${className}`}
    >
      {children}
    </section>
  );
}

export function CardHeader({
  title,
  description,
  actions,
  className = "",
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`flex flex-wrap items-start justify-between gap-3 ${className}`}>
      <div className="min-w-0">
        <h2 className="text-[15px] leading-tight font-semibold tracking-[-0.01em]">{title}</h2>
        {description && <p className="mt-0.5 text-[13px] text-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ inputs */

// No width here on purpose: `w-full` in the base fought every caller that
// wanted an intrinsic width, and which one won depended on Tailwind's output
// order rather than on the call site.
const FIELD_BASE =
  "rounded-[var(--radius-md)] border border-border bg-surface text-[14px] text-fg " +
  "transition-colors placeholder:text-subtle hover:border-border-strong " +
  "focus:border-accent focus:outline-none focus-visible:outline-none " +
  "disabled:cursor-not-allowed disabled:opacity-50";

export function Input({
  className = "",
  ...rest
}: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...rest} className={`${FIELD_BASE} h-9 w-full px-3 ${className}`} />;
}

export function SearchInput({
  className = "",
  ...rest
}: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className={`relative ${className}`}>
      <IconSearch
        className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-subtle"
        size={15}
      />
      <input {...rest} type="search" className={`${FIELD_BASE} h-9 w-full pr-3 pl-8`} />
    </div>
  );
}

export function Select({
  className = "",
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select {...rest} className={`${FIELD_BASE} h-9 cursor-pointer pr-8 pl-3 ${className}`}>
      {children}
    </select>
  );
}

export function Field({
  label,
  hint,
  htmlFor,
  children,
}: {
  label: string;
  hint?: ReactNode;
  htmlFor?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <label htmlFor={htmlFor} className="mb-1.5 block text-[13px] font-medium">
        {label}
      </label>
      {children}
      {hint && <p className="mt-1 text-[12px] text-subtle">{hint}</p>}
    </div>
  );
}

/* ------------------------------------------------------------- segmented */

export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: { value: T; label: string; count?: number }[];
  value: T;
  onChange: (v: T) => void;
  ariaLabel: string;
}) {
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className="inline-flex items-center gap-0.5 rounded-[var(--radius-md)] border border-border bg-surface2 p-0.5"
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(o.value)}
            className={`inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] px-2.5 py-1
              text-[13px] font-medium transition-colors ${
                active
                  ? "bg-surface text-fg shadow-[var(--shadow-xs)]"
                  : "text-muted hover:text-fg"
              }`}
          >
            {o.label}
            {o.count !== undefined && (
              <span className={`num text-[11px] ${active ? "text-subtle" : "text-subtle"}`}>
                {o.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/* ----------------------------------------------------------------- states */

export function EmptyState({
  title,
  description,
  action,
  icon,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-14 text-center">
      <div className="grid h-10 w-10 place-items-center rounded-[var(--radius-md)] border border-border bg-surface2 text-subtle">
        {icon ?? <IconEmpty size={18} />}
      </div>
      <div>
        <p className="text-[14px] font-semibold">{title}</p>
        {description && (
          <p className="mx-auto mt-1 max-w-sm text-[13px] text-muted">{description}</p>
        )}
      </div>
      {action}
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  description,
  onRetry,
}: {
  title?: string;
  description?: ReactNode;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-3 px-6 py-12 text-center"
    >
      <div className="grid h-10 w-10 place-items-center rounded-[var(--radius-md)] border border-[var(--danger-line)] bg-[var(--danger-weak)] text-[var(--danger)]">
        <IconAlert size={18} />
      </div>
      <div>
        <p className="text-[14px] font-semibold">{title}</p>
        {description && (
          <p className="mx-auto mt-1 max-w-sm text-[13px] text-muted">{description}</p>
        )}
      </div>
      {onRetry && (
        <Button size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

/** Inline message. `tone` carries the meaning; the icon is decorative. */
export function Callout({
  tone = "neutral",
  title,
  children,
  icon,
}: {
  tone?: Tone;
  title?: ReactNode;
  children?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className={`rounded-[var(--radius-md)] border px-3 py-2.5 text-[13px] ${TONE[tone]}`}>
      <div className="flex items-start gap-2">
        {icon && <span className="mt-px shrink-0">{icon}</span>}
        <div className="min-w-0">
          {title && <p className="font-semibold">{title}</p>}
          {children && <div className={title ? "mt-0.5 opacity-90" : ""}>{children}</div>}
        </div>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- skeleton */

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden />;
}

export function SkeletonRows({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="divide-y divide-border" aria-busy="true" aria-label="Loading rows">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex items-center gap-4 px-1 py-3">
          {Array.from({ length: cols }).map((_, c) => (
            <Skeleton
              key={c}
              className="h-3.5"
              // Varying widths read as content rather than as a loading bar.
              {...{ style: { width: `${[28, 18, 14, 10, 12][c % 5]}%` } }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- tooltip */

/** CSS-only tooltip. Appears on hover AND keyboard focus, which a title
 *  attribute does not do. */
export function Tooltip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="group/tt relative inline-flex">
      {children}
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-50 mb-1.5 -translate-x-1/2
          scale-95 rounded-[var(--radius-sm)] border border-border bg-surface px-2 py-1 text-[12px]
          whitespace-nowrap text-fg opacity-0 shadow-[var(--shadow-md)] transition
          group-hover/tt:scale-100 group-hover/tt:opacity-100
          group-focus-within/tt:scale-100 group-focus-within/tt:opacity-100"
      >
        {label}
      </span>
    </span>
  );
}

/* ------------------------------------------------------------------ table */

export function TH({
  children,
  align = "left",
  className = "",
  ...rest
}: React.ThHTMLAttributes<HTMLTableCellElement> & { align?: "left" | "right" }) {
  return (
    <th
      scope="col"
      {...rest}
      className={`label sticky top-0 z-10 bg-surface2 px-3 py-2 font-semibold ${
        align === "right" ? "text-right" : "text-left"
      } ${className}`}
    >
      {children}
    </th>
  );
}

export function TD({
  children,
  align = "left",
  className = "",
  ...rest
}: React.TdHTMLAttributes<HTMLTableCellElement> & { align?: "left" | "right" }) {
  return (
    <td
      {...rest}
      className={`px-3 py-2.5 align-middle ${align === "right" ? "text-right" : ""} ${className}`}
    >
      {children}
    </td>
  );
}

/** Key/value list used across the detail panels. */
export function DescriptionList({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="divide-y divide-border">
      {rows.map(([k, v], i) => (
        <div key={i} className="flex items-baseline justify-between gap-4 py-2">
          <dt className="shrink-0 text-[13px] text-muted">{k}</dt>
          <dd className="min-w-0 text-right text-[13px] font-medium break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
