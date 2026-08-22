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
            <span key={v} className="tnum text-[11px] leading-none text-faint">
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
                className="flex h-full flex-1 cursor-default flex-col items-center justify-end"
              >
                {d.total > 0 ? (
                  <div
                    // Capped and centred rather than filling its column.
                    // A fourteen-day axis holding a single busy day drew one
                    // full-width block against thirteen empty columns, which
                    // read as a stray rectangle rather than as a bar in a
                    // series. A capped width keeps it recognisably a bar
                    // whether one day has data or all fourteen do.
                    className="flex w-full max-w-[26px] flex-col-reverse overflow-hidden rounded-[3px] transition-opacity duration-100"
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
                  // A day with nothing in it still gets a mark, so the axis
                  // reads as fourteen days rather than as blank space that
                  // might be a rendering failure.
                  <div
                    className="w-full max-w-[26px] rounded-full bg-line"
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
        <span className="t-meta text-[12px]">{data[0]?.label}</span>
        <span className="text-[12.5px]" style={{ minHeight: 17 }}>
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
            <span className="t-meta text-[12px]">Hover a column for detail</span>
          )}
        </span>
        <span className="t-meta text-[12px]">{data[data.length - 1]?.label}</span>
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
    <span className="flex items-center gap-1.5 text-[12.5px]">
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

/**
 * A rate over time, drawn as a line with the gaps left as gaps.
 *
 * THE POINT OF THIS COMPONENT IS WHAT IT REFUSES TO DRAW. The analytics API
 * returns `null` for a day with no invoices, because there was no automation
 * rate that day — not a rate of zero. A line chart that coerces null to 0
 * draws a cliff to the floor and back, which reads as a catastrophic outage
 * and is a statement the data does not make. So a null breaks the line: the
 * series is drawn as separate segments, and days with no data get no point.
 *
 * Values are fractions in [0, 1] — the axis is fixed to that range rather than
 * scaled to the data, so a run of 96–98% is visibly near the top instead of
 * being stretched to look like a collapse.
 */
export function RateTrend({
  points,
  tone = "var(--accent)",
  height = 132,
  label,
}: {
  points: { day: string; label: string; value: number | null }[];
  tone?: string;
  height?: number;
  label: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const measured = points.filter((p) => p.value !== null);

  if (measured.length === 0) {
    return (
      <EmptyState
        compact
        icon={<IconOverview size={16} />}
        title="Nothing to chart yet"
        description={`${label} needs at least one day with invoices in it.`}
      />
    );
  }

  const W = 100;                          // viewBox units; the SVG scales to fit
  const step = points.length > 1 ? W / (points.length - 1) : 0;
  const y = (v: number) => height - v * (height - 8) - 4;
  const x = (i: number) => i * step;

  // Split into runs of consecutive measured days, so a gap stays a gap.
  const segments: { i: number; value: number }[][] = [];
  let current: { i: number; value: number }[] = [];
  points.forEach((p, i) => {
    if (p.value === null) {
      if (current.length) segments.push(current);
      current = [];
    } else {
      current.push({ i, value: p.value });
    }
  });
  if (current.length) segments.push(current);

  const active = hover !== null ? points[hover] : null;

  return (
    <div>
      <div className="flex gap-3">
        <div
          className="flex w-7 shrink-0 flex-col justify-between text-right"
          style={{ height }}
          aria-hidden
        >
          {[100, 50, 0].map((v) => (
            <span key={v} className="tnum text-[11px] leading-none text-faint">
              {v}%
            </span>
          ))}
        </div>

        <div className="relative min-w-0 flex-1">
          <div className="absolute inset-0 flex flex-col justify-between" aria-hidden>
            {[1, 0.5, 0].map((v) => (
              <div
                key={v}
                className="w-full border-t"
                style={{ borderColor: v === 0 ? "var(--line-strong)" : "var(--line)" }}
              />
            ))}
          </div>

          <svg
            viewBox={`0 0 ${W} ${height}`}
            preserveAspectRatio="none"
            style={{ height, width: "100%", display: "block" }}
            role="img"
            aria-label={`${label} by day`}
            onMouseLeave={() => setHover(null)}
          >
            {segments.map((seg, s) =>
              seg.length === 1 ? (
                // A single measured day between two gaps has no line to be
                // part of, so it is drawn as a point rather than dropped.
                <circle
                  key={s}
                  cx={x(seg[0].i)}
                  cy={y(seg[0].value)}
                  r={1.6}
                  fill={tone}
                  vectorEffect="non-scaling-stroke"
                />
              ) : (
                <polyline
                  key={s}
                  points={seg.map((p) => `${x(p.i)},${y(p.value)}`).join(" ")}
                  fill="none"
                  stroke={tone}
                  strokeWidth={1.5}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  vectorEffect="non-scaling-stroke"
                />
              )
            )}

            {hover !== null && points[hover].value !== null && (
              <circle
                cx={x(hover)}
                cy={y(points[hover].value as number)}
                r={2.2}
                fill={tone}
                vectorEffect="non-scaling-stroke"
              />
            )}

            {/* Full-height hover targets: a 1.5px line is impossible to hit. */}
            {points.map((p, i) => (
              <rect
                key={p.day}
                x={Math.max(0, x(i) - step / 2)}
                y={0}
                width={step || W}
                height={height}
                fill="transparent"
                onMouseEnter={() => setHover(i)}
              />
            ))}
          </svg>
        </div>
      </div>

      <div className="mt-2.5 flex items-center justify-between gap-3 pl-10">
        <span className="t-meta text-[12px]">{points[0]?.label}</span>
        <span className="text-[12.5px]" style={{ minHeight: 17 }}>
          {active ? (
            <span className="flex items-center gap-2.5">
              <span className="font-medium">{active.label}</span>
              {active.value === null ? (
                // Said in words, because there is no number to show and "0%"
                // would be the wrong one.
                <span className="t-meta">no invoices that day</span>
              ) : (
                <span className="tnum text-muted">{(active.value * 100).toFixed(0)}%</span>
              )}
            </span>
          ) : (
            <span className="t-meta text-[12px]">Hover a day for detail</span>
          )}
        </span>
        <span className="t-meta text-[12px]">{points[points.length - 1]?.label}</span>
      </div>
    </div>
  );
}

/**
 * A horizontal proportion bar split into labelled segments.
 *
 * Used for the decision mix and the ingestion funnel, where the question is
 * "how does this divide up" rather than "how has it moved". Segments below a
 * visible width still render, so a category that exists is never invisible.
 */
export function SplitBar({
  segments,
  total,
  height = 8,
  ariaLabel,
}: {
  segments: { label: string; value: number; color: string }[];
  total: number;
  height?: number;
  ariaLabel: string;
}) {
  const present = segments.filter((s) => s.value > 0);

  if (total <= 0 || present.length === 0) {
    return (
      <div
        role="img"
        aria-label={`${ariaLabel}: no data`}
        className="w-full rounded-full bg-sunken"
        style={{ height }}
      />
    );
  }

  return (
    <div
      role="img"
      aria-label={ariaLabel}
      className="flex w-full overflow-hidden rounded-full bg-sunken"
      style={{ height }}
    >
      {present.map((s) => (
        <div
          key={s.label}
          title={`${s.label}: ${s.value}`}
          style={{
            // A floor of 2%, so a single invoice in a category of a thousand
            // is still a visible sliver rather than a rounding error.
            width: `${Math.max(2, (s.value / total) * 100)}%`,
            background: s.color,
          }}
        />
      ))}
    </div>
  );
}
