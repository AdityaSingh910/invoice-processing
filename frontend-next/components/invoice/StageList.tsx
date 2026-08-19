"use client";

/**
 * The nine pipeline stages.
 *
 * Stages do not short-circuit: findings accumulate and only DECISION judges, so
 * every stage stays visible and reports its own outcome rather than the list
 * stopping at the first problem. That is a property of the backend worth making
 * legible — a clerk needs the whole picture, not the first thing that failed.
 */
import { STAGE_LABEL, STAGE_ORDER } from "@/lib/format";
import type { Stage } from "@/lib/types";
import { IconAlert, IconCheck, IconX } from "@/components/ui/icons";
import { Spinner } from "@/components/ui";

const TONE = {
  ok: { fg: "var(--success)", bg: "var(--success-weak)", line: "var(--success-line)" },
  warn: { fg: "var(--warning)", bg: "var(--warning-weak)", line: "var(--warning-line)" },
  fail: { fg: "var(--danger)", bg: "var(--danger-weak)", line: "var(--danger-line)" },
  info: { fg: "var(--accent)", bg: "var(--accent-weak)", line: "var(--accent-line)" },
} as const;

function Glyph({ status }: { status: string }) {
  if (status === "ok") return <IconCheck size={12} />;
  if (status === "fail") return <IconX size={12} />;
  if (status === "warn") return <IconAlert size={12} />;
  return <span className="h-1 w-1 rounded-full bg-current" />;
}

export function StageRow({
  stage,
  index,
  last,
  live = false,
}: {
  stage: Stage;
  index: number;
  last: boolean;
  live?: boolean;
}) {
  const tone = TONE[stage.status as keyof typeof TONE] ?? TONE.info;

  return (
    <li className={`relative flex gap-3 ${last ? "" : "pb-4"} ${live ? "rise" : ""}`}>
      {!last && (
        <span className="absolute top-6 bottom-0 left-[11px] w-px bg-border" aria-hidden />
      )}
      <span
        className="relative z-10 mt-0.5 grid h-[22px] w-[22px] shrink-0 place-items-center rounded-full border"
        style={{ background: tone.bg, borderColor: tone.line, color: tone.fg }}
      >
        <Glyph status={stage.status} />
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
          <span className="text-[13px] font-semibold">
            {STAGE_LABEL[stage.name] ?? stage.name}
          </span>
          <span className="flex items-center gap-2">
            <code className="rounded-[var(--radius-xs)] bg-surface2 px-1 py-px text-[10px] text-subtle">
              {stage.name}
            </code>
            {stage.ms !== undefined && (
              <span className="num text-[11px] text-subtle">{stage.ms} ms</span>
            )}
          </span>
        </div>
        <p className="mt-0.5 text-[13px] leading-snug text-muted">{stage.detail}</p>
      </div>
    </li>
  );
}

export default function StageList({
  stages,
  running,
}: {
  stages: Stage[];
  running: boolean;
}) {
  const seen = new Map(stages.map((s) => [s.name, s]));
  const next = STAGE_ORDER.find((n) => !seen.has(n));

  return (
    <ol className="list-none">
      {STAGE_ORDER.map((name, i) => {
        const last = i === STAGE_ORDER.length - 1;
        const done = seen.get(name);
        if (done) return <StageRow key={name} stage={done} index={i} last={last} live={running} />;

        const active = running && name === next;
        return (
          <li
            key={name}
            className={`relative flex gap-3 transition-opacity ${last ? "" : "pb-4"} ${
              active ? "opacity-100" : "opacity-40"
            }`}
          >
            {!last && (
              <span className="absolute top-6 bottom-0 left-[11px] w-px bg-border" aria-hidden />
            )}
            <span className="relative z-10 mt-0.5 grid h-[22px] w-[22px] shrink-0 place-items-center rounded-full border border-border bg-surface text-subtle">
              {active ? <Spinner size={11} /> : <span className="num text-[10px]">{i + 1}</span>}
            </span>
            <div className="min-w-0 flex-1">
              <span className="text-[13px] font-semibold">{STAGE_LABEL[name] ?? name}</span>
              <p className="mt-0.5 text-[13px] text-muted">
                {active ? "Running…" : "Waiting"}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
