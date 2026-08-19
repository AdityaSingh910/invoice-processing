"use client";

/** The nine pipeline stages. Stages do not short-circuit: findings accumulate
 *  and only DECISION judges, so every row stays visible and reports its own
 *  outcome rather than the run stopping at the first problem. */
import { LEVEL_ICON, STAGE_ORDER } from "@/lib/format";
import type { Stage } from "@/lib/types";

const TONE: Record<string, string> = { ok: "ok", warn: "warn", fail: "fail", info: "accent" };

/** A rail runs down the left so the nine stages read as one sequence rather
 *  than nine unrelated rows. */
function Rail({ last, children }: { last: boolean; children: React.ReactNode }) {
  return (
    <div className="relative flex justify-center">
      {!last && <span className="absolute top-7 bottom-0 w-px bg-border" aria-hidden />}
      {children}
    </div>
  );
}

export function StageRow({
  stage,
  index,
  last = false,
}: {
  stage: Stage;
  index: number;
  last?: boolean;
}) {
  const tone = TONE[stage.status] ?? "accent";
  return (
    <div className="grid grid-cols-[28px_minmax(0,1fr)] gap-3 pb-4 last:pb-0">
      <Rail last={last}>
        <span
          className="z-10 grid h-7 w-7 place-items-center rounded-full border-2 text-[12px] font-bold"
          style={{
            background: `var(--${tone}-soft)`,
            color: `var(--${tone})`,
            borderColor: "var(--panel)",
            boxShadow: `0 0 0 1px var(--${tone}-border, var(--border))`,
          }}
        >
          {LEVEL_ICON[stage.status] ?? index + 1}
        </span>
      </Rail>

      <div className="min-w-0 pt-0.5">
        <div className="flex items-baseline justify-between gap-3">
          <span className="font-mono text-[12px] font-bold tracking-[0.04em]">{stage.name}</span>
          {stage.ms !== undefined && (
            <span className="shrink-0 rounded-md bg-panel2 px-1.5 py-0.5 text-[11px] text-faint tabular-nums">
              {stage.ms} ms
            </span>
          )}
        </div>
        <div className="mt-0.5 text-[14px] text-dim">{stage.detail}</div>
      </div>
    </div>
  );
}

/** Live view: every stage is listed up-front as pending, then filled in as the
 *  stream reports it, so the shape of the process is visible before it runs. */
export default function StageList({ stages, running }: { stages: Stage[]; running: boolean }) {
  const byName = new Map(stages.map((s) => [s.name, s]));
  const nextPending = STAGE_ORDER.find((n) => !byName.has(n));

  return (
    <div>
      {STAGE_ORDER.map((name, i) => {
        const last = i === STAGE_ORDER.length - 1;
        const done = byName.get(name);
        if (done) return <StageRow key={name} stage={done} index={i} last={last} />;

        const active = running && name === nextPending;
        return (
          <div
            key={name}
            className={`grid grid-cols-[28px_minmax(0,1fr)] gap-3 pb-4 transition-opacity last:pb-0 ${
              active ? "opacity-100" : "opacity-40"
            }`}
          >
            <Rail last={last}>
              <span className="z-10 grid h-7 w-7 place-items-center rounded-full border border-border bg-panel2 text-[11px] text-faint">
                {active ? (
                  <span className="block h-3 w-3 animate-spin rounded-full border-2 border-accent border-t-transparent" />
                ) : (
                  i + 1
                )}
              </span>
            </Rail>
            <div className="min-w-0 pt-0.5">
              <div className="font-mono text-[12px] font-bold tracking-[0.04em]">{name}</div>
              <div className="mt-0.5 text-[14px] text-dim">
                {active ? "Running…" : "Waiting…"}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
