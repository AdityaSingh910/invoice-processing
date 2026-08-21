"use client";

/**
 * The pipeline, at two levels of detail.
 *
 * `PhaseStepper` is the six-phase summary an operator watches; `StageList` is
 * the nine stages the backend actually ran. Both derive from the same stage
 * array — the stepper aggregates, it does not track its own state, so it can
 * never claim a phase finished that the pipeline did not.
 *
 * Stages never short-circuit: findings accumulate and only DECISION judges, so
 * every stage stays visible with its own outcome rather than the list stopping
 * at the first problem.
 */
import { PHASES, STAGE_LABEL, STAGE_ORDER } from "@/lib/format";
import type { Stage } from "@/lib/types";
import { Spinner } from "@/components/ui";
import { IconAlert, IconCheck, IconX } from "@/components/ui/icons";

const TONE = {
  ok: { fg: "var(--ok)", bg: "var(--ok-quiet)", line: "var(--ok-line)" },
  warn: { fg: "var(--warn)", bg: "var(--warn-quiet)", line: "var(--warn-line)" },
  fail: { fg: "var(--bad)", bg: "var(--bad-quiet)", line: "var(--bad-line)" },
  info: { fg: "var(--accent)", bg: "var(--accent-quiet)", line: "var(--accent-line)" },
} as const;

function Glyph({ status, size = 11 }: { status: string; size?: number }) {
  if (status === "fail") return <IconX size={size} />;
  if (status === "warn") return <IconAlert size={size} />;
  if (status === "ok") return <IconCheck size={size} />;
  return <span className="h-1 w-1 rounded-full bg-current" />;
}

/* ------------------------------------------------------------------ stepper */

type PhaseState = "done" | "active" | "pending";

export function PhaseStepper({ stages, running }: { stages: Stage[]; running: boolean }) {
  const seen = new Map(stages.map((s) => [s.name, s]));
  const nextStage = STAGE_ORDER.find((n) => !seen.has(n));

  return (
    <ol className="flex items-center gap-1 overflow-x-auto pb-1">
      {PHASES.map((phase, i) => {
        const inPhase = phase.stages.map((n) => seen.get(n)).filter(Boolean) as Stage[];
        const complete = inPhase.length === phase.stages.length;
        const active = running && !complete && phase.stages.includes(nextStage ?? "");
        const state: PhaseState = complete ? "done" : active ? "active" : "pending";

        // The phase inherits the worst outcome inside it: one failed check is
        // the thing the operator needs to see, not the two that passed.
        const worst = inPhase.some((s) => s.status === "fail")
          ? "fail"
          : inPhase.some((s) => s.status === "warn")
            ? "warn"
            : "ok";
        const tone = TONE[worst];

        return (
          <li key={phase.key} className="flex min-w-0 flex-1 items-center gap-1">
            <div
              className={`flex min-w-0 flex-1 items-center gap-2 rounded-[var(--radius-md)] px-2 py-1.5 transition-colors ${
                state === "pending" ? "opacity-45" : ""
              } ${state === "active" ? "bg-accent-quiet" : ""}`}
            >
              <span
                className="grid h-[18px] w-[18px] shrink-0 place-items-center rounded-full border text-[10px]"
                style={
                  state === "done"
                    ? { background: tone.bg, borderColor: tone.line, color: tone.fg }
                    : state === "active"
                      ? { borderColor: "var(--accent)", color: "var(--accent)" }
                      : { borderColor: "var(--line-strong)", color: "var(--fg-faint)" }
                }
              >
                {state === "done" ? (
                  <Glyph status={worst} size={10} />
                ) : state === "active" ? (
                  <Spinner size={9} />
                ) : (
                  <span className="tnum">{i + 1}</span>
                )}
              </span>
              <span
                className={`truncate text-[11.5px] ${
                  state === "pending" ? "text-faint" : "font-medium"
                }`}
              >
                {phase.label}
              </span>
            </div>
            {i < PHASES.length - 1 && (
              <span
                aria-hidden
                className={`hidden h-px w-3 shrink-0 sm:block ${
                  complete ? "bg-line-strong" : "bg-line"
                }`}
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}

/* --------------------------------------------------------------- stage list */

export function StageRow({ stage, last }: { stage: Stage; index?: number; last: boolean }) {
  const tone = TONE[stage.status as keyof typeof TONE] ?? TONE.info;

  return (
    <li className={`relative flex gap-3 ${last ? "" : "pb-3.5"}`}>
      {!last && <span className="absolute top-5 bottom-0 left-[9px] w-px bg-line" aria-hidden />}
      <span
        className="relative z-10 mt-0.5 grid h-[19px] w-[19px] shrink-0 place-items-center rounded-full border"
        style={{ background: tone.bg, borderColor: tone.line, color: tone.fg }}
      >
        <Glyph status={stage.status} />
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3">
          <span className="text-[12.5px] font-medium">
            {STAGE_LABEL[stage.name] ?? stage.name}
          </span>
          <span className="flex items-center gap-2">
            <code className="rounded-[var(--radius-xs)] bg-sunken px-1 py-px text-[10px] text-faint">
              {stage.name}
            </code>
            {stage.ms !== undefined && (
              <span className="tnum text-[10.5px] text-faint">{stage.ms} ms</span>
            )}
          </span>
        </div>
        <p className="t-meta mt-0.5 leading-snug">{stage.detail}</p>
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
  // Nothing has been submitted yet, so this list is a PLAN rather than a
  // progress report. Nine rows each reading "Waiting" is nine repetitions of
  // one fact; at rest the stage names alone say what is about to happen, and
  // they are shown at full contrast because none of them has failed to do
  // anything yet.
  const idle = !running && stages.length === 0;

  return (
    <ol className="list-none">
      {STAGE_ORDER.map((name, i) => {
        const last = i === STAGE_ORDER.length - 1;
        const done = seen.get(name);
        if (done) return <StageRow key={name} stage={done} last={last} />;

        const active = running && name === next;
        return (
          <li
            key={name}
            className={`relative flex gap-3 transition-opacity ${
              last ? "" : idle ? "pb-2.5" : "pb-3.5"
            } ${active || idle ? "" : "opacity-40"}`}
          >
            {!last && (
              <span className="absolute top-5 bottom-0 left-[9px] w-px bg-line" aria-hidden />
            )}
            <span
              className={`relative z-10 mt-0.5 grid h-[19px] w-[19px] shrink-0 place-items-center
                rounded-full border bg-surface ${
                  idle ? "border-line-strong text-muted" : "border-line text-faint"
                }`}
            >
              {active ? <Spinner size={9} /> : <span className="tnum text-[9.5px]">{i + 1}</span>}
            </span>
            <div className="min-w-0 flex-1">
              <span className={`text-[12.5px] font-medium ${idle ? "text-secondary" : ""}`}>
                {STAGE_LABEL[name] ?? name}
              </span>
              {!idle && <p className="t-meta mt-0.5">{active ? "Running…" : "Waiting"}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
