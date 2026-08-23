"use client";

/**
 * Primitive layer.
 *
 * Everything the app renders is built from these, so size, weight, radius and
 * focus behaviour are decided once rather than re-improvised per screen. A
 * screen needing a new variant should add it here, not compose one-off utility
 * strings at the call site.
 */
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { IconAlert, IconChevronDown, IconEmpty, IconSearch } from "./icons";

/* ------------------------------------------------------------------ button */

type Variant = "primary" | "secondary" | "ghost" | "danger" | "subtle";
type Size = "xs" | "sm" | "md";

const VARIANT: Record<Variant, string> = {
  primary: "bg-accent text-accent-fg hover:bg-accent-hover shadow-[var(--shadow-xs)]",
  secondary: "bg-raised text-fg border border-line hover:bg-hover hover:border-line-strong",
  subtle: "bg-sunken text-secondary hover:bg-hover hover:text-fg",
  ghost: "text-muted hover:bg-hover hover:text-fg",
  danger: "bg-transparent text-bad border border-bad-line hover:bg-bad-quiet",
};

const SIZE: Record<Size, string> = {
  xs: "h-6 px-2 text-[12.5px] gap-1 rounded-[var(--radius-sm)]",
  sm: "h-7 px-2.5 text-[13.5px] gap-1.5 rounded-[var(--radius-sm)]",
  md: "h-8 px-3 text-[14px] gap-1.5 rounded-[var(--radius-md)]",
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
      // A genuinely disabled button is drawn as a flat neutral chip rather than
      // as a faded copy of itself: a 40%-opacity primary still reads as a live
      // blue call to action, which is exactly the control the user is being
      // told they cannot press yet.
      //
      // A LOADING button keeps its own variant. It is not unavailable, it is
      // working, and greying it out mid-request looks like the click failed.
      className={`inline-flex shrink-0 items-center justify-center font-medium whitespace-nowrap
        transition-[background-color,border-color,color,box-shadow,transform] duration-100
        active:translate-y-px disabled:pointer-events-none
        ${VARIANT[variant]} ${SIZE[size]}
        ${disabled && !loading ? "!border-line !bg-sunken !text-faint !shadow-none" : ""}
        ${loading ? "cursor-progress" : ""} ${className}`}
    >
      {loading ? <Spinner size={size === "md" ? 13 : 11} /> : icon}
      {children}
    </button>
  );
}

export function Spinner({ size = 13 }: { size?: number }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      className="inline-block shrink-0 animate-spin rounded-full border-[1.5px] border-current border-t-transparent"
      style={{ width: size, height: size }}
    />
  );
}

/* ------------------------------------------------------------------- badge */

export type Tone = "neutral" | "ok" | "warn" | "bad" | "accent";

const TONE_SOFT: Record<Tone, string> = {
  neutral: "bg-sunken text-muted",
  ok: "bg-ok-quiet text-ok",
  warn: "bg-warn-quiet text-warn",
  bad: "bg-bad-quiet text-bad",
  accent: "bg-accent-quiet text-accent",
};

const DOT: Record<Tone, string> = {
  neutral: "bg-faint",
  ok: "bg-ok-vivid",
  warn: "bg-warn-vivid",
  bad: "bg-bad-vivid",
  accent: "bg-accent",
};

/**
 * Status is carried by a small colour dot plus a word, not by a filled block.
 * At table density a row of saturated pills becomes the loudest thing on the
 * screen and drowns the figures the row exists to show.
 */
export function Badge({
  tone = "neutral",
  children,
  dot = false,
  icon,
  className = "",
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  dot?: boolean;
  icon?: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] px-1.5 py-0.5
        text-[12.5px] font-medium whitespace-nowrap ${TONE_SOFT[tone]} ${className}`}
    >
      {dot && <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT[tone]}`} />}
      {icon}
      {children}
    </span>
  );
}

/** One place decides how a server verdict is coloured, so it can never be
 *  shown two different ways on two different screens. */
export const toneFor = (status?: string | null): Tone => {
  switch (status) {
    case "APPROVED":
    case "HUMAN_APPROVED":
    case "open":
      return "ok";
    case "NEEDS_REVIEW":
      return "warn";
    case "REJECTED":
    case "HUMAN_REJECTED":
    case "closed":
      return "bad";
    default:
      return "neutral";
  }
};

const STATUS_WORD: Record<string, string> = {
  APPROVED: "Approved",
  NEEDS_REVIEW: "Needs review",
  REJECTED: "Rejected",
  HUMAN_APPROVED: "Approved by reviewer",
  HUMAN_REJECTED: "Rejected by reviewer",
};

/**
 * A verdict is not an ordinary label, so it does not look like one.
 *
 * This used to be a `Badge` with a colour dot -- the same soft chip shape every
 * tag and count on the screen uses, with the outcome carried by six pixels of
 * colour beside the word. A status then read as the same KIND of thing as the
 * tags around it.
 *
 * It is a bordered pill now: the shape says "this is an outcome", the tone
 * says which one, and the word spells it out in full. No dot and no glyph --
 * a tick beside the word "Approved" and a cross beside "Rejected" say nothing
 * the word has not already said, and at this size they read as clutter rather
 * than as reinforcement. The tone tokens are the app's own, so it stays
 * legible in both themes and agrees with every other coloured surface.
 */
const STATUS_PILL: Record<Tone, string> = {
  ok: "border-ok-line bg-ok-quiet text-ok",
  warn: "border-warn-line bg-warn-quiet text-warn",
  bad: "border-bad-line bg-bad-quiet text-bad",
  accent: "border-accent-line bg-accent-quiet text-accent",
  neutral: "border-line bg-sunken text-muted",
};

export function StatusBadge({
  status,
  className = "",
}: {
  status?: string | null;
  className?: string;
}) {
  if (!status) return null;
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-[2px]
        text-[11.5px] leading-[16px] font-semibold whitespace-nowrap ${
          STATUS_PILL[toneFor(status)]
        } ${className}`}
    >
      {STATUS_WORD[status] ?? String(status).replace(/_/g, " ").toLowerCase()}
    </span>
  );
}

/* ----------------------------------------------------------------- surfaces */

/** The workhorse surface. `flush` drops padding for tables and lists that
 *  should meet the panel edge. */
export function Panel({
  children,
  className = "",
  flush = false,
  hover = false,
}: {
  children: ReactNode;
  className?: string;
  flush?: boolean;
  hover?: boolean;
}) {
  return (
    <section
      className={`panel overflow-hidden ${flush ? "" : "p-4"} ${
        hover ? "transition-colors hover:border-line-strong" : ""
      } ${className}`}
    >
      {children}
    </section>
  );
}

export function PanelHeader({
  title,
  description,
  actions,
  className = "",
  bordered = false,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
  bordered?: boolean;
}) {
  return (
    <div
      className={`flex flex-wrap items-start justify-between gap-3 ${
        bordered ? "border-b border-line px-4 py-3" : ""
      } ${className}`}
    >
      <div className="min-w-0">
        <h2 className="t-section">{title}</h2>
        {description && <p className="t-meta mt-0.5">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-1.5">{actions}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ inputs */

const FIELD =
  "rounded-[var(--radius-md)] border border-line bg-sunken text-[14px] text-fg " +
  "transition-colors placeholder:text-faint hover:border-line-strong " +
  "focus:border-accent focus:bg-surface focus:outline-none focus-visible:outline-none " +
  "disabled:cursor-not-allowed disabled:opacity-50";

export function Input({ className = "", ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...rest} className={`${FIELD} h-9 w-full px-3 ${className}`} />;
}

export function Textarea({
  className = "",
  ...rest
}: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...rest} className={`${FIELD} w-full resize-y px-3 py-2 ${className}`} />;
}

export function SearchInput({ className = "", ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className={`relative ${className}`}>
      <IconSearch
        className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-faint"
        size={14}
      />
      <input {...rest} type="search" className={`${FIELD} h-8 w-full pr-3 pl-[30px]`} />
    </div>
  );
}

/**
 * A native <select> keeps the platform's keyboard behaviour and its option
 * list — both of which a div-based menu has to reimplement badly — but the
 * OS-drawn arrow is the one piece of chrome that ignores the design system.
 * `appearance: none` (globals.css) strips it; this draws the replacement in
 * currentColor, so it tracks light/dark like every other icon.
 */
export function Select({
  className = "",
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className={`relative inline-flex shrink-0 ${className}`}>
      <select {...rest} className={`${FIELD} h-8 w-full cursor-pointer pr-7 pl-2.5`}>
        {children}
      </select>
      <IconChevronDown
        size={13}
        aria-hidden
        className="pointer-events-none absolute top-1/2 right-2 -translate-y-1/2 text-faint"
      />
    </div>
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
      <label htmlFor={htmlFor} className="mb-1.5 block text-[13.5px] font-medium text-secondary">
        {label}
      </label>
      {children}
      {hint && <p className="t-meta mt-1">{hint}</p>}
    </div>
  );
}

/* --------------------------------------------------------------- segmented */

export function Segmented<T extends string>({
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
      // `self-start` matters: as a direct child of a flex column (the
      // Reference page) `inline-flex` alone is still stretched to the full
      // container width by the default `align-items: stretch`, which turned a
      // compact tab group into a full-bleed bar.
      className="inline-flex w-fit shrink-0 items-center gap-0.5 self-start rounded-[var(--radius-md)]
        border border-line bg-sunken p-0.5"
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
              text-[13.5px] font-medium transition-colors ${
                active ? "bg-raised text-fg shadow-[var(--shadow-xs)]" : "text-muted hover:text-fg"
              }`}
          >
            {o.label}
            {o.count !== undefined && (
              <span className="tnum text-[12px] text-faint">{o.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ states */

export function EmptyState({
  title,
  description,
  action,
  icon,
  compact = false,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div
      className={`flex flex-col items-center gap-2.5 text-center ${compact ? "px-4 py-8" : "px-6 py-14"}`}
    >
      <div className="grid h-9 w-9 place-items-center rounded-[var(--radius-md)] border border-line bg-sunken text-faint">
        {icon ?? <IconEmpty size={16} />}
      </div>
      <div>
        <p className="text-[14px] font-medium">{title}</p>
        {description && <p className="t-meta mx-auto mt-1 max-w-xs">{description}</p>}
      </div>
      {action}
    </div>
  );
}

export function ErrorState({
  title = "Could not load",
  description,
  onRetry,
}: {
  title?: string;
  description?: ReactNode;
  onRetry?: () => void;
}) {
  return (
    <div role="alert" className="flex flex-col items-center gap-2.5 px-6 py-12 text-center">
      <div className="grid h-9 w-9 place-items-center rounded-[var(--radius-md)] border border-bad-line bg-bad-quiet text-bad">
        <IconAlert size={16} />
      </div>
      <div>
        <p className="text-[14px] font-medium">{title}</p>
        {description && <p className="t-meta mx-auto mt-1 max-w-xs">{description}</p>}
      </div>
      {onRetry && (
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

/** Inline message. The left rule carries the tone; the fill stays quiet. */
export function Callout({
  tone = "neutral",
  title,
  children,
  icon,
  className = "",
}: {
  tone?: Tone;
  title?: ReactNode;
  children?: ReactNode;
  icon?: ReactNode;
  className?: string;
}) {
  const accent = {
    neutral: "var(--line-strong)",
    ok: "var(--ok-vivid)",
    warn: "var(--warn-vivid)",
    bad: "var(--bad-vivid)",
    accent: "var(--accent)",
  }[tone];

  return (
    <div
      className={`rounded-[var(--radius-md)] px-3 py-2.5 text-[13.5px] ${TONE_SOFT[tone]} ${className}`}
      style={{ borderLeft: `2px solid ${accent}` }}
    >
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

export function Skeleton({
  className = "",
  style,
}: {
  className?: string;
  style?: React.CSSProperties;
}) {
  return <div className={`skeleton ${className}`} style={style} aria-hidden />;
}

export function SkeletonRows({ rows = 6, cols = 5 }: { rows?: number; cols?: number }) {
  const widths = [30, 20, 14, 12, 16, 10];
  return (
    <div className="divide-line" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex items-center gap-4 px-4 py-2.5">
          {Array.from({ length: cols }).map((_, c) => (
            <Skeleton key={c} className="h-3" style={{ width: `${widths[c % 6]}%` }} />
          ))}
        </div>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- tooltip */

/**
 * Shows on hover AND keyboard focus, which a `title` attribute never does.
 *
 * POSITIONED IN A PORTAL, AGAINST THE VIEWPORT -- not absolutely, inside the
 * trigger. The absolutely-positioned version was laid out relative to whatever
 * the nearest positioned ancestor happened to be, and it was `whitespace-nowrap`,
 * so a long label on a trigger near the right-hand edge of a panel (the "?" on
 * every Overview KPI card is exactly that) rendered as one long line that
 * escaped its card, ran across its neighbours and was clipped by the window --
 * unreadable text drawn on top of readable text.
 *
 * Two things fix it and both are needed. The label WRAPS inside a bounded
 * width, so a sentence is a small block rather than a 700px line; and the block
 * is measured after it mounts and then CLAMPED into the viewport, so it can
 * shift sideways to stay on screen and flip below the trigger when there is no
 * room above. `position: fixed` in a body portal is what makes that possible at
 * all -- inside the card, `overflow` and stacking contexts on any ancestor get
 * a vote, and several of them do clip.
 */
export function Tooltip({
  label,
  children,
  side = "top",
}: {
  label: string;
  children: ReactNode;
  /** Preferred side. Honoured when it fits; flipped when it does not. */
  side?: "top" | "right";
}) {
  const triggerRef = useRef<HTMLSpanElement | null>(null);
  const bubbleRef = useRef<HTMLSpanElement | null>(null);
  const [open, setOpen] = useState(false);
  // Null until measured. Rendering at 0,0 for one frame would flash the
  // tooltip in the corner of the screen before it jumps into place.
  const [at, setAt] = useState<{ top: number; left: number } | null>(null);

  const place = useCallback(() => {
    const trigger = triggerRef.current?.getBoundingClientRect();
    const bubble = bubbleRef.current?.getBoundingClientRect();
    if (!trigger || !bubble) return;

    const gap = 8;
    const margin = 8; // never closer than this to a window edge
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    let top: number;
    let left: number;

    if (side === "right" && trigger.right + gap + bubble.width + margin <= vw) {
      left = trigger.right + gap;
      top = trigger.top + trigger.height / 2 - bubble.height / 2;
    } else {
      left = trigger.left + trigger.width / 2 - bubble.width / 2;
      // Above by preference; below when the top of the window is in the way.
      top = trigger.top - gap - bubble.height;
      if (top < margin) top = trigger.bottom + gap;
    }

    // Clamp last, so the flip above cannot push it back off screen.
    left = Math.min(Math.max(margin, left), Math.max(margin, vw - bubble.width - margin));
    top = Math.min(Math.max(margin, top), Math.max(margin, vh - bubble.height - margin));

    setAt({ top, left });
  }, [side]);

  // Layout effect, not effect: measure and place before the browser paints, so
  // the tooltip is never visible in the wrong position even for one frame.
  useLayoutEffect(() => {
    if (!open) {
      setAt(null);
      return;
    }
    place();
    // A tooltip anchored to a row in a scrolling panel has to follow it, and
    // one near an edge has to re-clamp when the window changes size.
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, place]);

  return (
    <>
      <span
        ref={triggerRef}
        className="relative inline-flex"
        onPointerEnter={() => setOpen(true)}
        onPointerLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      >
        {children}
      </span>
      {open &&
        typeof document !== "undefined" &&
        createPortal(
          <span
            ref={bubbleRef}
            role="tooltip"
            style={{
              top: at?.top ?? 0,
              left: at?.left ?? 0,
              // Hidden rather than unmounted until measured: it has to be in
              // the document to have a width to measure.
              visibility: at ? "visible" : "hidden",
            }}
            className="pointer-events-none fixed z-[100] max-w-[min(22rem,calc(100vw-1rem))]
              rounded-[var(--radius-sm)] border border-line bg-raised px-2 py-1
              text-[12.5px] leading-snug text-fg shadow-[var(--shadow-md)]"
          >
            {label}
          </span>,
          document.body
        )}
    </>
  );
}

/* ------------------------------------------------------------------ table */

/**
 * The product's one table.
 *
 * Every list of records — the invoice register, the PO ledger, the three-way
 * match — renders through this, so column rhythm, header treatment, hairlines
 * and row hover are decided once. Before this each page hand-rolled its own
 * `<th className="t-caption border-b …">`, and they had already drifted: three
 * different header paddings and two different row heights across four screens.
 *
 * Horizontal overflow is owned here too. A wide table must scroll inside its
 * own panel rather than widening the page, which is what put a horizontal
 * scrollbar on the whole document at tablet width.
 */
export function DataTable({
  children,
  minWidth,
  className = "",
}: {
  children: ReactNode;
  /** Below this the table scrolls instead of crushing its columns. */
  minWidth?: number;
  className?: string;
}) {
  return (
    <div className={`w-full overflow-x-auto ${className}`}>
      <table className="dt" style={minWidth ? { minWidth } : undefined}>
        {children}
      </table>
    </div>
  );
}

export function TH({
  children,
  align = "left",
  className = "",
  ...rest
}: React.ThHTMLAttributes<HTMLTableCellElement> & { align?: "left" | "right" }) {
  return (
    /* `data-align` rather than the utility class alone: the table stylesheet
       styles headers through a descendant selector (`table.dt > thead > tr >
       th`), which lives in the same cascade layer as Tailwind's utilities and
       out-specifies them -- so `text-right` here was silently overruled and
       every right-aligned header in the product sat left of the figures under
       it. globals.css matches this attribute at a specificity that wins. */
    <th
      scope="col"
      data-align={align}
      {...rest}
      className={`${align === "right" ? "text-right" : "text-left"} ${className}`}
    >
      {children}
    </th>
  );
}

/**
 * A sortable column header.
 *
 * The direction caret is always rendered, at low opacity when the column is
 * not the active sort, so the header row does not reflow by a few pixels the
 * moment someone sorts by it — and so the column reads as sortable before it
 * is clicked. The previous version set `opacity-0`, which hid the affordance
 * entirely: nothing on the screen said the columns could be sorted at all.
 */
export function SortTH({
  label,
  active,
  ascending,
  onSort,
  align = "left",
  className = "",
}: {
  label: string;
  active: boolean;
  ascending: boolean;
  onSort: () => void;
  align?: "left" | "right";
  className?: string;
}) {
  return (
    <th
      scope="col"
      aria-sort={active ? (ascending ? "ascending" : "descending") : "none"}
      className={`${align === "right" ? "text-right" : "text-left"} ${className}`}
    >
      <button
        type="button"
        onClick={onSort}
        aria-label={`Sort by ${label}${active ? (ascending ? ", ascending" : ", descending") : ""}`}
        // The caret always trails the label, in both alignments. Reversing
        // it for right-aligned columns put the marker before the word — the
        // header read "\u25BC Amount", which parses as a bullet, not a sort
        // direction.
        className={`inline-flex items-center gap-1 rounded-[var(--radius-xs)] transition-colors
          hover:text-fg ${active ? "text-fg" : ""}`}
      >
        {label}
        <span
          aria-hidden
          className={`text-[10px] leading-none transition-opacity ${
            active ? "text-accent opacity-100" : "opacity-30"
          }`}
        >
          {active && ascending ? "▲" : "▼"}
        </span>
      </button>
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
    <td {...rest} className={`${align === "right" ? "num" : ""} ${className}`}>
      {children}
    </td>
  );
}

/** Key/value list used across the detail panels. */
export function KeyValues({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="divide-line">
      {rows.map(([k, v], i) => (
        <div key={i} className="flex items-baseline justify-between gap-4 py-1.5">
          <dt className="t-meta shrink-0">{k}</dt>
          <dd className="min-w-0 text-right text-[13.5px] font-medium break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Thin proportion bar. Used for budgets and rankings. */
export function Meter({
  value,
  max,
  tone = "accent",
  height = 4,
  ariaLabel,
}: {
  value: number;
  max: number;
  tone?: Tone;
  height?: number;
  ariaLabel: string;
}) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const fill = {
    neutral: "var(--fg-faint)",
    ok: "var(--ok-vivid)",
    warn: "var(--warn-vivid)",
    bad: "var(--bad-vivid)",
    accent: "var(--accent)",
  }[tone];

  return (
    <div
      role="img"
      aria-label={ariaLabel}
      className="w-full overflow-hidden rounded-full bg-sunken"
      style={{ height }}
    >
      <div
        className="h-full rounded-full transition-[width] duration-500 ease-out"
        style={{ width: `${pct}%`, background: fill }}
      />
    </div>
  );
}
