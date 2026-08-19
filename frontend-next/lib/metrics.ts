/**
 * Metrics derived from the runs the API actually returned.
 *
 * Everything here is computed from real fields on real records. Nothing is
 * sampled, estimated or padded — if the backend does not know something, no
 * metric for it appears. In particular there is no per-field extraction
 * confidence anywhere in this file, because the pipeline does not produce one
 * yet; inventing a percentage would be inventing evidence.
 */
import type { PurchaseOrder, RunRecord } from "./types";

export interface Totals {
  runs: number;
  approved: number;
  needsReview: number;
  rejected: number;
  /** Held runs nobody has ruled on — the actionable queue. */
  openExceptions: number;
  /** Approved without a human having to touch it, as a share of all runs. */
  straightThroughRate: number | null;
  valueProcessed: number;
  valueApproved: number;
  valueHeld: number;
  /** MEAN wall-clock time across runs that recorded stage timings — not a
   *  median. The tile that renders it is labelled accordingly. */
  avgProcessingMs: number | null;
  touchedByHuman: number;
}

const sumStages = (r: RunRecord) =>
  (r.stages || []).reduce((acc, s) => acc + (typeof s.ms === "number" ? s.ms : 0), 0);

export function totals(runs: RunRecord[]): Totals {
  const by = (s: string) => runs.filter((r) => r.status === s);

  const approved = by("APPROVED");
  const needsReview = by("NEEDS_REVIEW");
  const rejected = by("REJECTED");

  // "Straight through" means the rules approved it on their own. A run a person
  // accepted is a success, but it is not automation, so it does not count here.
  const auto = runs.filter(
    (r) => r.status === "APPROVED" && !r.human_decision
  ).length;

  const timed = runs.map(sumStages).filter((ms) => ms > 0);
  const value = (list: RunRecord[]) => list.reduce((a, r) => a + (r.total || 0), 0);

  return {
    runs: runs.length,
    approved: approved.length,
    needsReview: needsReview.length,
    rejected: rejected.length,
    openExceptions: runs.filter(
      (r) => (r.automated_decision ?? r.status) === "NEEDS_REVIEW" && !r.human_decision
    ).length,
    straightThroughRate: runs.length ? auto / runs.length : null,
    valueProcessed: value(runs),
    valueApproved: value(approved),
    valueHeld: value(needsReview),
    avgProcessingMs: timed.length ? timed.reduce((a, b) => a + b, 0) / timed.length : null,
    touchedByHuman: runs.filter((r) => !!r.human_decision).length,
  };
}

export interface DayBucket {
  day: string;
  label: string;
  approved: number;
  needsReview: number;
  rejected: number;
  total: number;
}

/** Local calendar-day key. toISOString() would bucket by UTC while the axis is
 *  built from local dates, so east of Greenwich today's runs land in
 *  yesterday's bucket — or outside the window entirely. */
const dayKey = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate()
  ).padStart(2, "0")}`;

/** Runs grouped by calendar day, oldest first, with empty days kept so the
 *  x-axis stays honest about gaps in activity. */
export function byDay(runs: RunRecord[], days = 14): DayBucket[] {
  const buckets = new Map<string, DayBucket>();
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    const key = dayKey(d);
    buckets.set(key, {
      day: key,
      label: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
      approved: 0,
      needsReview: 0,
      rejected: 0,
      total: 0,
    });
  }

  for (const r of runs) {
    if (!r.created_at) continue;
    const b = buckets.get(dayKey(new Date(r.created_at)));
    if (!b) continue;                       // outside the window
    if (r.status === "APPROVED") b.approved++;
    else if (r.status === "NEEDS_REVIEW") b.needsReview++;
    else if (r.status === "REJECTED") b.rejected++;
    b.total++;
  }

  return [...buckets.values()];
}

export interface PoUsage {
  po: PurchaseOrder;
  consumed: number;
  remaining: number;
  pct: number;
  over: boolean;
}

/** Consumption mirrors the backend rule exactly: only APPROVED runs consume a
 *  PO's budget. Held and rejected invoices deliberately do not. */
export function poUsage(runs: RunRecord[], pos: PurchaseOrder[]): PoUsage[] {
  const consumed = new Map<string, number>();
  for (const r of runs) {
    if (r.status !== "APPROVED" || !r.po_number) continue;
    consumed.set(r.po_number, (consumed.get(r.po_number) || 0) + (r.total || 0));
  }

  return pos.map((po) => {
    const used = consumed.get(po.po_number) || 0;
    return {
      po,
      consumed: used,
      remaining: po.amount - used,
      pct: po.amount > 0 ? Math.min(100, (used / po.amount) * 100) : 0,
      over: used > po.amount,
    };
  });
}

/**
 * Why invoices stop, grouped into categories.
 *
 * The engine's reason strings carry per-invoice specifics ("Invoice #INV-3310-A
 * for 3000.0 …"), so counting raw strings produces a list of individual
 * invoices rather than a list of causes. These patterns map each reason onto
 * the rule that produced it — the categories mirror rules.py, and anything
 * unrecognised falls through to a digit-stripped first clause rather than being
 * dropped, so a new rule still shows up here instead of silently vanishing.
 */
const REASON_CATEGORY: [RegExp, string][] = [
  // The duplicate rule words its finding as "matches run #7", never as
  // "duplicate", so matching the obvious word alone misses every one.
  [/duplicat|matches run|matches earlier|resubmi/i, "Duplicate submission"],
  [/missing required field/i, "Missing required fields"],
  [/no matching purchase order|no explicit po/i, "No matching purchase order"],
  [/over the remaining|outside the .*tolerance|exceeds/i, "Over the PO balance"],
  [/vendor .*not approved|not on the approved/i, "Vendor not approved"],
  [/nothing could be read|no embedded text|unreadable/i, "Document unreadable"],
  [/currency/i, "Currency mismatch"],
  [/subtotal|arithmetic/i, "Invoice arithmetic"],
  [/greater than zero|invalid .*amount|invalid .*total/i, "Invalid invoice total"],
  [/instruction|injection/i, "Suspicious instruction text"],
  [/inferred/i, "PO match only inferred"],
];

function categorise(text: string): string {
  for (const [re, label] of REASON_CATEGORY) if (re.test(text)) return label;
  return text.split(/[.:(]/)[0].replace(/[\d$,.#-]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 60);
}

export function topExceptionReasons(runs: RunRecord[], limit = 4) {
  const counts = new Map<string, number>();

  for (const r of runs) {
    if (r.status === "APPROVED") continue;
    // One count per category per invoice: a reason repeated inside a single run
    // should not inflate the ranking.
    const seen = new Set<string>();
    for (const raw of r.reasons || []) {
      const level = typeof raw === "string" ? "info" : raw.level || "info";
      if (level !== "fail") continue;             // only the reasons that bit
      const key = categorise(typeof raw === "string" ? raw : raw.text);
      if (!key || seen.has(key)) continue;
      seen.add(key);
      counts.set(key, (counts.get(key) || 0) + 1);
    }
  }

  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([reason, count]) => ({ reason, count }));
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export function formatPercent(v: number | null, digits = 0): string {
  if (v === null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

/** Compact money for KPI tiles, where $1,234,567.00 would not fit. */
export function compactMoney(v: number): string {
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (abs >= 10_000) return `$${(v / 1000).toFixed(1)}k`;
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}
