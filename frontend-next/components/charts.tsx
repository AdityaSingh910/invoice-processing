"use client";

/**
 * Charts — hand-drawn SVG, no charting dependency.
 *
 * Deliberately spare: no gridlines, no axis furniture, no legends repeating
 * what a label already says. At this data volume the shape is the message, and
 * the exact figures live in the tiles and tables beside them.
 *
 * Every series is keyed to the same three status colours used everywhere else,
 * so a colour never means two things.
 */
import type { DayBucket } from "@/lib/metrics";

const STATUS_FILL = {
  approved: "var(--success-solid)",
  needsReview: "var(--warning-solid)",
  rejected: "var(--danger-solid)",
} as const;

/**
 * Runs per day, stacked by outcome.
 *
 * Bars are drawn as percentages inside a flex row rather than a scaled SVG so
 * the chart reflows with its container — a fixed viewBox would letterbox on
 * mobile.
 */
export function DailyVolume({ data }: { data: DayBucket[] }) {
  const peak = Math.max(1, ...data.map((d) => d.total));
  const hasAny = data.some((d) => d.total > 0);

  return (
    <div>
      <div className="flex h-32 items-end gap-1" role="img" aria-label="Runs per day by outcome">
        {data.map((d) => {
          const h = (d.total / peak) * 100;
          return (
            <div key={d.day} className="group relative flex h-full flex-1 flex-col justify-end">
              {d.total > 0 ? (
                <div
                  className="flex w-full flex-col-reverse overflow-hidden rounded-[3px] transition-opacity group-hover:opacity-80"
                  style={{ height: `${Math.max(h, 4)}%` }}
                >
                  {(["approved", "needsReview", "rejected"] as const).map((k) =>
                    d[k] > 0 ? (
                      <div
                        key={k}
                        style={{ height: `${(d[k] / d.total) * 100}%`, background: STATUS_FILL[k] }}
                      />
                    ) : null
                  )}
                </div>
              ) : (
                <div className="h-0.5 w-full rounded-full bg-border" />
              )}

              {/* Tooltip: hover only, and it never covers the bar it describes. */}
              <div
                className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1.5 hidden
                  -translate-x-1/2 rounded-[var(--radius-sm)] border border-border bg-surface
                  px-2 py-1 text-[11px] whitespace-nowrap shadow-[var(--shadow-md)] group-hover:block"
              >
                <div className="font-semibold">{d.label}</div>
                <div className="text-muted">
                  {d.total} run{d.total === 1 ? "" : "s"}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-2 flex justify-between text-[11px] text-subtle">
        <span>{data[0]?.label}</span>
        {!hasAny && <span>No activity in this period</span>}
        <span>{data[data.length - 1]?.label}</span>
      </div>
    </div>
  );
}

/** A single proportion bar. Used for PO budgets and the outcome mix. */
export function MeterBar({
  segments,
  height = 8,
  ariaLabel,
}: {
  segments: { value: number; color: string; label?: string }[];
  height?: number;
  ariaLabel: string;
}) {
  const total = segments.reduce((a, s) => a + s.value, 0);

  return (
    <div
      role="img"
      aria-label={ariaLabel}
      className="flex w-full overflow-hidden rounded-full bg-surface2"
      style={{ height }}
    >
      {total > 0 &&
        segments.map((s, i) =>
          s.value > 0 ? (
            <div
              key={i}
              title={s.label}
              style={{ width: `${(s.value / total) * 100}%`, background: s.color }}
            />
          ) : null
        )}
    </div>
  );
}

/** Legend chip; pairs with MeterBar so colours are never left unexplained. */
export function LegendDot({
  color,
  label,
  value,
}: {
  color: string;
  label: string;
  value?: string | number;
}) {
  return (
    <div className="flex items-center gap-1.5 text-[12px]">
      <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
      <span className="text-muted">{label}</span>
      {value !== undefined && <span className="num font-semibold">{value}</span>}
    </div>
  );
}

export const CHART_COLORS = STATUS_FILL;
