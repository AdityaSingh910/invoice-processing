"use client";

/**
 * Charts — hand-drawn, no charting dependency.
 *
 * Restrained by intent: two gridlines, no chart border, no axis spines, no
 * legend repeating a label that is already on screen. At this data volume the
 * shape is the message and the exact figures live in the tiles beside it.
 *
 * Series colours are the same three status colours used everywhere else, so a
 * colour never means two different things.
 */
import { useState } from "react";
import type { DayBucket } from "@/lib/metrics";
import { EmptyState } from "@/components/ui";
import { IconOverview } from "@/components/ui/icons";

export const SERIES = {
  approved: "var(--ok-vivid)",
  needsReview: "var(--warn-vivid)",
  rejected: "var(--bad-vivid)",
} as const;

/**
 * Daily volume, stacked by outcome.
 *
 * Bars are percentage-width flex children rather than a fixed viewBox so the
 * chart reflows with its container instead of letterboxing on a narrow screen.
 */
export function VolumeChart({ data }: { data: DayBucket[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const peak = Math.max(...data.map((d) => d.total));
  const active = hover !== null ? data[hover] : null;

  // A chart of fourteen empty columns says nothing; say it in words instead.
  if (peak === 0) {
    return (
      <EmptyState
        compact
        icon={<IconOverview size={16} />}
        title="No invoices in the last 14 days"
        description="Volume by day will appear here once invoices are processed."
      />
    );
  }

  // Round the axis up to something readable rather than to the raw peak.
  const step = peak <= 4 ? 1 : peak <= 10 ? 2 : Math.ceil(peak / 4);
  const top = Math.ceil(peak / step) * step;
  const lines = Array.from({ length: Math.min(4, top / step) + 1 }, (_, i) => i * step).reverse();

  return (
    <div>
      <div className="flex gap-3">
        {/* y-axis labels, outside the plot so they never overlap a bar */}
        <div
          className="flex w-6 shrink-0 flex-col justify-between text-right"
          style={{ height: 132 }}
          aria-hidden
        >
          {lines.map((v) => (
            <span key={v} className="tnum text-[10px] leading-none text-faint">
              {v}
            </span>
          ))}
        </div>

        <div className="relative min-w-0 flex-1">
          {/* gridlines sit behind the bars at the same intervals as the labels */}
          <div className="absolute inset-0 flex flex-col justify-between" aria-hidden>
            {lines.map((v) => (
              <div
                key={v}
                className="w-full border-t"
                style={{ borderColor: v === 0 ? "var(--line-strong)" : "var(--line)" }}
              />
            ))}
          </div>

          <div
            className="relative flex items-end gap-[3px]"
            style={{ height: 132 }}
            role="img"
            aria-label={`Invoices per day for the last ${data.length} days`}
            onMouseLeave={() => setHover(null)}
          >
            {data.map((d, i) => (
              <div
                key={d.day}
                onMouseEnter={() => setHover(i)}
                className="flex h-full flex-1 cursor-default flex-col justify-end"
              >
                {d.total > 0 ? (
                  <div
                    className="flex w-full flex-col-reverse overflow-hidden rounded-[2px] transition-opacity duration-100"
                    style={{
                      height: `${(d.total / top) * 100}%`,
                      opacity: hover === null || hover === i ? 1 : 0.35,
                    }}
                  >
                    {(["approved", "needsReview", "rejected"] as const).map((k) =>
                      d[k] > 0 ? (
                        <div
                          key={k}
                          style={{ height: `${(d[k] / d.total) * 100}%`, background: SERIES[k] }}
                        />
                      ) : null
                    )}
                  </div>
                ) : (
                  <div
                    className="w-full rounded-full bg-line"
                    style={{ height: 2, opacity: hover === i ? 1 : 0.6 }}
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* One readout line rather than a floating tooltip: no occlusion, and it
          holds a fixed height so the panel does not jump on hover. */}
      <div className="mt-2.5 flex items-center justify-between gap-3 pl-9">
        <span className="t-meta text-[11px]">{data[0]?.label}</span>
        <span className="text-[11.5px]" style={{ minHeight: 17 }}>
          {active ? (
            <span className="flex items-center gap-2.5">
              <span className="font-medium">{active.label}</span>
              {active.total === 0 ? (
                <span className="t-meta">no activity</span>
              ) : (
                (["approved", "needsReview", "rejected"] as const)
                  .filter((k) => active[k] > 0)
                  .map((k) => (
                    <span key={k} className="flex items-center gap-1 text-muted">
                      <span
                        className="h-1.5 w-1.5 rounded-full"
                        style={{ background: SERIES[k] }}
                      />
                      <span className="tnum">{active[k]}</span>
                    </span>
                  ))
              )}
            </span>
          ) : (
            <span className="t-meta text-[11px]">Hover a column for detail</span>
          )}
        </span>
        <span className="t-meta text-[11px]">{data[data.length - 1]?.label}</span>
      </div>
    </div>
  );
}

/** Legend chip. Pairs with the chart so colours are never left unexplained. */
export function LegendItem({
  color,
  label,
  value,
}: {
  color: string;
  label: string;
  value?: number;
}) {
  return (
    <span className="flex items-center gap-1.5 text-[11.5px]">
      <span className="h-2 w-2 shrink-0 rounded-[2px]" style={{ background: color }} />
      <span className="text-muted">{label}</span>
      {value !== undefined && <span className="tnum font-medium">{value}</span>}
    </span>
  );
}

/**
 * Sparkline for a KPI tile. Deliberately unlabelled — it shows direction, and
 * the exact figures are elsewhere on the same card.
 */
export function Sparkline({
  values,
  tone = "var(--accent)",
  width = 90,
  height = 26,
}: {
  values: number[];
  tone?: string;
  width?: number;
  height?: number;
}) {
  if (values.length < 2) return null;

  const max = Math.max(...values, 1);
  const step = width / (values.length - 1);
  const y = (v: number) => height - (v / max) * (height - 3) - 1.5;
  const line = values.map((v, i) => `${i * step},${y(v)}`).join(" ");
  const area = `0,${height} ${line} ${width},${height}`;

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
      <polygon points={area} fill={tone} opacity={0.1} />
      <polyline
        points={line}
        fill="none"
        stroke={tone}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
