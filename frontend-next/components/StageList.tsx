"use client";

/** The nine pipeline stages. Stages do not short-circuit: findings accumulate
 *  and only DECISION judges, so every row stays visible and reports its own
 *  outcome rather than the run stopping at the first problem. */
import { LEVEL_ICON, STAGE_ORDER } from "@/lib/format";
import type { Stage } from "@/lib/types";

const LEVEL_STYLE: Record<string, { bg: string; fg: string }> = {
  ok: { bg: "var(--ok-soft)", fg: "var(--ok)" },
  warn: { bg: "var(--warn-soft)", fg: "var(--warn)" },
  fail: { bg: "var(--fail-soft)", fg: "var(--fail)" },
  info: { bg: "var(--accent-soft)", fg: "var(--accent)" },
};

export function StageRow({ stage, index }: { stage: Stage; index: number }) {
  const style = LEVEL_STYLE[stage.status] ?? LEVEL_STYLE.info;
  return (
    <div className="flex items-start gap-3 border-b border-border py-2.5 last:border-0">
      <div
        className="dot mt-0.5 h-6 w-6 text-xs"
        data-level={stage.status}
        style={{ background: style.bg, color: style.fg }}
      >
        {LEVEL_ICON[stage.status] ?? index + 1}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <span className="font-mono text-[12px] font-semibold tracking-wide">{stage.name}</span>
          {stage.ms !== undefined && <span className="text-[11px] text-faint">{stage.ms} ms</span>}
        </div>
        <div className="text-dim">{stage.detail}</div>
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
        const done = byName.get(name);
        if (done) return <StageRow key={name} stage={done} index={i} />;

        const active = running && name === nextPending;
        return (
          <div
            key={name}
            className={`flex items-start gap-3 border-b border-border py-2.5 last:border-0 ${
              active ? "" : "opacity-45"
            }`}
          >
            <div className="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full border border-border bg-panel2 text-[11px] text-faint">
              {active ? (
                <span className="block h-2.5 w-2.5 animate-spin rounded-full border-2 border-accent border-t-transparent" />
              ) : (
                i + 1
              )}
            </div>
            <div className="min-w-0 flex-1">
              <div className="font-mono text-[12px] font-semibold tracking-wide">{name}</div>
              <div className="text-dim">{active ? "Running…" : "Waiting…"}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
