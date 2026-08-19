"use client";

/** The deterministic reasons the rule engine emitted, rendered verbatim. */
import { LEVEL_ICON } from "@/lib/format";
import type { Reason } from "@/lib/types";

export default function ReasonList({ reasons }: { reasons: Reason[] }) {
  return (
    <ul className="grid gap-2.5">
      {reasons.map((r, i) => {
        const level = typeof r === "string" ? "info" : r.level || "info";
        const text = typeof r === "string" ? r : r.text;
        return (
          <li key={i} className="flex items-start gap-3 text-[14px]">
            <span className="dot mt-0.5" data-level={level}>
              {LEVEL_ICON[level] || "i"}
            </span>
            <span className="min-w-0 text-dim">{text}</span>
          </li>
        );
      })}
    </ul>
  );
}
