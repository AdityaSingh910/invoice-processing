"use client";

/** The nine pipeline stages. Stages do not short-circuit: findings accumulate
 *  and only DECISION judges, so every row stays visible and reports its own
 *  outcome rather than the run stopping at the first problem. */
import { LEVEL_ICON, STAGE_LABEL, STAGE_ORDER } from "@/lib/format";
import type { Stage } from "@/lib/types";

const TONE: Record<string, string> = { ok: "ok", warn: "warn", fail: "fail", info: "accent" };

/** A rail runs down the left so the nine stages read as one journey rather
 *  than nine unrelated rows. */
function Rail({ last, children }: { last: boolean; children: React.ReactNode }) {
  return (
    <div className="relative flex justify-center">
      {!last && (
        <span
          className="absolute top-8 bottom-0 w-0.5 rounded-full"
          style={{ background: "var(--border)" }}
          aria-hidden
        />
      )}
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
    <div className="pop grid grid-cols-[32px_minmax(0,1fr)] gap-3.5 pb-4 last:pb-0">
      <Rail last={last}>
        <span
          className="z-10 grid h-8 w-8 place-items-center rounded-full text-[13px] font-extrabold text-white"
          style={{ background: `var(--grad-${tone}, var(--grad-accent))` }}
        >
          {LEVEL_ICON[stage.status] ?? index + 1}
        </span>
      </Rail>

      <div className="min-w-0 pt-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <span className="text-[15px] font-bold">{STAGE_LABEL[stage.name] ?? stage.name}</span>
          <div className="flex items-center gap-2">
            <span className="rounded-full bg-panel3 px-2 py-0.5 font-mono text-[10px] font-bold text-faint">
              {stage.name}
            </span>
            {stage.ms !== undefined && (
              <span className="text-[11px] text-faint tabular-nums">{stage.ms} ms</span>
            )}
          </div>
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
            className={`grid grid-cols-[32px_minmax(0,1fr)] gap-3.5 pb-4 transition-opacity last:pb-0 ${
              active ? "opacity-100" : "opacity-35"
            }`}
          >
            <Rail last={last}>
              <span
                className="z-10 grid h-8 w-8 place-items-center rounded-full border-2 border-dashed text-[12px] font-bold text-faint"
                style={{ borderColor: "var(--border-strong)", background: "var(--panel-2)" }}
              >
                {active ? (
                  <span className="block h-3.5 w-3.5 animate-spin rounded-full border-2 border-accent border-t-transparent" />
                ) : (
                  i + 1
                )}
              </span>
            </Rail>
            <div className="min-w-0 pt-1">
              <div className="text-[15px] font-bold">{STAGE_LABEL[name] ?? name}</div>
              <div className="mt-0.5 text-[14px] text-dim">
                {active ? "Working on it…" : "Waiting"}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
